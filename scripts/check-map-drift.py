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
exit 0 = drift 無し / 1 = drift 有り (CI / map-drift trigger backstop 用) / 2 = fatal

文脈ガード (e-5320): 機械照合の真値源 (lib/commands.py・server/app.py・skills/) は
beacon 本体の codebase 構造に固定されている。beacon 以外のプロジェクトで走らせても
beacon 自身の surface を列挙するだけなので、その場合は照合を refuse し「この map は
AI 維持のみ・機械の安全網なし」と明示して exit 0 で抜ける (SKIP: 行 / json は
{"skipped": ...})。判定は cwd から上に辿った source 署名で行い、REPO への cwd 比較は
使わない (pipx install で REPO != 開発リポになっても開発リポは誤 skip しない)。
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

# --- 文脈ガード (e-5320) -------------------------------------------------
# enumerate_cli / _api / _skill は REPO 配下の lib/commands.py・server/app.py・
# skills/ を読む = beacon の codebase 構造に固定されている。したがって機械照合が
# 意味を持つのは「今 map を突こうとしているプロジェクトが beacon 本体そのもの」の
# ときだけ。他プロジェクト (例: 営業ダッシュボード) で叩くと beacon 自身の surface を
# 列挙し、そのプロジェクトの地図に化ける (project-96ec39 からの報告で裏取り)。
#
# 判定は「現在のプロジェクト (= cwd から上に辿った root) が beacon の source
# 署名を持つか」で行い、`cwd == REPO` 比較は使わない: pipx install 時は REPO
# (= install 先) != 開発リポになり得て、`cwd == REPO` だと正規の beacon 開発リポで
# ガードが誤発火し、本体の map-drift CI が黙って skip される別の穴を開けるため。
# 署名探索は REPO に依存しないので、この pipx ケースでも開発リポ (= 署名を持つ) は
# 正しく素通りする。
_SOURCE_SIGNATURE = (
    ("lib", "commands.py"),   # CLI dispatch dict (cli surface の真値源)
    ("server", "app.py"),     # API route decorator (api surface の真値源)
    ("skills",),              # skills/*.md (skill surface の真値源)
)


def _has_source_signature(root: str) -> bool:
    for parts in _SOURCE_SIGNATURE:
        target = os.path.join(root, *parts)
        # skills/ はディレクトリ、他はファイル。isdir/isfile を分けて確認する。
        if len(parts) == 1:
            if not os.path.isdir(target):
                return False
        elif not os.path.isfile(target):
            return False
    return True


def is_beacon_source_project(start: str | None = None) -> bool:
    """現在のプロジェクト (= cwd から上に辿った最寄りの beacon source root) が
    beacon 本体の source repo かを、署名ファイルの実在で判定する。

    True  = beacon 本体 (開発リポ / worktree / CI checkout) → 機械照合は有効。
    False = 他プロジェクト → 機械照合は beacon の surface を列挙するだけで無効。
    """
    cur = os.path.abspath(start if start is not None else os.getcwd())
    while True:
        if _has_source_signature(cur):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


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
    """@app.<method>('path') と router (prefix 付き) を列挙 → 'METHOD path'。

    ms-127 の god-module 分割 (e-4869〜4871) で /api/* の実装が app.py から
    server/routers_*.py (auth / admin / orgs / me / treks / projects / version …)
    へ順次 include_router で切り出された。これらの router は完全な /api/... path を
    自身の @router デコレータに書く (prefix なし) ので、`server/routers_*.py` を
    **glob で自動発見** して空 prefix で走査する。将来さらに router を割っても、
    ファイル名が routers_*.py なら列挙にこの関数の変更は要らない (= 切り出し先の
    足し忘れで移設済み route が「幽霊 (map にあるが実体なし)」と誤検知される
    偽陽性の穴を構造的に塞ぐ)。trailnode 系だけは相対 path を書くので glob に
    載せず、mount 時の prefix を明示補完する。
    """
    routes: set[str] = set()
    app = open(os.path.join(REPO, "server", "app.py"), encoding="utf-8").read()
    for meth, path in re.findall(r'@app\.(get|post|put|patch|delete|websocket)\(\s*["\']([^"\']+)["\']', app):
        routes.add(f"{meth.upper()} {_norm_path(path)}")
    # (file, prefix): trailnode は相対 path なので mount prefix を明示。
    scan: list[tuple[str, str]] = [
        (os.path.join(REPO, "server", "trailnode.py"), "/api/trailnode"),
        (os.path.join(REPO, "server", "trailnode_orgs.py"), "/api/trailnode/orgs"),
    ]
    # routers_*.py は完全 path を書くので空 prefix で glob 自動発見。
    scan += [(fp, "") for fp in sorted(globmod.glob(os.path.join(REPO, "server", "routers_*.py")))]
    for fp, prefix in scan:
        if not os.path.exists(fp):
            continue
        txt = open(fp, encoding="utf-8").read()
        for meth, path in re.findall(r'@router\.(get|post|put|patch|delete|websocket)\(\s*["\']([^"\']*)["\']', txt):
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

    # 文脈ガード (e-5320): beacon 本体の source repo 以外で走らせても、機械照合は
    # beacon 自身の CLI/API/Skill を列挙するだけで無意味 (むしろ他プロジェクトの
    # 地図を beacon の surface で「照合」して汚染する)。beacon 以外では refuse し、
    # 「この map は AI 維持のみ・機械の安全網なし」を明示する。CI / map-drift
    # backstop は常に beacon repo 内で走るので、この分岐には入らない。
    if not is_beacon_source_project():
        if args.json:
            print(json.dumps({
                "skipped": "not-beacon-source-project",
                "reason": "機械照合は beacon 本体の source (lib/commands.py・server/app.py・skills/) に固定されており、他プロジェクトでは無効",
            }, ensure_ascii=False, indent=2))
        else:
            print("SKIP: このプロジェクトは beacon 本体の source repo ではないため機械照合をスキップします。")
            print("  application-map の機械照合 (書き漏れ / 幽霊検出) は beacon の CLI/API/Skill 構造に固定されています。")
            print("  他プロジェクトで走らせると beacon 自身の surface を列挙してこのプロジェクトの地図に化けるため、実行しません。")
            print("  → この map は AI 維持のみ (機械の安全網なし) です。")
        # exit 0: 「照合できなかった」は drift 有り (=1) でも fatal (=2) でもない。
        # 呼び出し側 (skill / 万一の外部 CI) の build を落とさない。
        return 0

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
