//! Shared HTTP plumbing for cloud_* Tauri commands.
//!
//! Extracted from lib.rs (= ms-72 e-1779) so the new member / invitation
//! bindings in member.rs can reuse the same auth-token + 401-refresh flow
//! without a second ureq client or a parallel credential path. lib.rs still
//! holds the token IO + api_url resolver because they're tightly coupled to
//! the rest of the app shell; this module only owns the HTTP verbs.
//!
//! All helpers retry once on a 401 by force-refreshing the id_token via
//! lib.rs::refresh_id_token(). That's the same shape cloud_get / cloud_post
//! already had — extending PATCH / DELETE / POST-with-body keeps the policy
//! uniform: every cloud_* command tolerates an expired access token without
//! the JS caller having to know.

use serde_json::Value;

use crate::{load_auth_token, refresh_id_token, resolve_api_url};

fn full_url(path: &str) -> String {
    format!("{}{}", resolve_api_url(), path)
}

/// Authenticated GET. Returns the response body as a String — the SHARED
/// dataSource layer JSON.parse()s it.
pub fn cloud_get_raw(path: &str) -> Result<String, String> {
    let token = load_auth_token().ok_or("Not authenticated. Run: beacon auth login")?;
    let url = full_url(path);
    match ureq::get(&url)
        .set("Authorization", &format!("Bearer {}", token))
        .call()
    {
        Ok(resp) => resp.into_string().map_err(|e| format!("Read error: {}", e)),
        Err(ureq::Error::Status(401, _)) => {
            let new_token =
                refresh_id_token().ok_or("Token expired and refresh failed. Run: beacon auth login")?;
            ureq::get(&url)
                .set("Authorization", &format!("Bearer {}", new_token))
                .call()
                .map_err(|e| format!("API error: {}", e))?
                .into_string()
                .map_err(|e| format!("Read error: {}", e))
        }
        Err(e) => Err(format!("API error: {}", e)),
    }
}

/// Unauthenticated GET — used by `cloud_preview_invitation` because the
/// landing endpoint is intentionally public (= invitee hasn't signed in yet).
/// We omit the Authorization header entirely rather than send a stale or
/// missing token; the server doesn't require it and adding a bad one would
/// only invite spurious 401s.
pub fn cloud_get_unauthed(path: &str) -> Result<String, String> {
    let url = full_url(path);
    match ureq::get(&url).call() {
        Ok(resp) => resp.into_string().map_err(|e| format!("Read error: {}", e)),
        Err(e) => Err(format!("API error: {}", e)),
    }
}

/// Authenticated POST with a JSON body. Mirrors the 401-retry policy.
pub fn cloud_post_json(path: &str, body: &Value) -> Result<String, String> {
    let token = load_auth_token().ok_or("Not authenticated. Run: beacon auth login")?;
    let url = full_url(path);
    let body_str = body.to_string();
    match ureq::post(&url)
        .set("Authorization", &format!("Bearer {}", token))
        .set("Content-Type", "application/json")
        .send_string(&body_str)
    {
        Ok(resp) => resp.into_string().map_err(|e| format!("Read error: {}", e)),
        Err(ureq::Error::Status(401, _)) => {
            let new_token =
                refresh_id_token().ok_or("Token expired and refresh failed. Run: beacon auth login")?;
            ureq::post(&url)
                .set("Authorization", &format!("Bearer {}", new_token))
                .set("Content-Type", "application/json")
                .send_string(&body_str)
                .map_err(|e| format!("API error: {}", e))?
                .into_string()
                .map_err(|e| format!("Read error: {}", e))
        }
        Err(ureq::Error::Status(code, resp)) => {
            // Surface the server's detail message verbatim so the user sees
            // "Only project owner can invite members" instead of just "400".
            let body = resp.into_string().unwrap_or_default();
            Err(format!("HTTP {}: {}", code, body))
        }
        Err(e) => Err(format!("API error: {}", e)),
    }
}

/// Authenticated PATCH with a JSON body.
pub fn cloud_patch_json(path: &str, body: &Value) -> Result<String, String> {
    let token = load_auth_token().ok_or("Not authenticated. Run: beacon auth login")?;
    let url = full_url(path);
    let body_str = body.to_string();
    let do_call = |tok: &str| {
        ureq::request("PATCH", &url)
            .set("Authorization", &format!("Bearer {}", tok))
            .set("Content-Type", "application/json")
            .send_string(&body_str)
    };
    match do_call(&token) {
        Ok(resp) => resp.into_string().map_err(|e| format!("Read error: {}", e)),
        Err(ureq::Error::Status(401, _)) => {
            let new_token =
                refresh_id_token().ok_or("Token expired and refresh failed. Run: beacon auth login")?;
            do_call(&new_token)
                .map_err(|e| format!("API error: {}", e))?
                .into_string()
                .map_err(|e| format!("Read error: {}", e))
        }
        Err(ureq::Error::Status(code, resp)) => {
            let body = resp.into_string().unwrap_or_default();
            Err(format!("HTTP {}: {}", code, body))
        }
        Err(e) => Err(format!("API error: {}", e)),
    }
}

/// Authenticated DELETE. No request body. Mirrors the 401-retry policy.
pub fn cloud_delete(path: &str) -> Result<String, String> {
    let token = load_auth_token().ok_or("Not authenticated. Run: beacon auth login")?;
    let url = full_url(path);
    let do_call = |tok: &str| {
        ureq::request("DELETE", &url)
            .set("Authorization", &format!("Bearer {}", tok))
            .call()
    };
    match do_call(&token) {
        Ok(resp) => resp.into_string().map_err(|e| format!("Read error: {}", e)),
        Err(ureq::Error::Status(401, _)) => {
            let new_token =
                refresh_id_token().ok_or("Token expired and refresh failed. Run: beacon auth login")?;
            do_call(&new_token)
                .map_err(|e| format!("API error: {}", e))?
                .into_string()
                .map_err(|e| format!("Read error: {}", e))
        }
        Err(ureq::Error::Status(code, resp)) => {
            let body = resp.into_string().unwrap_or_default();
            Err(format!("HTTP {}: {}", code, body))
        }
        Err(e) => Err(format!("API error: {}", e)),
    }
}
