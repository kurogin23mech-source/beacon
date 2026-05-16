use std::process::Command;
use std::sync::Mutex;
use tauri::{Manager, State};

struct AppState {
    project_dir: Mutex<Option<String>>,
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
    // Verify .beacon/project.json exists
    let project_file = std::path::Path::new(&dir).join(".beacon/project.json");
    if !project_file.exists() {
        return Err(format!("No beacon project found at {}", dir));
    }
    *state.project_dir.lock().unwrap() = Some(dir.clone());
    Ok(format!("Project set: {}", dir))
}

#[tauri::command]
fn open_project_dialog() -> Result<String, String> {
    // Return empty - frontend will use tauri dialog plugin
    Ok(String::new())
}

#[tauri::command]
fn load_project_json(state: State<AppState>) -> Result<String, String> {
    let dir = state.project_dir.lock().unwrap();
    let dir = dir.as_deref().ok_or("No project directory set")?;
    let path = std::path::Path::new(dir).join(".beacon/project.json");
    std::fs::read_to_string(&path)
        .map_err(|e| format!("Failed to read project.json: {}", e))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AppState {
            project_dir: Mutex::new(None),
        })
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            // Try to auto-detect project from CWD
            if let Ok(cwd) = std::env::current_dir() {
                let project_file = cwd.join(".beacon/project.json");
                if project_file.exists() {
                    let state = app.state::<AppState>();
                    *state.project_dir.lock().unwrap() = Some(cwd.to_string_lossy().to_string());
                }
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
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
