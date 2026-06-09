use std::sync::atomic::{AtomicBool, Ordering};

use futures_util::StreamExt;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tauri::{AppHandle, Emitter, State};
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::fs::File;
use tokio::io::AsyncWriteExt;

#[derive(Default)]
pub struct DownloadState {
    pub cancel_flag: AtomicBool,
}

#[derive(Clone, Serialize)]
struct DownloadProgress {
    downloaded: u64,
    total: u64,
    percentage: f64,
    status: String,
    speed_mbps: f64,
}

#[derive(Clone, Serialize)]
struct DownloadComplete {
    path: String,
    success: bool,
    error: Option<String>,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct DirectorActionStatus {
    status: String,
    message: String,
    action: String,
    details: Option<serde_json::Value>,
}

#[derive(Clone, Deserialize, Serialize)]
pub struct BoardTaskResponse {
    id: String,
    title: String,
    description: String,
    status: String,
    assigned_agent_id: Option<String>,
    result: Option<String>,
}

#[tauri::command]
pub async fn download_model(
    app: AppHandle,
    state: State<'_, DownloadState>,
    url: String,
    output_path: String,
) -> Result<String, String> {
    state.cancel_flag.store(false, Ordering::Relaxed);

    let client = reqwest::Client::new();
    let response = client
        .get(&url)
        .header("User-Agent", "lmWorld/0.1.0")
        .send()
        .await
        .map_err(|e| format!("Failed to start download: {}", e))?;

    let total_size = response
        .content_length()
        .ok_or("Failed to get content length")?;

    let mut file = File::create(&output_path)
        .await
        .map_err(|e| format!("Failed to create file: {}", e))?;

    let mut downloaded: u64 = 0;
    let mut stream = response.bytes_stream();
    let start_time = std::time::Instant::now();

    while let Some(chunk_result) = stream.next().await {
        if state.cancel_flag.load(Ordering::Relaxed) {
            let _ = tokio::fs::remove_file(&output_path).await;
            return Err("Download cancelled".to_string());
        }

        let chunk = chunk_result.map_err(|e| format!("Download error: {}", e))?;
        file.write_all(&chunk)
            .await
            .map_err(|e| format!("Write error: {}", e))?;

        downloaded += chunk.len() as u64;
        let elapsed = start_time.elapsed().as_secs_f64();
        let speed_mbps = if elapsed > 0.0 {
            (downloaded as f64 / elapsed) / 1_000_000.0
        } else {
            0.0
        };

        let progress = DownloadProgress {
            downloaded,
            total: total_size,
            percentage: (downloaded as f64 / total_size as f64) * 100.0,
            status: "downloading".to_string(),
            speed_mbps,
        };

        let _ = app.emit("download-progress", progress);
    }

    file.flush().await.map_err(|e| format!("Flush error: {}", e))?;

    let complete = DownloadComplete {
        path: output_path.clone(),
        success: true,
        error: None,
    };
    let _ = app.emit("download-complete", complete);

    Ok(output_path)
}

#[tauri::command]
pub fn cancel_download(state: State<'_, DownloadState>) -> Result<(), String> {
    state.cancel_flag.store(true, Ordering::Relaxed);
    Ok(())
}

#[tauri::command]
pub async fn get_models_dir() -> Result<String, String> {
    let home = dirs::home_dir().ok_or("Failed to get home directory")?;
    let models_dir = home.join(".lmworld").join("models");
    tokio::fs::create_dir_all(&models_dir)
        .await
        .map_err(|e| format!("Failed to create models directory: {}", e))?;
    Ok(models_dir.to_string_lossy().to_string())
}

#[tauri::command]
pub async fn list_downloaded_models() -> Result<Vec<String>, String> {
    let home = dirs::home_dir().ok_or("Failed to get home directory")?;
    let models_dir = home.join(".lmworld").join("models");

    let mut models = Vec::new();
    if models_dir.exists() {
        let mut entries = tokio::fs::read_dir(&models_dir)
            .await
            .map_err(|e| format!("Failed to read directory: {}", e))?;

        while let Some(entry) = entries
            .next_entry()
            .await
            .map_err(|e| format!("Failed to read entry: {}", e))?
        {
            let path = entry.path();
            if path.is_file() {
                if let Some(name) = path.file_name() {
                    models.push(name.to_string_lossy().to_string());
                }
            }
        }
    }

    Ok(models)
}

#[tauri::command]
pub async fn add_task_to_db(
    title: String,
    description: Option<String>,
    status: Option<String>,
    assigned_agent_id: Option<String>,
) -> Result<BoardTaskResponse, String> {
    let title = title.trim();
    if title.is_empty() {
        return Err("Task title cannot be empty".to_string());
    }

    let client = reqwest::Client::new();
    let description = description.unwrap_or_default();
    let create_response = create_task_with_idempotent_title(&client, title, &description, assigned_agent_id.clone()).await?;

    let mut task = create_response
        .json::<BoardTaskResponse>()
        .await
        .map_err(|error| format!("Invalid Board response: {error}"))?;

    let requested_status = status.unwrap_or_else(|| "todo".to_string());
    if requested_status != "todo" {
        let update_response = client
            .put(format!("http://127.0.0.1:8001/api/tasks/{}", task.id))
            .json(&json!({ "status": requested_status }))
            .send()
            .await
            .map_err(|error| format!("Task created but status update failed: {error}"))?;

        if !update_response.status().is_success() {
            let code = update_response.status();
            let body = update_response.text().await.unwrap_or_default();
            return Err(format!("Task created but status update failed ({code}): {body}"));
        }

        task = update_response
            .json::<BoardTaskResponse>()
            .await
            .map_err(|error| format!("Invalid Board update response: {error}"))?;
    }

    Ok(task)
}

async fn create_task_with_idempotent_title(
    client: &reqwest::Client,
    title: &str,
    description: &str,
    assigned_agent_id: Option<String>,
) -> Result<reqwest::Response, String> {
    let first = post_task(client, title, description, assigned_agent_id.clone()).await?;
    if first.status().is_success() {
        return Ok(first);
    }

    let code = first.status();
    let body = first.text().await.unwrap_or_default();
    let duplicate_like = body.to_lowercase().contains("duplicate")
        || body.to_lowercase().contains("unique")
        || body.to_lowercase().contains("already exists")
        || code.as_u16() == 409;
    if !duplicate_like {
        return Err(format!("Board task creation failed ({code}): {body}"));
    }

    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0);
    let suffix = format!(" [{millis}]");
    let max_base_len = 200usize.saturating_sub(suffix.len());
    let retry_title = format!("{}{}", title.chars().take(max_base_len).collect::<String>(), suffix);
    let retry = post_task(client, &retry_title, description, assigned_agent_id).await?;
    if retry.status().is_success() {
        return Ok(retry);
    }

    let retry_code = retry.status();
    let retry_body = retry.text().await.unwrap_or_default();
    Err(format!(
        "Board task creation failed after idempotent duplicate-title retry ({retry_code}): {retry_body}; original={code} {body}"
    ))
}

async fn post_task(
    client: &reqwest::Client,
    title: &str,
    description: &str,
    assigned_agent_id: Option<String>,
) -> Result<reqwest::Response, String> {
    client
        .post("http://127.0.0.1:8001/api/tasks")
        .json(&json!({
            "title": title,
            "description": description,
            "assigned_agent_id": assigned_agent_id,
        }))
        .send()
        .await
        .map_err(|error| format!("Board backend is unreachable: {error}"))
}

#[tauri::command]
pub async fn rust_add_task_to_db(
    title: String,
    description: Option<String>,
    status: Option<String>,
    assigned_agent_id: Option<String>,
) -> Result<BoardTaskResponse, String> {
    add_task_to_db(title, description, status, assigned_agent_id).await
}

#[tauri::command]
pub fn director_action_status(app: AppHandle, status: DirectorActionStatus) -> Result<(), String> {
    let level = if status.status == "success" { "info" } else { "error" };
    log::log!(
        if level == "info" { log::Level::Info } else { log::Level::Error },
        "Director action {}: {} ({})",
        status.action,
        status.message,
        status.status
    );
    app.emit("director-action-status", status)
        .map_err(|error| format!("Failed to emit Director action status: {error}"))
}
