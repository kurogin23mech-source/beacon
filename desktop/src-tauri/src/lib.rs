use std::process::Command;
use std::sync::Mutex;
use serde::Serialize;
use tauri::{Emitter, Manager, State};
use notify::{RecommendedWatcher, RecursiveMode, Watcher};

struct AppState {
    project_dir: Mutex<Option<String>>,
    cloud_project_id: Mutex<Option<String>>,
    watcher: Mutex<Option<RecommendedWatcher>>,
}

fn start_file_watcher(app_handle: tauri::AppHandle, project_dir: &str) -> Option<RecommendedWatcher> {
    let beacon_dir = std::path::Path::new(project_dir).join(".beacon");
    if !beacon_dir.exists() {
        return None;
    }

    let (tx, rx) = std::sync::mpsc::channel::<()>();

    let mut watcher = notify::recommended_watcher(move |res: notify::Result<notify::Event>| {
        if let Ok(event) = res {
            use notify::EventKind;
            match event.kind {
                EventKind::Modify(_) | EventKind::Create(_) | EventKind::Remove(_) => {
                    let _ = tx.send(());
                }
                _ => {}
            }
        }
    }).ok()?;

    watcher.watch(&beacon_dir, RecursiveMode::NonRecursive).ok()?;

    std::thread::spawn(move || {
        while rx.recv().is_ok() {
            std::thread::sleep(std::time::Duration::from_millis(150));
            while rx.try_recv().is_ok() {}
            let _ = app_handle.emit("beacon-changed", ());
        }
    });

    Some(watcher)
}

const DEFAULT_API_URL: &str = "https://beacon-ai.dev";

/// Find a beacon project by checking multiple sources
fn find_project_dir() -> Option<String> {
    // 1. CLI argument: beacon-desktop /path/to/project
    let args: Vec<String> = std::env::args().collect();
    if args.len() > 1 {
        let candidate = &args[1];
        let p = std::path::Path::new(candidate);
        if p.join(".beacon/project.json").exists() {
            return Some(candidate.clone());
        }
        // Maybe they passed the .beacon dir itself
        if p.join("project.json").exists() {
            if let Some(parent) = p.parent() {
                return Some(parent.to_string_lossy().to_string());
            }
        }
    }

    // 2. Check env var
    if let Ok(dir) = std::env::var("BEACON_PROJECT_DIR") {
        let p = std::path::Path::new(&dir).join(".beacon/project.json");
        if p.exists() {
            return Some(dir);
        }
    }

    // 3. Walk up from CWD
    if let Ok(cwd) = std::env::current_dir() {
        let mut dir = cwd.as_path();
        loop {
            if dir.join(".beacon/project.json").exists() {
                return Some(dir.to_string_lossy().to_string());
            }
            match dir.parent() {
                Some(parent) => dir = parent,
                None => break,
            }
        }
    }

    None
}

#[derive(Serialize, Clone)]
struct ProjectInfo {
    path: String,
    name: String,
    mode: String,
}

/// Scan home directory for beacon projects (max depth 3)
fn scan_beacon_projects() -> Vec<ProjectInfo> {
    let mut results = Vec::new();
    let home = match std::env::var("HOME") {
        Ok(h) => h,
        Err(_) => return results,
    };
    let home_path = std::path::Path::new(&home);

    // Scan direct children and tools/ subdirectory
    let search_dirs: Vec<std::path::PathBuf> = {
        let mut dirs = Vec::new();
        if let Ok(entries) = std::fs::read_dir(home_path) {
            for entry in entries.flatten() {
                let p = entry.path();
                if p.is_dir() {
                    dirs.push(p.clone());
                    // Also check one level deeper (e.g. ~/tools/beacon)
                    if let Ok(sub) = std::fs::read_dir(&p) {
                        for s in sub.flatten() {
                            if s.path().is_dir() {
                                dirs.push(s.path());
                            }
                        }
                    }
                }
            }
        }
        dirs
    };

    for dir in search_dirs {
        let project_file = dir.join(".beacon/project.json");
        if project_file.exists() {
            let name = std::fs::read_to_string(&project_file)
                .ok()
                .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
                .and_then(|v| v.get("name")?.as_str().map(String::from))
                .unwrap_or_else(|| dir.file_name().unwrap_or_default().to_string_lossy().to_string());

            let config_file = dir.join(".beacon/config.json");
            let mode = std::fs::read_to_string(&config_file)
                .ok()
                .and_then(|s| serde_json::from_str::<serde_json::Value>(&s).ok())
                .and_then(|v| v.get("mode")?.as_str().map(String::from))
                .unwrap_or_else(|| "local".to_string());

            results.push(ProjectInfo {
                path: dir.to_string_lossy().to_string(),
                name,
                mode,
            });
        }
    }

    results.sort_by(|a, b| a.name.cmp(&b.name));
    results
}

#[tauri::command]
fn list_projects() -> Vec<ProjectInfo> {
    scan_beacon_projects()
}

/// Load auth token from ~/.beacon/credentials.json
/// HOME is used on macOS/Linux; USERPROFILE is used on Windows.
fn load_auth_token() -> Option<String> {
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .ok()?;
    let path = std::path::Path::new(&home).join(".beacon/credentials.json");
    let content = std::fs::read_to_string(&path).ok()?;
    let data: serde_json::Value = serde_json::from_str(&content).ok()?;
    // Prefer id_token (needed for API auth), fall back to token
    data.get("id_token")
        .or_else(|| data.get("token"))
        .and_then(|v| v.as_str())
        .map(String::from)
}

/// Refresh the id_token using the refresh_token from credentials.json.
/// Updates credentials.json in-place on success and returns the new id_token.
fn refresh_id_token() -> Option<String> {
    let home = std::env::var("HOME").ok()?;
    let creds_path = std::path::Path::new(&home).join(".beacon/credentials.json");
    let content = std::fs::read_to_string(&creds_path).ok()?;
    let mut data: serde_json::Value = serde_json::from_str(&content).ok()?;

    let refresh_token = data.get("refresh_token")?.as_str()?.to_string();
    let client_id = data.get("client_id")?.as_str()?.to_string();
    let client_secret = data.get("client_secret")?.as_str()?.to_string();
    let token_uri = data.get("token_uri")
        .and_then(|v| v.as_str())
        .unwrap_or("https://oauth2.googleapis.com/token")
        .to_string();

    let body = format!(
        "grant_type=refresh_token&refresh_token={}&client_id={}&client_secret={}",
        refresh_token, client_id, client_secret
    );

    let resp_json: serde_json::Value = ureq::post(&token_uri)
        .set("Content-Type", "application/x-www-form-urlencoded")
        .send_string(&body)
        .ok()?
        .into_json()
        .ok()?;

    let new_id_token = resp_json.get("id_token")?.as_str()?.to_string();
    if let Some(access_token) = resp_json.get("access_token").and_then(|v| v.as_str()) {
        data["token"] = serde_json::Value::String(access_token.to_string());
    }
    data["id_token"] = serde_json::Value::String(new_id_token.clone());

    if let Ok(updated) = serde_json::to_string_pretty(&data) {
        let _ = std::fs::write(&creds_path, updated);
    }

    Some(new_id_token)
}

/// Make an authenticated GET request to the Cloud API.
/// Always reads a fresh token from disk. On 401, auto-refreshes via refresh_token and retries once.
fn cloud_get(path: &str) -> Result<String, String> {
    let token = load_auth_token()
        .ok_or("Not authenticated. Run: beacon auth login")?;
    let url = format!("{}{}", DEFAULT_API_URL, path);

    match ureq::get(&url).set("Authorization", &format!("Bearer {}", token)).call() {
        Ok(resp) => resp.into_string().map_err(|e| format!("Read error: {}", e)),
        Err(ureq::Error::Status(401, _)) => {
            let new_token = refresh_id_token()
                .ok_or("Token expired and refresh failed. Run: beacon auth login")?;
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

#[tauri::command]
fn cloud_list_projects(_state: State<AppState>) -> Result<String, String> {
    cloud_get("/api/projects")
}

#[tauri::command]
fn cloud_load_project(state: State<AppState>, project_id: String) -> Result<String, String> {
    *state.cloud_project_id.lock().unwrap() = Some(project_id.clone());
    cloud_get(&format!("/api/projects/{}", project_id))
}

#[tauri::command]
fn cloud_list_documents(state: State<AppState>) -> Result<String, String> {
    let pid = state.cloud_project_id.lock().unwrap().clone().ok_or("No cloud project selected")?;
    cloud_get(&format!("/api/projects/{}/documents", pid))
}

#[tauri::command]
fn cloud_get_document(state: State<AppState>, doc_id: String) -> Result<String, String> {
    let pid = state.cloud_project_id.lock().unwrap().clone().ok_or("No cloud project selected")?;
    cloud_get(&format!("/api/projects/{}/documents/{}", pid, doc_id))
}

#[tauri::command]
fn cloud_list_retros(state: State<AppState>) -> Result<String, String> {
    let pid = state.cloud_project_id.lock().unwrap().clone().ok_or("No cloud project selected")?;
    cloud_get(&format!("/api/projects/{}/retros", pid))
}

#[tauri::command]
fn cloud_get_retro(state: State<AppState>, week: String) -> Result<String, String> {
    let pid = state.cloud_project_id.lock().unwrap().clone().ok_or("No cloud project selected")?;
    cloud_get(&format!("/api/projects/{}/retros/{}", pid, week))
}

fn cloud_post(path: &str) -> Result<String, String> {
    let token = load_auth_token()
        .ok_or("Not authenticated. Run: beacon auth login")?;
    let url = format!("{}{}", DEFAULT_API_URL, path);
    match ureq::post(&url).set("Authorization", &format!("Bearer {}", token)).set("Content-Length", "0").call() {
        Ok(resp) => resp.into_string().map_err(|e| format!("Read error: {}", e)),
        Err(ureq::Error::Status(401, _)) => {
            let new_token = refresh_id_token()
                .ok_or("Token expired and refresh failed. Run: beacon auth login")?;
            ureq::post(&url)
                .set("Authorization", &format!("Bearer {}", new_token))
                .set("Content-Length", "0")
                .call()
                .map_err(|e| format!("API error: {}", e))?
                .into_string()
                .map_err(|e| format!("Read error: {}", e))
        }
        Err(e) => Err(format!("API error: {}", e)),
    }
}

#[tauri::command]
fn cloud_archive_project(state: State<AppState>) -> Result<String, String> {
    let pid = state.cloud_project_id.lock().unwrap().clone().ok_or("No cloud project selected")?;
    cloud_post(&format!("/api/projects/{}/archive", pid))
}

#[tauri::command]
fn cloud_unarchive_project(state: State<AppState>) -> Result<String, String> {
    let pid = state.cloud_project_id.lock().unwrap().clone().ok_or("No cloud project selected")?;
    cloud_post(&format!("/api/projects/{}/unarchive", pid))
}

#[tauri::command]
fn is_authenticated(_state: State<AppState>) -> bool {
    load_auth_token().is_some()
}

/// Return the current id_token for live-update WebSocket auth. Caller embeds
/// it in the `?token=` query param to /ws/projects/{id}. We do NOT pre-refresh
/// here — refresh only triggers on a 401-equivalent (server WS close 4403), at
/// which point JS calls `cloud_refresh_auth_token` and reconnects. Mirrors the
/// HTTP path in cloud_get(), keeping the live cycle deterministic instead of
/// hidden behind a silent always-refresh.
#[tauri::command]
fn cloud_get_auth_token() -> Result<String, String> {
    load_auth_token().ok_or_else(|| "Not authenticated. Run: beacon auth login".to_string())
}

/// Force a token refresh and return the new id_token. Called by JS when the
/// WebSocket is closed with code 4403 (token expired). Returns Err if refresh
/// fails (user must sign in again).
#[tauri::command]
fn cloud_refresh_auth_token() -> Result<String, String> {
    refresh_id_token().ok_or_else(|| "Token expired and refresh failed. Run: beacon auth login".to_string())
}

/// Run a beacon CLI command and return its JSON output
fn run_beacon(project_dir: &str, args: &[&str]) -> Result<String, String> {
    let output = Command::new("beacon")
        .args(args)
        .current_dir(project_dir)
        .env("BEACON_JSON", "1")
        .output()
        .map_err(|e| format!("Failed to run beacon: {}", e))?;

    if output.status.success() {
        Ok(String::from_utf8_lossy(&output.stdout).to_string())
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr);
        Err(format!("beacon error: {}", stderr))
    }
}

#[tauri::command]
fn get_status(state: State<AppState>) -> Result<String, String> {
    let dir = state.project_dir.lock().unwrap();
    let dir = dir.as_deref().ok_or("No project directory set")?;
    run_beacon(dir, &["status", "--json"])
}

#[tauri::command]
fn get_task_list(state: State<AppState>, ms_id: String) -> Result<String, String> {
    let dir = state.project_dir.lock().unwrap();
    let dir = dir.as_deref().ok_or("No project directory set")?;
    run_beacon(dir, &["task", "list", "--json", "--ms", &ms_id])
}

#[tauri::command]
fn get_milestone_graph(state: State<AppState>) -> Result<String, String> {
    let dir = state.project_dir.lock().unwrap();
    let dir = dir.as_deref().ok_or("No project directory set")?;
    run_beacon(dir, &["milestone", "graph", "--json"])
}

#[tauri::command]
fn get_documents(state: State<AppState>, scope: String) -> Result<String, String> {
    let dir = state.project_dir.lock().unwrap();
    let dir = dir.as_deref().ok_or("No project directory set")?;
    let mut args = vec!["doc", "list", "--json"];
    if !scope.is_empty() {
        args.push("--scope");
        args.push(&scope);
    }
    run_beacon(dir, &args)
}

#[tauri::command]
fn get_document_content(state: State<AppState>, doc_id: String) -> Result<String, String> {
    let dir = state.project_dir.lock().unwrap();
    let dir = dir.as_deref().ok_or("No project directory set")?;
    run_beacon(dir, &["doc", "show", &doc_id])
}

#[tauri::command]
fn set_project_dir(app: tauri::AppHandle, state: State<AppState>, dir: String) -> Result<String, String> {
    let project_file = std::path::Path::new(&dir).join(".beacon/project.json");
    if !project_file.exists() {
        return Err(format!("No beacon project found at {}", dir));
    }
    *state.project_dir.lock().unwrap() = Some(dir.clone());
    let w = start_file_watcher(app, &dir);
    *state.watcher.lock().unwrap() = w;
    Ok(format!("Project set: {}", dir))
}

#[tauri::command]
fn open_project_dialog() -> Result<String, String> {
    Ok(String::new())
}

#[tauri::command]
fn list_retros(state: State<AppState>) -> Result<String, String> {
    let dir = state.project_dir.lock().unwrap();
    let dir = dir.as_deref().ok_or("No project directory set")?;
    let retro_dir = std::path::Path::new(dir).join(".beacon/retro");
    let mut retros: Vec<serde_json::Value> = Vec::new();
    if retro_dir.is_dir() {
        if let Ok(entries) = std::fs::read_dir(&retro_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().map_or(false, |e| e == "md") {
                    let fname = path.file_stem().unwrap_or_default().to_string_lossy().to_string();
                    let modified = std::fs::metadata(&path).ok()
                        .and_then(|m| m.modified().ok())
                        .map(|t| {
                            let d = t.duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
                            chrono_format(d.as_secs())
                        })
                        .unwrap_or_default();
                    retros.push(serde_json::json!({"week": fname, "updated_at": modified}));
                }
            }
        }
    }
    retros.sort_by(|a, b| b["week"].as_str().unwrap_or("").cmp(a["week"].as_str().unwrap_or("")));
    serde_json::to_string(&retros).map_err(|e| e.to_string())
}

fn chrono_format(secs: u64) -> String {
    let days_since_epoch = (secs / 86400) as i64;
    // Calculate date from days since 1970-01-01
    let mut y = 1970i64;
    let mut remaining = days_since_epoch;
    loop {
        let days_in_year = if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) { 366 } else { 365 };
        if remaining < days_in_year { break; }
        remaining -= days_in_year;
        y += 1;
    }
    let months: &[i64] = if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) {
        &[31,29,31,30,31,30,31,31,30,31,30,31]
    } else {
        &[31,28,31,30,31,30,31,31,30,31,30,31]
    };
    let mut m = 1;
    for &days_in_month in months {
        if remaining < days_in_month { break; }
        remaining -= days_in_month;
        m += 1;
    }
    let d = remaining + 1;
    format!("{:04}-{:02}-{:02}", y, m, d)
}

#[tauri::command]
fn get_retro_content(state: State<AppState>, week: String) -> Result<String, String> {
    let dir = state.project_dir.lock().unwrap();
    let dir = dir.as_deref().ok_or("No project directory set")?;
    let path = std::path::Path::new(dir).join(".beacon/retro").join(format!("{}.md", week));
    let content = std::fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read retro: {}", e))?;
    let result = serde_json::json!({"week": week, "content": content});
    serde_json::to_string(&result).map_err(|e| e.to_string())
}

#[tauri::command]
fn load_project_json(state: State<AppState>) -> Result<String, String> {
    let dir = state.project_dir.lock().unwrap();
    let dir = dir.as_deref().ok_or("No project directory set. Launch from a beacon project directory or set BEACON_PROJECT_DIR.")?;
    let path = std::path::Path::new(dir).join(".beacon/project.json");
    std::fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read project.json: {}", e))
}

#[tauri::command]
fn cloud_diagnose() -> String {
    let mut lines = vec![];

    // 1. HOME env
    let home = std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .unwrap_or_else(|_| "(not set)".into());
    lines.push(format!("HOME/USERPROFILE: {}", home));

    // 2. credentials.json
    let creds_path = std::path::Path::new(&home).join(".beacon/credentials.json");
    lines.push(format!("credentials path: {}", creds_path.display()));
    lines.push(format!("credentials exists: {}", creds_path.exists()));

    if creds_path.exists() {
        match std::fs::read_to_string(&creds_path) {
            Ok(content) => {
                let has_id_token = content.contains("\"id_token\"");
                let has_refresh = content.contains("\"refresh_token\"");
                lines.push(format!("has id_token: {}", has_id_token));
                lines.push(format!("has refresh_token: {}", has_refresh));
            }
            Err(e) => lines.push(format!("read error: {}", e)),
        }
    }

    // 3. API call
    match load_auth_token() {
        None => lines.push("token: None (not authenticated)".into()),
        Some(token) => {
            lines.push(format!("token: {}...", &token[..token.len().min(20)]));
            let url = format!("{}/api/projects", DEFAULT_API_URL);
            match ureq::get(&url).set("Authorization", &format!("Bearer {}", token)).call() {
                Ok(resp) => match resp.into_string() {
                    Ok(body) => lines.push(format!("API OK: {}", &body[..body.len().min(200)])),
                    Err(e) => lines.push(format!("API read error: {}", e)),
                },
                Err(e) => lines.push(format!("API call error: {}", e)),
            }
        }
    }

    lines.join("\n")
}

/// Find a project dir in a second-instance args vector and switch to it.
/// Called by tauri-plugin-single-instance when another `open -a Beacon --args /path`
/// (or `beacon-desktop /path`) is invoked while an instance is already running.
/// This eliminates the `pkill -x Beacon` warm-start workaround in /beacon-init.
fn switch_project_from_args(app: &tauri::AppHandle, args: &[String]) {
    // Look through args (skipping argv[0]) for the first path containing .beacon/project.json
    let candidate = args.iter().skip(1).find_map(|a| {
        let p = std::path::Path::new(a);
        if p.join(".beacon/project.json").exists() {
            Some(a.clone())
        } else if p.join("project.json").exists() {
            p.parent().map(|x| x.to_string_lossy().to_string())
        } else {
            None
        }
    });
    if let Some(dir) = candidate {
        let state: tauri::State<AppState> = app.state();
        *state.project_dir.lock().unwrap() = Some(dir.clone());
        let w = start_file_watcher(app.clone(), &dir);
        *state.watcher.lock().unwrap() = w;
        // Notify frontend so it can clear state and reload via load_project_json
        let _ = app.emit("project-changed", &dir);
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        // F27: single-instance — when a second launch happens (e.g. /beacon-init
        // re-running `open -a Beacon --args /new/path`), forward args to the
        // running instance instead of starting a new process. Replaces the
        // pkill warm-start workaround.
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            log::info!("single-instance fired, args: {:?}", args);
            switch_project_from_args(app, &args);
            // Bring window to front so user sees the switch
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .manage(AppState {
            project_dir: Mutex::new(find_project_dir()),
            cloud_project_id: Mutex::new(None),
            watcher: Mutex::new(None),
        })
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            let state: tauri::State<AppState> = app.state();
            if let Some(dir) = state.project_dir.lock().unwrap().clone() {
                let w = start_file_watcher(app.handle().clone(), &dir);
                *state.watcher.lock().unwrap() = w;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_status,
            get_task_list,
            get_milestone_graph,
            get_documents,
            get_document_content,
            set_project_dir,
            open_project_dialog,
            load_project_json,
            list_projects,
            list_retros,
            get_retro_content,
            is_authenticated,
            cloud_list_projects,
            cloud_load_project,
            cloud_list_documents,
            cloud_get_document,
            cloud_list_retros,
            cloud_get_retro,
            cloud_diagnose,
            cloud_archive_project,
            cloud_unarchive_project,
            cloud_get_auth_token,
            cloud_refresh_auth_token,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
