"use client";

import { useState } from "react";
import { Download, X } from "lucide-react";
import { downloadModel, cancelDownload, getModelsDir } from "@/lib/tauri";

interface ModelDownloadProps {
  modelId: string;
  onComplete?: () => void;
}

export default function ModelDownload({ modelId, onComplete }: ModelDownloadProps) {
  const [downloading, setDownloading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [speed, setSpeed] = useState(0);
  const [error, setError] = useState<string>("");

  const handleDownload = async () => {
    setDownloading(true);
    setProgress(0);
    setError("");

    try {
      const modelsDir = await getModelsDir();
      const filename = modelId.replace(/\//g, "_") + ".gguf";
      const outputPath = `${modelsDir}/${filename}`;
      
      // Construct HuggingFace download URL
      const url = `https://huggingface.co/${modelId}/resolve/main/model.gguf`;
      
      await downloadModel(url, outputPath, (p) => {
        setProgress(p.percentage);
        setSpeed(p.speed_mbps);
      });

      onComplete?.();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloading(false);
    }
  };

  const handleCancel = () => {
    cancelDownload();
    setDownloading(false);
    setProgress(0);
  };

  return (
    <div className="space-y-3">
      {error && (
        <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-3 text-red-400 text-sm">
          {error}
        </div>
      )}

      {downloading ? (
        <div className="space-y-2">
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-300">Downloading...</span>
            <span className="text-blue-400">{progress.toFixed(1)}%</span>
          </div>
          <div className="w-full bg-gray-800 rounded-full h-2">
            <div
              className="bg-blue-500 h-2 rounded-full transition-all duration-300"
              style={{ width: `${progress}%` }}
            />
          </div>
          <div className="flex items-center justify-between text-xs text-gray-500">
            <span>{speed.toFixed(2)} MB/s</span>
            <button
              onClick={handleCancel}
              className="text-red-400 hover:text-red-300 flex items-center gap-1"
            >
              <X size={14} />
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          onClick={handleDownload}
          className="w-full bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-xl flex items-center justify-center gap-2 transition-colors"
        >
          <Download size={18} />
          Download Model
        </button>
      )}
    </div>
  );
}
