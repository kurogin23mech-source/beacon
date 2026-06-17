"""ms-72 e-1779: Tauri Rust member + invitation commands — registration pin.

These tests don't spin up Tauri (= cargo / npm cost) — they pin the *contract*
between SHARED layer.js callers and the Rust `invoke_handler!` registration:

* Every cloud_* command the SHARED dataSource invokes is registered in
  `tauri::Builder::default().invoke_handler(...)` so the JS side never falls
  through to "command not found" at runtime.
* Every command exposes a `#[tauri::command] fn name(...)` signature with the
  argument names SHARED passes (`projectId`, `args.email`, `args.role`, etc.).
  A mismatch silently turns into a 400 from Tauri's invoke layer, which is the
  failure mode this test catches.

The build also has to wire the 10 new commands through `desktop/build.py` so
`desktop/dist/index.html` carries the SHARED Settings > Members + invitation
UI that calls them. That's covered by the dist-grep tests at the bottom.

Why not call cargo build / cargo tauri build here? CI in Beacon doesn't carry
the macOS toolchain at the moment, and the source-text contract above catches
the regressions that historically broke this path (= ms-72 prior throw stubs
silently shipped because no test pinned the wiring).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC_TAURI = ROOT / "desktop" / "src-tauri" / "src"
LIB_RS = SRC_TAURI / "lib.rs"
MEMBER_RS = SRC_TAURI / "member.rs"
CLOUD_HTTP_RS = SRC_TAURI / "cloud_http.rs"
LAYER_JS = ROOT / "desktop" / "layer.js"
DIST_HTML = ROOT / "desktop" / "dist" / "index.html"
WEB_HTML = ROOT / "server" / "static" / "index.html"


# ---------------------------------------------------------------------------
# Source files exist
# ---------------------------------------------------------------------------

def test_member_rs_exists():
    assert MEMBER_RS.is_file(), (
        f"{MEMBER_RS} missing — Rust member bindings not created"
    )


def test_cloud_http_rs_exists():
    assert CLOUD_HTTP_RS.is_file(), (
        f"{CLOUD_HTTP_RS} missing — shared HTTP plumbing not extracted"
    )


# ---------------------------------------------------------------------------
# Rust command registration
# ---------------------------------------------------------------------------

REQUIRED_COMMANDS = [
    "cloud_list_members",
    "cloud_invite_member",
    "cloud_change_member_role",
    "cloud_remove_member",
    "cloud_create_invitation",
    "cloud_list_invitations",
    "cloud_cancel_invitation",
    "cloud_preview_invitation",
    "cloud_accept_invitation",
    "cloud_logout",
]


@pytest.fixture(scope="module")
def lib_rs_text() -> str:
    return LIB_RS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def member_rs_text() -> str:
    return MEMBER_RS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def layer_js_text() -> str:
    return LAYER_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dist_html_text() -> str:
    return DIST_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def web_html_text() -> str:
    return WEB_HTML.read_text(encoding="utf-8")


def _extract_invoke_handler_block(lib_rs: str) -> str:
    """Return the body of `.invoke_handler(tauri::generate_handler![...])`."""
    m = re.search(
        r"\.invoke_handler\(tauri::generate_handler!\[(.*?)\]\)",
        lib_rs,
        re.DOTALL,
    )
    assert m, "invoke_handler!(tauri::generate_handler![..]) block not found in lib.rs"
    return m.group(1)


@pytest.mark.parametrize("cmd", REQUIRED_COMMANDS)
def test_command_registered_in_invoke_handler(lib_rs_text, cmd):
    block = _extract_invoke_handler_block(lib_rs_text)
    # Accept either `cmd` or `member::cmd` since member commands are namespaced.
    assert (
        re.search(rf"(^|[\s,]){cmd}\b", block) is not None
        or re.search(rf"member::{cmd}\b", block) is not None
    ), (
        f"{cmd} not registered in tauri::generate_handler!. "
        f"SHARED layer.js will get 'command not found' at runtime."
    )


@pytest.mark.parametrize("cmd", REQUIRED_COMMANDS)
def test_command_has_tauri_macro(member_rs_text, cmd):
    # Each member.rs command is `#[tauri::command]\npub fn <cmd>(...)`.
    pattern = rf"#\[tauri::command\]\s*pub fn\s+{cmd}\b"
    assert re.search(pattern, member_rs_text) is not None, (
        f"{cmd} missing #[tauri::command] + pub fn signature in member.rs"
    )


# ---------------------------------------------------------------------------
# JS → Rust argument shape contract
# ---------------------------------------------------------------------------

# (command, expected JS invoke args)
# Each tuple lists the top-level keys SHARED dataSource passes via invoke()'s
# second argument. Rust must accept them under the same names (serde renaming
# notwithstanding — the runtime contract is the JS key name).
JS_INVOKE_SHAPE = [
    ("cloud_list_members", ["projectId"]),
    ("cloud_invite_member", ["projectId", "args"]),
    ("cloud_change_member_role", ["projectId", "args"]),
    ("cloud_remove_member", ["projectId", "args"]),
    ("cloud_create_invitation", ["projectId", "args"]),
    ("cloud_list_invitations", ["projectId"]),
    ("cloud_cancel_invitation", ["projectId", "args"]),
    ("cloud_preview_invitation", ["args"]),
    ("cloud_accept_invitation", ["args"]),
]


@pytest.mark.parametrize("cmd,keys", JS_INVOKE_SHAPE)
def test_layer_js_passes_expected_invoke_args(layer_js_text, cmd, keys):
    # Find the invoke('<cmd>', { ... }) call.
    m = re.search(
        rf"invoke\(\s*['\"]" + re.escape(cmd) + r"['\"]\s*,\s*\{([^}]+)\}",
        layer_js_text,
    )
    assert m, f"layer.js does not invoke {cmd}"
    arg_block = m.group(1)
    for k in keys:
        assert re.search(rf"\b{k}\s*:", arg_block) is not None, (
            f"invoke('{cmd}', ...) missing arg `{k}` in layer.js: {arg_block!r}"
        )


def test_cloud_logout_invoked_in_layer(layer_js_text):
    assert "invoke('cloud_logout')" in layer_js_text, (
        "Sign-out path must call cloud_logout in layer.js"
    )


# ---------------------------------------------------------------------------
# e-1774 — SHARED Members + invitation UI actually shipped into desktop/dist
# ---------------------------------------------------------------------------

# These are the SHARED-region tokens introduced by ms-78 (= invitation flow)
# that previously did not exist in the dist build. If any go missing the
# Settings > Members tab in Tauri silently lacks the invite UI.
SHARED_INVITATION_TOKENS = [
    "createInvitation",
    "listInvitations",
    "cancelInvitation",
    "previewInvitation",
    "acceptInvitation",
    "_settingsInviteMember",
    "_settingsLoadInvitationList",
    "_settingsCancelInvitation",
    "settings-invite-url-box",
    "settings-invitation-list",
    "settings-copy-invite-url",
    "renderName",
]


@pytest.mark.parametrize("token", SHARED_INVITATION_TOKENS)
def test_dist_has_shared_invitation_token(dist_html_text, token):
    assert token in dist_html_text, (
        f"desktop/dist/index.html missing SHARED invitation token {token!r}. "
        f"Re-run `python3 desktop/build.py` after editing server/static/index.html."
    )


@pytest.mark.parametrize("token", SHARED_INVITATION_TOKENS)
def test_web_keeps_shared_invitation_token(web_html_text, token):
    # Guards against a future rewrite of the Web UI accidentally removing the
    # SHARED tokens that desktop/build.py copies into dist.
    assert token in web_html_text, (
        f"server/static/index.html missing {token!r} — SHARED source of truth lost"
    )


def test_dist_has_admin_and_signout_buttons(dist_html_text):
    # ms-72 e-1779 — Tauri account menu must mirror Web's Admin + Sign out.
    # The throw-stub variant ("beacon auth logout from the terminal") MUST be
    # gone, otherwise we shipped both forms or the wrong one.
    assert 'data-action="goto-admin"' in dist_html_text
    assert 'data-action="logout"' in dist_html_text
    assert "beacon auth logout` from the terminal" not in dist_html_text, (
        "Old CLI-hint stub still in dist; PLATFORM.accountMenuItemsHTML "
        "should now render the Admin + Sign out buttons."
    )


def test_dist_has_tauri_logout_handler(dist_html_text):
    # The Tauri logout handler in layer.js must reach the dist; otherwise
    # clicking Sign out falls through SHARED's handler chain with no action.
    assert "invoke('cloud_logout')" in dist_html_text
    # And Tauri-specific goto-admin override must intercept BEFORE SHARED's
    # `gotoAdmin()` (= which calls window.location.assign('/admin') and breaks
    # the Tauri webview). We pin the comment string we wrote into layer.js so
    # accidental removal of the intercept fails the test.
    assert "Tauri-specific account-menu intercepts" in dist_html_text


# ---------------------------------------------------------------------------
# Throw stubs are gone (= regression guard)
# ---------------------------------------------------------------------------

LEGACY_STUB = "Member management is Web-only in this build"


def test_layer_js_no_member_throw_stub(layer_js_text):
    assert LEGACY_STUB not in layer_js_text, (
        "desktop/layer.js still throws the 'Web-only' stub for member ops; "
        "those branches must now invoke the Rust cloud_* commands."
    )


def test_dist_no_member_throw_stub(dist_html_text):
    assert LEGACY_STUB not in dist_html_text, (
        "desktop/dist/index.html still ships the 'Web-only' stub; re-run "
        "`python3 desktop/build.py` after updating desktop/layer.js."
    )


# ---------------------------------------------------------------------------
# Shared HTTP helpers reused (= no duplicate ureq clients)
# ---------------------------------------------------------------------------

def test_member_rs_uses_shared_cloud_http(member_rs_text):
    # member.rs must reuse the helpers in cloud_http.rs, not roll its own.
    assert "use crate::cloud_http::" in member_rs_text, (
        "member.rs should import HTTP helpers from cloud_http to keep a single "
        "auth/refresh code path."
    )
    # And must not contain a stray `ureq::get` / `ureq::post` (= would mean a
    # parallel HTTP client without the 401-refresh policy).
    assert "ureq::" not in member_rs_text, (
        "member.rs should not call ureq directly — go through cloud_http."
    )
