//! ms-72 e-1779 — Member admin + invitation Tauri bindings.
//!
//! Until this module landed, `desktop/layer.js` `dataSource` threw a "Web-only"
//! error for every member CRUD path, leaving the Tauri build with a structurally
//! incomplete Settings > Members tab (= ms-71 Phase 2-a 機能マトリクス の最後の穴).
//!
//! Two REST families are exposed here:
//!
//! * **Legacy direct-add member ops** (`cloud_list_members`, `cloud_invite_member`,
//!   `cloud_change_member_role`, `cloud_remove_member`). These map 1:1 to
//!   `/api/projects/{pid}/members[/{email}]`. They're the rows shown in the
//!   member list, plus the legacy "invite by email directly" flow used by the
//!   CLI for self-service add.
//! * **Token-based invitation flow** (`cloud_create_invitation`, `cloud_list_invitations`,
//!   `cloud_cancel_invitation`, `cloud_preview_invitation`, `cloud_accept_invitation`).
//!   This is the ms-78 invite URL flow: owner issues a token, invitee opens
//!   `/join/<token>`, logs in, and consumes it. Preview is public (no auth);
//!   the other three require the caller's Bearer token.
//!
//! Plus `cloud_logout` so the Tauri app can clear local credentials.json from
//! the account menu, matching the Web "Sign out" button (= ms-72 e-1779 AC #4).
//!
//! All HTTP plumbing reuses the same auth-token + 401-refresh pattern that
//! `lib.rs::cloud_get` / `cloud_post` use, so we stay on the single ureq client
//! and a single credentials store. Adding new bindings here must keep that
//! invariant (= no new HTTP client, no parallel auth path).

use serde::Deserialize;
use tauri::State;

use crate::cloud_http::{cloud_delete, cloud_get_raw, cloud_patch_json, cloud_post_json};
use crate::AppState;

// ---------------------------------------------------------------------------
// Legacy direct-add member ops (= rows in Settings > Members)
// ---------------------------------------------------------------------------

#[tauri::command]
pub fn cloud_list_members(
    state: State<AppState>,
    project_id: Option<String>,
) -> Result<String, String> {
    let pid = resolve_pid(&state, project_id)?;
    cloud_get_raw(&format!("/api/projects/{}/members", pid))
}

#[derive(Deserialize)]
pub struct InviteMemberArgs {
    pub email: String,
    pub role: String,
}

#[tauri::command]
pub fn cloud_invite_member(
    state: State<AppState>,
    project_id: Option<String>,
    args: InviteMemberArgs,
) -> Result<String, String> {
    let pid = resolve_pid(&state, project_id)?;
    let body = serde_json::json!({
        "email": args.email,
        "role": args.role,
    });
    cloud_post_json(&format!("/api/projects/{}/members", pid), &body)
}

#[derive(Deserialize)]
pub struct ChangeRoleArgs {
    pub email: String,
    pub role: String,
}

#[tauri::command]
pub fn cloud_change_member_role(
    state: State<AppState>,
    project_id: Option<String>,
    args: ChangeRoleArgs,
) -> Result<String, String> {
    let pid = resolve_pid(&state, project_id)?;
    let body = serde_json::json!({ "role": args.role });
    let path = format!(
        "/api/projects/{}/members/{}",
        pid,
        url_encode(&args.email),
    );
    cloud_patch_json(&path, &body)
}

#[derive(Deserialize)]
pub struct RemoveMemberArgs {
    pub email: String,
}

#[tauri::command]
pub fn cloud_remove_member(
    state: State<AppState>,
    project_id: Option<String>,
    args: RemoveMemberArgs,
) -> Result<String, String> {
    let pid = resolve_pid(&state, project_id)?;
    let path = format!(
        "/api/projects/{}/members/{}",
        pid,
        url_encode(&args.email),
    );
    cloud_delete(&path)
}

// ---------------------------------------------------------------------------
// ms-78 token-based invitation flow (= Generate invite URL button)
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
pub struct CreateInvitationArgs {
    pub email: String,
    pub role: String,
}

#[tauri::command]
pub fn cloud_create_invitation(
    state: State<AppState>,
    project_id: Option<String>,
    args: CreateInvitationArgs,
) -> Result<String, String> {
    let pid = resolve_pid(&state, project_id)?;
    let body = serde_json::json!({
        "email": args.email,
        "role": args.role,
    });
    cloud_post_json(&format!("/api/projects/{}/invitations", pid), &body)
}

#[tauri::command]
pub fn cloud_list_invitations(
    state: State<AppState>,
    project_id: Option<String>,
) -> Result<String, String> {
    let pid = resolve_pid(&state, project_id)?;
    cloud_get_raw(&format!("/api/projects/{}/invitations", pid))
}

#[derive(Deserialize)]
pub struct CancelInvitationArgs {
    #[serde(rename = "invitationId")]
    pub invitation_id: String,
}

#[tauri::command]
pub fn cloud_cancel_invitation(
    state: State<AppState>,
    project_id: Option<String>,
    args: CancelInvitationArgs,
) -> Result<String, String> {
    let pid = resolve_pid(&state, project_id)?;
    let path = format!(
        "/api/projects/{}/invitations/{}",
        pid,
        url_encode(&args.invitation_id),
    );
    cloud_delete(&path)
}

#[derive(Deserialize)]
pub struct PreviewInvitationArgs {
    pub token: String,
}

/// Public landing endpoint — no Bearer token required. Server returns project
/// name / role / inviter so the invitee can see "X invited you to Y as Z"
/// before signing in. We still go through cloud_get_raw which sends an
/// Authorization header if present; the server endpoint just ignores it.
#[tauri::command]
pub fn cloud_preview_invitation(args: PreviewInvitationArgs) -> Result<String, String> {
    crate::cloud_http::cloud_get_unauthed(&format!(
        "/api/invitations/{}",
        url_encode(&args.token),
    ))
}

#[derive(Deserialize)]
pub struct AcceptInvitationArgs {
    pub token: String,
    #[serde(default, rename = "displayName")]
    pub display_name: String,
}

#[tauri::command]
pub fn cloud_accept_invitation(args: AcceptInvitationArgs) -> Result<String, String> {
    let body = serde_json::json!({
        "display_name": args.display_name,
    });
    cloud_post_json(
        &format!("/api/invitations/{}/accept", url_encode(&args.token)),
        &body,
    )
}

// ---------------------------------------------------------------------------
// Sign out (= clear credentials.json so the next launch goes back to login)
// ---------------------------------------------------------------------------

/// Delete the local credentials file so subsequent `load_auth_token()` calls
/// return None. Returns Ok even if the file did not exist (= idempotent), so
/// the UI doesn't surface a confusing error when the user clicks Sign out on
/// an already-signed-out state. Matches Web's `logout()` which clears
/// localStorage tokens unconditionally.
#[tauri::command]
pub fn cloud_logout() -> Result<(), String> {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .map_err(|_| "HOME / USERPROFILE not set".to_string())?;
    let path = std::path::Path::new(&home).join(".beacon/credentials.json");
    if path.exists() {
        std::fs::remove_file(&path)
            .map_err(|e| format!("Failed to remove credentials.json: {}", e))?;
    }
    Ok(())
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/// Resolve the project_id either from the explicit JS argument (= invoked by
/// SHARED code that already knows `state.projectId`) or fall back to the
/// AppState slot set when the Tauri app loaded a cloud project. This mirrors
/// the existing `cloud_list_documents` / `cloud_load_project` pattern: SHARED
/// code passes pid explicitly, init paths use the stored one.
fn resolve_pid(state: &State<AppState>, explicit: Option<String>) -> Result<String, String> {
    if let Some(pid) = explicit {
        if !pid.is_empty() {
            return Ok(pid);
        }
    }
    state
        .cloud_project_id
        .lock()
        .unwrap()
        .clone()
        .ok_or_else(|| "No cloud project selected".to_string())
}

/// Minimal URL-encoder for path segments — we only need to protect the
/// characters that appear in emails (`@`, `+`, `.`) and invitation ids
/// (alphanumeric + `-` / `_`). Pulls no new dep and matches what Web's
/// `encodeURIComponent` produces for the legal subset we accept.
fn url_encode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for b in s.bytes() {
        let ok = matches!(
            b,
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~'
        );
        if ok {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{:02X}", b));
        }
    }
    out
}
