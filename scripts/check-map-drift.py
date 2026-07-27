#!/usr/bin/env python3
"""check-map-drift.py — application-map の照合 (ms-104 / e-3151)

全貌マップ (CORE doc `application-map`) が「メンテされている」を
「メンテされている *はず*」から分ける唯一の楔 = 機械照合。

2 方向で突く:
  - 書き漏れ (missing): source に実在する surface が、地図のどの楔にも
    覆われていない → 「地図に足せ」
  - 幽霊 (phantom):   地図の楔が source の実在に解決しない → 「地図から消せ」

surface は 4 種、楔は `type:ident` の 1 形式 (map doc 内に backtick で埋め込む):
  cli:   `cli:beacon task done`      exact
         `cli:beacon task *`         family (= その noun 配下すべてを覆う)
  api:   `api:POST /api/.../done`    exact ({param} 名は正規化)
         `api:* /api/treks/*`        family (= path prefix)
  skill: `skill:/beacon-task`        exact
  file:  `file:channel/bus.mjs`      exact / glob (= 裏方の仕組み用、CLI verb 無し)

source of truth:
  cli   = lib/commands.py の dispatch dict (commands = {...}) のキー
  api   = server/app.py の @app.<method> + server/trailnode*.py の @router.<method>
  skill = skills/*.md

使い方:
  python3 scripts/check-map-drift.py <map.md>        # ファイルを照合
  python3 scripts/check-map-drift.py --enumerate     # source の実在 surface を列挙のみ
  python3 scripts/check-map-drift.py <map.md> --json  # 機械可読
exit 0 = drift 無し / 1 = drift 有り (CI / map-drift trigger backstop 用)
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import re
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# CLI surface enumeration lives in lib/cli_surface.py (the single source of truth
# shared with lib/verb_ledger.py) so the two readers can never drift (ms-114
# e-3740). This lint imports it rather than keeping its own copy.
sys.path.insert(0, os.path.join(REPO, "lib"))
from cli_surface import enumerate_cli_verbs  # noqa: E402

# --- 不規則綴りの exact 楔 → dispatch key の alias (= 綴りが key と機械変換で一致しない分) ---
CLI_SPELLING_ALIAS = {
    "cloud upload-initial": "cloud_push",
    "setup": "common_setup",
}


def enumerate_cli() -> set[str]:
    """lib/commands.py の dispatch dict キー + bin/beacon 直処理の top-level を列挙
    (= 共有の cli_surface.enumerate_cli_verbs)。"""
    return enumerate_cli_verbs(os.path.join(REPO, "lib", "commands.py"))


def cli_noun(key: str) -> str:
    return key.split("_", 1)[0]


def normalize_cli_wedge_to_key(spelling: str) -> str:
    """exact cli 楔 `beacon task done` → dispatch key `task_done`。"""
    spelling = spelling.strip()
    if spelling.startswith("beacon "):
        spelling = spelling[len("beacon "):]
    if spelling in CLI_SPELLING_ALIAS:
        return CLI_SPELLING_ALIAS[spelling]
    return spelling.replace("-", "_").replace(" ", "_")


def _norm_path(p: str) -> str:
    """API path の {param 名} を {} に潰して比較安定化。root '/' は保持。"""
    n = re.sub(r"\{[^}]+\}", "{}", p.rstrip("/"))
    return n or "/"


def enumerate_api() -> set[str]:
    """@app.<method>('path') と router (prefix 付き) を列挙 → 'METHOD path'。"""
    routes: set[str] = set()
    app = open(os.path.join(REPO, "server", "app.py"), encoding="utf-8").read()
    for meth, path in re.findall(r'@app\.(get|post|put|patch|delete|websocket)\(\s*["\']([^"\']+)["\']', app):
        routes.add(f"{meth.upper()} {_norm_path(path)}")
    for fname, prefix in (("trailnode.py", "/api/trailnode"), ("trailnode_orgs.py", "/api/trailnode/orgs")):
        fp = os.path.join(REPO, "server", fname)
        if not os.path.exists(fp):
            continue
        txt = open(fp, encoding="utf-8").read()
        for meth, path in re.findall(r'@router\.(get|post|put|patch|delete)\(\s*["\']([^"\']*)["\']', txt):
            routes.add(f"{meth.upper()} {_norm_path(prefix + path)}")
    return routes


def enumerate_skills() -> set[str]:
    """skills/*.md → '/beacon-xxx' (methodology の _ 先頭も含む)。"""
    out: set[str] = set()
    for fp in globmod.glob(os.path.join(REPO, "skills", "*.md")):
        base = os.path.basename(fp)[:-3]
        out.add("/" + base)
    return out


# ------------------------------------------------------------------ wedges

WEDGE_RE = re.compile(r"`(cli|api|skill|file):([^`]+)`")


def parse_wedges(text: str) -> list[tuple[str, str]]:
    return [(t, v.strip()) for t, v in WEDGE_RE.findall(text)]


def reconcile(map_text: str) -> dict:
    wedges = parse_wedges(map_text)
    real_cli = enumerate_cli()
    real_api = enumerate_api()
    real_skill = enumerate_skills()

    cli_exact, cli_family = set(), set()
    api_exact, api_family = set(), set()
    skill_wedges, file_wedges = set(), set()
    for t, v in wedges:
        if t == "cli":
            if v.rstrip().endswith("*"):
                # `beacon <noun> *` → family は noun
                cli_family.add(v.rstrip()[:-1].strip().removeprefix("beacon ").strip())
            else:
                cli_exact.add(normalize_cli_wedge_to_key(v))
        elif t == "api":
            meth, _, path = v.partition(" ")
            if meth.strip() == "*":
                # `* /prefix` → method wildcard + path prefix family (末尾 * / は落とす)
                path = path.strip()
                if path.endswith("*"):
                    path = path[:-1]
                api_family.add(_norm_path(path).rstrip("/"))
            else:
                api_exact.add(f"{meth.strip().upper()} {_norm_path(path.strip())}")
        elif t == "skill":
            skill_wedges.add(v.strip())
        elif t == "file":
            file_wedges.add(v.strip())

    # ---- CLI coverage: 各 real key は noun-family か exact で覆われるべき ----
    covered_nouns = set(cli_family)
    cli_missing = sorted(
        k for k in real_cli
        if cli_noun(k) not in covered_nouns and k not in cli_exact
    )
    cli_phantom_family = sorted(
        f"beacon {n} *" for n in cli_family
        if not any(cli_noun(k) == n for k in real_cli)
    )
    cli_phantom_exact = sorted(
        f"beacon {s.replace('_', ' ')}" for s in cli_exact if s not in real_cli
    )

    # ---- API coverage ----
    def api_covered(route: str) -> bool:
        if route in api_exact:
            return True
        path = route.split(" ", 1)[1] if " " in route else route
        return any(path.startswith(pref) for pref in api_family)

    api_missing = sorted(r for r in real_api if not api_covered(r))
    api_phantom_family = sorted(
        f"* {p}/*" for p in api_family
        if not any(r.split(" ", 1)[1].startswith(p) for r in real_api)
    )
    api_phantom_exact = sorted(r for r in api_exact if r not in real_api)

    # ---- Skill: 完全一致集合 ----
    skill_missing = sorted(real_skill - skill_wedges)
    skill_phantom = sorted(skill_wedges - real_skill)

    # ---- File: 存在 (glob 可) ----
    file_phantom = []
    for f in sorted(file_wedges):
        matches = globmod.glob(os.path.join(REPO, f))
        if not matches:
            file_phantom.append(f)

    missing_total = len(cli_missing) + len(api_missing) + len(skill_missing)
    phantom_total = (len(cli_phantom_family) + len(cli_phantom_exact)
                     + len(api_phantom_family) + len(api_phantom_exact)
                     + len(skill_phantom) + len(file_phantom))

    return {
        "counts": {
            "real": {"cli": len(real_cli), "api": len(real_api), "skill": len(real_skill)},
            "wedges": {"cli_exact": len(cli_exact), "cli_family": len(cli_family),
                       "api_exact": len(api_exact), "api_family": len(api_family),
                       "skill": len(skill_wedges), "file": len(file_wedges)},
            "missing": missing_total, "phantom": phantom_total,
        },
        "missing": {"cli": cli_missing, "api": api_missing, "skill": skill_missing},
        "phantom": {
            "cli_family": cli_phantom_family, "cli_exact": cli_phantom_exact,
            "api_family": api_phantom_family, "api_exact": api_phantom_exact,
            "skill": skill_phantom, "file": file_phantom,
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("map", nargs="?", help="全貌マップの markdown ファイル")
    ap.add_argument("--doc-id", help="ファイルの代わりに live CORE doc を `beacon doc show` で照合")
    ap.add_argument("--enumerate", action="store_true", help="source の実在 surface を列挙のみ")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.enumerate:
        data = {
            "cli": sorted(enumerate_cli()),
            "api": sorted(enumerate_api()),
            "skill": sorted(enumerate_skills()),
        }
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            for kind, items in data.items():
                print(f"=== {kind} ({len(items)}) ===")
                for it in items:
                    print(f"  {it}")
        return 0

    if args.doc_id:
        try:
            text = subprocess.run(
                ["beacon", "doc", "show", args.doc_id],
                capture_output=True, text=True, timeout=30, check=True,
            ).stdout
        except Exception as e:
            print(f"FATAL: beacon doc show {args.doc_id} に失敗: {e}", file=sys.stderr)
            return 2
    elif args.map:
        text = open(args.map, encoding="utf-8").read()
    else:
        ap.error("map ファイル / --doc-id / --enumerate のいずれかを指定してください")
    res = reconcile(text)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["counts"]["missing"] == 0 and res["counts"]["phantom"] == 0 else 1

    c = res["counts"]
    print(f"real: cli={c['real']['cli']} api={c['real']['api']} skill={c['real']['skill']}")
    print(f"書き漏れ (missing): {c['missing']}  /  幽霊 (phantom): {c['phantom']}")
    if res["missing"]["cli"]:
        print("\n[書き漏れ CLI] 実在するが地図に無い noun/leaf:")
        for k in res["missing"]["cli"]:
            print(f"  - beacon {k.replace('_', ' ')}")
    if res["missing"]["api"]:
        print("\n[書き漏れ API]:")
        for r in res["missing"]["api"]:
            print(f"  - {r}")
    if res["missing"]["skill"]:
        print("\n[書き漏れ Skill]:")
        for s in res["missing"]["skill"]:
            print(f"  - {s}")
    ph = res["phantom"]
    for label, items in (("CLI family", ph["cli_family"]), ("CLI exact", ph["cli_exact"]),
                         ("API family", ph["api_family"]), ("API exact", ph["api_exact"]),
                         ("Skill", ph["skill"]), ("File", ph["file"])):
        if items:
            print(f"\n[幽霊 {label}] 地図にあるが実在しない:")
            for it in items:
                print(f"  - {it}")
    return 0 if c["missing"] == 0 and c["phantom"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
