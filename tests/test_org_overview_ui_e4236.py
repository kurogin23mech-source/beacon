"""Web/Desktop UI presence tests for the org 俯瞰 (read-only) — ms-118 / e-4236 slice2.

slice1 (e-4236) は data 層 (GET /api/orgs/{id}/overview) を land した。slice2 は
その俯瞰を Settings > Projects & Members タブに read-only で描く frontend view。

ここでは「構造的な配線が index.html (Web) と dist/index.html (Tauri) の両方に
在る」ことを pin する。JS の実挙動は手動 / e2e で見るが、以下は future の
"simplify" 編集が黙ってこの view を消したり、二層 parity を割ったりするのを防ぐ
regression guard (= test_treks_tab_ui.py と同じ狙い):

  - dataSource が loadOrgs / loadOrgOverviews を持つ (= 俯瞰が食うデータ取得)
  - overview endpoint (/api/orgs/{id}/overview) を消費している
  - 俯瞰セクションの render helper (_renderSettingsOrgsSection) が在る
  - read-only 不変条件: 俯瞰の render helper は data-action を 1 つも emit しない
    (= SPEC 方針5「書き込みは当面 CLI / AI 代行」、handler parity 不要の担保)
  - Web と Tauri artifact の両方に上記が在る (二層 parity)
"""
from __future__ import annotations

import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WEB_INDEX = REPO_ROOT / "server" / "static" / "index.html"
DESKTOP_INDEX = REPO_ROOT / "desktop" / "dist" / "index.html"


def _read(path):
    return path.read_text(encoding="utf-8")


class TestOrgOverviewUiPresent:
    def setup_method(self, _method):
        self.web = _read(WEB_INDEX)
        self.desktop = _read(DESKTOP_INDEX)

    def test_datasource_loaders_present(self):
        # 二層 parity: loadOrgs / loadOrgOverviews は Web (実 HTTP) と Tauri
        # (Rust binding 未着地の no-op stub) の両方に存在する。SHARED の
        # openSettings がどちらでも呼べる (= method parity) ことを担保する。
        for src, label in [(self.web, "web"), (self.desktop, "desktop")]:
            assert "loadOrgs:" in src, f"loadOrgs missing in {label}"
            assert "loadOrgOverviews:" in src, f"loadOrgOverviews missing in {label}"

    def test_web_consumes_overview_endpoint(self):
        # Web は slice1 の集約 endpoint を HTTP で消費する (= data 層の再利用)。
        # Tauri は全 backend を Rust invoke() 経由にする設計なので HTTP path は
        # 持たない (org 用 Rust binding は follow-up、この build では no-op stub)。
        assert "/api/orgs/${encodeURIComponent(oid)}/overview" in self.web, \
            "web must consume the overview endpoint"
        assert "'/api/orgs'" in self.web, "web must consume the org list endpoint"

    def test_render_helper_present(self):
        for src, label in [(self.web, "web"), (self.desktop, "desktop")]:
            assert "_renderSettingsOrgsSection" in src, \
                f"org overview render helper missing in {label}"
            # Members タブ本体から呼ばれていること (= 実際に描画経路に載る)。
            assert "${_renderSettingsOrgsSection()}" in src, \
                f"org overview section not wired into members tab in {label}"

    def test_overview_section_is_read_only(self):
        # read-only 不変条件: 俯瞰 render helper の本体に data-action を混ぜない。
        # helper 定義 (function _renderSettingsOrgsSection ... 次の function まで) を
        # 切り出して data-action の不在を確認する。
        marker = "function _renderSettingsOrgsSection()"
        start = self.web.index(marker)
        # 次の top-level function 定義までを helper 本体とみなす。
        nxt = self.web.find("\nfunction ", start + len(marker))
        body = self.web[start:nxt if nxt != -1 else len(self.web)]
        assert "data-action" not in body, (
            "org overview は read-only。data-action を持たせない "
            "(SPEC 方針5: 書き込みは CLI / AI 代行)"
        )

    def test_team_org_only_filter(self):
        # 俯瞰は team org (org-t-*) のみ。personal org は出さない (受入条件7 の意図)。
        assert "org-t-" in self.web
