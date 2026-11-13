"""mode 語彙ガードが **出力スキーマ例の JSON** まで見ることの固定 (ms-119)。

**なぜこの試験が要るか。** `scripts/check-review-mode-vocab.py` は「instrument が
kernel に無い mode を名乗ったら赤」を担う。初版は散文のバッククォート語だけを
抽出していたので、instrument が judge に書かせる **出力スキーマ例** (JSON の
二重引用符) がまるごと死角だった。そしてその死角に、この検査が消すために作られた
``"mode": "diff | full"`` が ``maintainability-review`` に居残っていた —— 検査は
緑、名乗りはズレたまま (独立保守性レビュー high, 2026-11-10)。

**緑のガードは信頼されるので、偽の緑は無ガードより悪い。** だからここでは
「ガードが在る」ことではなく **「壊したら赤くなる」こと** と
**「壊していないのに赤くならない」こと** の両方を測る。片方だけだと、
偽の緑 (ザルな一致) か偽の赤 (過剰な抽出) のどちらかに倒れる。
"""

import importlib.util
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
GUARD = ROOT / "scripts" / "check-review-mode-vocab.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("review_mode_vocab", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# 出力スキーマ例の形。**バッククォートが無い**のが要点 (初版の死角)。
_SCHEMA_EXAMPLE = '''## 出力 (JSON)

```json
{
  "mode": "%s",
  "target": "PR #1"
}
```
'''


def test_json_schema_example_is_not_a_blind_spot():
    """JSON 例に書かれた mode 名が抽出されること (死角が閉じたことの固定)。"""
    mod = _load_guard()
    path = ROOT / "skills" / "maintainability-review" / "SKILL.md"
    declared = mod._declared_modes(str(path))
    # いまの正本は説明形なので、kernel の語がここから採れている。
    assert declared, "出力スキーマ例から mode 名が 1 つも採れていない (死角が再発)"
    assert declared <= set(mod.review_spine.ARTIFACT_MODES), (
        f"正本が kernel に無い mode を名乗っている: "
        f"{sorted(declared - set(mod.review_spine.ARTIFACT_MODES))}")


def test_guard_goes_red_when_json_example_names_an_unknown_mode(tmp_path):
    """**反転**: JSON 例に kernel に無い ``full`` を仕込むと赤くなること。

    初版はこの形で緑だった。ここが緑に戻ったら、ガードは意味を失っている。
    """
    mod = _load_guard()
    kernel = set(mod.review_spine.ARTIFACT_MODES)
    assert "full" not in kernel, (
        "前提が崩れた: kernel が 'full' を持つなら、この試験が測る反転が成立しない")

    seeded = tmp_path / "SKILL.md"
    seeded.write_text(_SCHEMA_EXAMPLE % "diff | full", encoding="utf-8")

    declared = mod._declared_modes(str(seeded))
    assert "full" in declared, (
        "JSON 例の 'full' を抽出できていない —— これが初版の偽の緑そのもの。"
        f"抽出結果: {sorted(declared)}")
    assert declared - kernel == {"full"}, (
        f"未知語の集合が 'full' だけでない (抽出が粗い): {sorted(declared - kernel)}")


def test_guard_main_exits_nonzero_when_an_instrument_drifts(tmp_path, monkeypatch):
    """スクリプト全体 (main) として非 0 で終わること。

    ``_declared_modes`` 単体が正しくても、main がその結果を判定に使っていなければ
    CI は緑のままになる。抽出と判定の両方を通して測る。
    """
    mod = _load_guard()
    skills = tmp_path / "skills"
    for name in mod.INSTRUMENTS:
        d = skills / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            _SCHEMA_EXAMPLE % "diff | whole-target", encoding="utf-8")
    # 1 つだけドリフトさせる。
    (skills / mod.INSTRUMENTS[0] / "SKILL.md").write_text(
        _SCHEMA_EXAMPLE % "diff | full", encoding="utf-8")

    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    assert mod.main() == 1, "instrument がドリフトしているのに main が 0 を返した"


def test_guard_main_is_green_when_no_instrument_drifts(tmp_path, monkeypatch):
    """**偽の赤**も測る: 壊していないのに赤くならないこと。

    抽出を広げると、説明文の混じった断片から非 mode 語 (``bundle`` など) を拾って
    偽の赤を出す方向に倒れやすい。正本が使っている説明形をそのまま置いて緑を固定する。
    """
    mod = _load_guard()
    explanatory = "bundle の mode の値をそのまま (diff / whole-target / full-surface / decision-audit)"
    skills = tmp_path / "skills"
    for name in mod.INSTRUMENTS:
        d = skills / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            _SCHEMA_EXAMPLE % explanatory, encoding="utf-8")

    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    assert mod.main() == 0, (
        "説明形の mode 行で偽の赤が出た (抽出が説明文から非 mode 語を拾っている)")

    # 説明文の語を mode として拾っていないことを直接も固定する。
    declared = mod._declared_modes(
        str(skills / mod.INSTRUMENTS[0] / "SKILL.md"))
    assert "bundle" not in declared, (
        f"説明文の 'bundle' を mode として拾っている: {sorted(declared)}")


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
