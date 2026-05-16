use std::process::Command;
use std::sync::Mutex;
use serde::Serialize;
use tauri::{Manager, State};

struct AppState {
    project_dir: Mutex<Option<String>>,
    cloud_token: Mutex<Option<String>>,
    cloud_project_id: Mutex<Option<String>>,
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
fn load_auth_token() -> Option<String> {
    let home = std::env::var("HOME").ok()?;
    let path = std::path::Path::new(&home).join(".beacon/credentials.json");
    let content = std::fs::read_to_string(&path).ok()?;
    let data: serde_json::Value = serde_json::from_str(&content).ok()?;
    // Prefer id_token (needed for API auth), fall back to token
    data.get("id_token")
        .or_else(|| data.get("token"))
        .and_then(|v| v.as_str())
        .map(String::from)
}

/// Make an authenticated GET request to the Cloud API
fn cloud_get(path: &str, token: &str) -> Result<String, String> {
    let url = format!("{}{}", DEFAULT_API_URL, path);
    let resp = ureq::get(&url)
        .set("Authorization", &format!("Bearer {}", token))
        .call()
        .map_err(|e| format!("API error: {}", e))?;
    resp.into_string().map_err(|e| format!("Read error: {}", e))
}

#[tauri::command]
fn cloud_list_projects(state: State<AppState>) -> Result<String, String> {
    let token = state.cloud_token.lock().unwrap();
    let token = token.as_deref().ok_or("Not authenticated. Run: beacon auth login")?;
    cloud_get("/api/projects", token)
}

#[tauri::command]
fn cloud_load_project(state: State<AppState>, project_id: String) -> Result<String, String> {
    let token = state.cloud_token.lock().unwrap();
    let token = token.as_deref().ok_or("Not authenticated")?;
    *state.cloud_project_id.lock().unwrap() = Some(project_id.clone());
    cloud_get(&format!("/api/projects/{}", project_id), token)
}

#[tauri::command]
fn cloud_list_documents(state: State<AppState>) -> Result<String, String> {
    let token = state.cloud_token.lock().unwrap();
    let token = token.as_deref().ok_or("Not authenticated")?;
    let pid = state.cloud_project_id.lock().unwrap();
    let pid = pid.as_deref().ok_or("No cloud project selected")?;
    cloud_get(&format!("/api/projects/{}/documents", pid), token)
}

#[tauri::command]
fn cloud_get_document(state: State<AppState>, doc_id: String) -> Result<String, String> {
    let token = state.cloud_token.lock().unwrap();
    let token = token.as_deref().ok_or("Not authenticated")?;
    let pid = state.cloud_project_id.lock().unwrap();
    let pid = pid.as_deref().ok_or("No cloud project selected")?;
    cloud_get(&format!("/api/projects/{}/documents/{}", pid, doc_id), token)
}

#[tauri::command]
fn cloud_list_retros(state: State<AppState>) -> Result<String, String> {
    let token = state.cloud_token.lock().unwrap();
    let token = token.as_deref().ok_or("Not authenticated")?;
    let pid = state.cloud_project_id.lock().unwrap();
    let pid = pid.as_deref().ok_or("No cloud project selected")?;
    cloud_get(&format!("/api/projects/{}/retros", pid), token)
}

#[tauri::command]
fn cloud_get_retro(state: State<AppState>, week: String) -> Result<String, String> {
    let token = state.cloud_token.lock().unwrap();
    let token = token.as_deref().ok_or("Not authenticated")?;
    let pid = state.cloud_project_id.lock().unwrap();
    let pid = pid.as_deref().ok_or("No cloud project selected")?;
    cloud_get(&format!("/api/projects/{}/retros/{}", pid, week), token)
}

#[tauri::command]
fn is_authenticated(state: State<AppState>) -> bool {
    state.cloud_token.lock().unwrap().is_some()
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
fn set_project_dir(state: State<AppState>, dir: String) -> Result<String, String> {
    let project_file = std::path::Path::new(&dir).join(".beacon/project.json");
    if !project_file.exists() {
        return Err(format!("No beacon project found at {}", dir));
    }
    *state.project_dir.lock().unwrap() = Some(dir.clone());
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AppState {
            project_dir: Mutex::new(find_project_dir()),
            cloud_token: Mutex::new(load_auth_token()),
            cloud_project_id: Mutex::new(None),
        })
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
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
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
