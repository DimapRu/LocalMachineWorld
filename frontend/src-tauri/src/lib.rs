mod commands;

use commands::{
  add_task_to_db, cancel_download, director_action_status, download_model, get_models_dir,
  list_downloaded_models, rust_add_task_to_db, DownloadState,
};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
  tauri::Builder::default()
    .plugin(tauri_plugin_shell::init())
    .plugin(tauri_plugin_fs::init())
    .plugin(tauri_plugin_dialog::init())
    .plugin(tauri_plugin_http::init())
    .manage(DownloadState::default())
    .invoke_handler(tauri::generate_handler![
      download_model,
      cancel_download,
      get_models_dir,
      list_downloaded_models,
      add_task_to_db,
      rust_add_task_to_db,
      director_action_status,
    ])
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
    .run(tauri::generate_context!())
    .expect("error while running tauri application");
}
