import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export interface DownloadProgress {
  downloaded: number;
  total: number;
  percentage: number;
  status: string;
  speed_mbps: number;
}

export interface DownloadComplete {
  path: string;
  success: boolean;
  error?: string;
}

export async function downloadModel(
  url: string,
  outputPath: string,
  onProgress?: (progress: DownloadProgress) => void
): Promise<string> {
  const unlisten = await listen<DownloadProgress>("download-progress", (event) => {
    onProgress?.(event.payload);
  });

  try {
    const result = await invoke<string>("download_model", {
      url,
      outputPath,
    });
    return result;
  } finally {
    unlisten();
  }
}

export async function cancelDownload(): Promise<void> {
  await invoke("cancel_download");
}

export async function getModelsDir(): Promise<string> {
  return await invoke("get_models_dir");
}

export async function listDownloadedModels(): Promise<string[]> {
  return await invoke("list_downloaded_models");
}
