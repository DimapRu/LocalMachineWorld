"use client";

import { useEffect, useRef, useState } from "react";
import {
  Cpu,
  HardDrive,
  MemoryStick,
  Check,
  Loader2,
  Key,
  Folder,
  Wrench,
  Search,
  UserPlus,
  AlertTriangle,
} from "lucide-react";

interface HFModel {
  id: string;
  name: string;
  size_gb: Record<string, number>;
  tags: string[];
  supports_tools?: boolean;
}

interface FilterOptions {
  hardware: {
    gpu: {
      available: boolean;
      total_vram_gb: number;
      used_vram_gb: number;
      free_vram_gb: number;
    };
    cpu: {
      total_ram_gb: number;
      used_ram_gb: number;
      free_ram_gb: number;
    };
  };
  suggestions: Record<string, string[]>;
}

interface HFAuthStatus {
  authenticated: boolean;
  models_dir: string;
}

interface MCPTool {
  id: string;
  name: string;
  description: string;
  category: string;
  enabled: boolean;
}

function estimateSizes(modelId: string): Record<string, number> {
  const params = parseParams(modelId);
  return {
    fp32: +(params * 4).toFixed(1),
    fp16: +(params * 2).toFixed(1),
    int8: +(params * 1).toFixed(1),
    int4: +(params * 0.5).toFixed(1),
  };
}

function parseParams(id: string): number {
  const lower = id.toLowerCase();
  const m = lower.match(/(\d+\.?\d*)\s?b/i);
  if (m) {
    return parseFloat(m[1]);
  }
  const mm = lower.match(/(\d+\.?\d*)\s?m/i);
  if (mm) {
    return parseFloat(mm[1]) / 1000;
  }
  if (lower.includes("tinyllama")) return 1.1;
  if (lower.includes("llama3.2")) return 3.0;
  if (lower.includes("llama3.1") || lower.includes("llama3")) return 8.0;
  if (lower.includes("mistral") || lower.includes("mixtral")) return 7.0;
  if (lower.includes("phi3")) return 3.8;
  if (lower.includes("phi4")) return 14.0;
  if (lower.includes("gemma2")) return 9.0;
  if (lower.includes("gemma")) return 2.0;
  if (lower.includes("qwen2.5")) return 7.0;
  if (lower.includes("qwen")) return 7.0;
  if (lower.includes("codellama")) return 7.0;
  if (lower.includes("deepseek")) return 7.0;
  return 7.0;
}

function buildRecommendedModels(options: FilterOptions): HFModel[] {
  console.log("[buildRecommendedModels] called with options:", JSON.stringify(options, null, 2));
  if (!options) {
    console.log("[buildRecommendedModels] early return: !options");
    return [];
  }
  const hardware = options.hardware || {};
  const gpu = hardware.gpu || { available: false, total_vram_gb: 0 };
  const cpu = hardware.cpu || { total_ram_gb: 0 };

  const vram = gpu.available ? gpu.total_vram_gb : 0;
  const ram = cpu.total_ram_gb ?? 0;
  const memory = vram > 0 ? vram : ram;
  console.log("[buildRecommendedModels] vram:", vram, "ram:", ram, "memory:", memory);

  let key: string;
  if (vram > 0) {
    if (memory <= 4) key = "for_gpu_4gb";
    else if (memory <= 6) key = "for_gpu_6gb";
    else if (memory <= 8) key = "for_gpu_8gb";
    else if (memory <= 12) key = "for_gpu_12gb";
    else key = "for_gpu_16gb";
  } else {
    key = "for_cpu_only";
  }
  console.log("[buildRecommendedModels] selected key:", key);

  const suggestions = options.suggestions || {};
  const ids = suggestions[key] || suggestions["for_cpu_only"] || [];
  console.log("[buildRecommendedModels] ids from suggestions:", ids);

  if (ids.length === 0) {
    console.log("[buildRecommendedModels] ids.length === 0, trying all suggestions");
    const allIds = Array.from(new Set(Object.values(suggestions).flat()));
    console.log("[buildRecommendedModels] allIds from suggestions:", allIds);
    if (allIds.length === 0) {
      console.log("[buildRecommendedModels] allIds also empty, using hardcoded fallback");
      return [
        { id: "llama3.2:latest", name: "llama3.2:latest", size_gb: estimateSizes("llama3.2-3b"), tags: ["fallback"] },
        { id: "phi3:latest", name: "phi3:latest", size_gb: estimateSizes("phi3-3b"), tags: ["fallback"] },
        { id: "qwen2.5:latest", name: "qwen2.5:latest", size_gb: estimateSizes("qwen2.5-7b"), tags: ["fallback"] },
        { id: "codellama:latest", name: "codellama:latest", size_gb: estimateSizes("codellama-7b"), tags: ["fallback"] },
      ];
    }
    return allIds.map((id) => ({
      id,
      name: id,
      size_gb: estimateSizes(id),
      tags: ["recommended"],
    }));
  }
  const result = ids.map((id) => ({
    id,
    name: id,
    size_gb: estimateSizes(id),
    tags: [key.replace(/_/g, " ")],
  }));
  console.log("[buildRecommendedModels] returning:", result.map(m => m.id));
  return result;
}

export default function HirePage() {
  const [filterOptions, setFilterOptions] = useState<FilterOptions | null>(null);
  const [recommendedModels, setRecommendedModels] = useState<HFModel[]>([]);
  const [hfModels, setHfModels] = useState<HFModel[]>([]);
  const [hfSearch, setHfSearch] = useState("");
  const [hfLoading, setHfLoading] = useState(false);
  const [hfError, setHfError] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState({
    name: "",
    role: "worker",
    model_name: "",
    model_provider: "ollama",
    device: "auto" as "auto" | "gpu" | "cpu",
    system_prompt: "",
    auto_accept: false,
    mcp_tools: [] as string[],
  });
  const [success, setSuccess] = useState(false);
  const [hfAuth, setHfAuth] = useState<HFAuthStatus | null>(null);
  const [hfTokenInput, setHfTokenInput] = useState("");
  const [hfTokenSaving, setHfTokenSaving] = useState(false);
  const [mcpTools, setMcpTools] = useState<MCPTool[]>([]);
  const [mcpLoading, setMcpLoading] = useState(true);
  const [downloadError, setDownloadError] = useState("");
  const [pullProgress, setPullProgress] = useState<{ model: string; percent: number; info: string } | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const isHiringRef = useRef(false);
  const hideProgressRef = useRef<number | null>(null);

  useEffect(() => {
    fetch("http://127.0.0.1:8001/api/hf-models/auth-status")
      .then((r) => r.json())
      .then((data: HFAuthStatus) => setHfAuth(data))
      .catch((err) => console.error("Failed to load HF auth status:", err));

    const filterPromise = fetch("http://127.0.0.1:8001/api/hf-models/filter-options")
      .then((r) => (r.ok ? r.json() : null))
      .then((data: FilterOptions | null) => {
        if (data) setFilterOptions(data);
        return data;
      })
      .catch(() => null);

    const installedPromise = fetch("http://127.0.0.1:8001/api/models")
      .then((r) => (r.ok ? r.json() : null))
      .catch(() => null);

    Promise.all([filterPromise, installedPromise]).then(([filterData, installedData]) => {
      const installed: HFModel[] = [];
      const seen = new Set<string>();
      const collect = (group: { models?: Array<{ name?: string; model?: string; supports_tools?: boolean }> } | undefined) => {
        for (const m of group?.models || []) {
          const id = m.name || m.model || "";
          if (id && !seen.has(id)) {
            seen.add(id);
            installed.push({
              id,
              name: id,
              size_gb: estimateSizes(id),
              tags: ["installed"],
              supports_tools: m.supports_tools === true,
            });
          }
        }
      };
      if (installedData) {
        collect(installedData.gpu);
        collect(installedData.cpu);
      }

      // Check internet availability: assume online unless explicitly false
      const isOnline = installedData?.online !== false;

      let models = installed;
      // Only offer downloadable models when we have internet (can pull them)
      if (isOnline && models.length === 0 && filterData) {
        models = buildRecommendedModels(filterData).map((m) => ({
          ...m,
          tags: ["not installed — Ollama will pull on hire"],
        }));
      }

      // If offline and nothing installed, warn the user
      if (!isOnline && models.length === 0) {
        setDownloadError("No internet – only installed models shown");
      }

      setRecommendedModels(models);
      if (models.length > 0) {
        setForm((f) => (f.model_name ? f : { ...f, model_name: models[0].id }));
      }
      setLoading(false);
    });

    fetch("http://127.0.0.1:8001/api/mcp-tools")
      .then((r) => r.json())
      .then((data: MCPTool[]) => {
        setMcpTools(data);
        setMcpLoading(false);
      })
      .catch(() => setMcpLoading(false));
  }, []);

  // Live model-pull progress via office WebSocket
  useEffect(() => {
    const ws = new WebSocket("ws://127.0.0.1:8001/ws/office");
    wsRef.current = ws;
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg?.type !== "pull_progress") return;
        // Only show progress for the model the user is currently hiring.
        if (!isHiringRef.current) return;
        const incomingModel = msg.model;
        // Update progress state.
        setPullProgress({
          model: incomingModel,
          percent: typeof msg.percent === "number" ? msg.percent : 0,
          info: msg.info || "",
        });
        if (hideProgressRef.current) {
          window.clearTimeout(hideProgressRef.current);
          hideProgressRef.current = null;
        }
        if ((msg.percent ?? 0) >= 100) {
          hideProgressRef.current = window.setTimeout(() => {
            setPullProgress(null);
            isHiringRef.current = false;
          }, 2000);
        }
      } catch {
        /* ignore non-json */
      }
    };
    ws.onclose = () => {
      wsRef.current = null;
    };
    return () => {
      ws.close();
      wsRef.current = null;
    };
  }, []);

  const searchHfModels = async () => {
    setHfLoading(true);
    setHfModels([]);
    setHfError("");
    try {
      const url = `http://127.0.0.1:8001/api/hf-models/recommended?query=${encodeURIComponent(hfSearch)}&limit=100`;
      const res = await fetch(url);
      if (!res.ok) {
        const msg = `HTTP ${res.status} from backend`;
        console.error("HF search HTTP error:", msg);
        setHfError(msg);
        return;
      }
      const data = await res.json();
      if (data.error) {
        console.error("HF search API error:", data.error);
        setHfError(String(data.error));
        return;
      }
      const models = (data.models || []).map((m: { id: string; tags?: string[] }) => ({
        id: m.id,
        name: m.id,
        size_gb: estimateSizes(m.id),
        tags: m.tags || ["hf"],
      }));
      setHfModels(models);
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      console.error("HF search failed at http://127.0.0.1:8001/api/hf-models/search:", err);
      let hint = "";
      if (errMsg.includes("Failed to fetch") || errMsg === "Failed to load") {
        hint = " Check your internet connection and that the backend is running.";
      }
      setHfError(errMsg + hint);
    } finally {
      setHfLoading(false);
    }
  };

  const handleSetToken = async (e: React.FormEvent) => {
    e.preventDefault();
    setHfTokenSaving(true);
    try {
      const res = await fetch("http://127.0.0.1:8001/api/hf-models/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: hfTokenInput }),
      });
      if (res.ok) {
        const data = await res.json();
        setHfAuth({ authenticated: data.authenticated, models_dir: hfAuth?.models_dir || "" });
        setHfTokenInput("");
      }
    } catch (err) {
      console.error("Failed to set HF token at http://127.0.0.1:8001/api/hf-models/auth:", err);
    } finally {
      setHfTokenSaving(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSaving(true);
    setDownloadError("");
    isHiringRef.current = true;
    setPullProgress({
      model: form.model_name,
      percent: 0,
      info: "preparing download\u2026",
    });
    try {
      // Check backend health first for better error messaging
      let backendReachable = false;
      try {
        const healthRes = await fetch("http://127.0.0.1:8001/", { method: "GET" });
        backendReachable = healthRes.ok || healthRes.status === 200 || healthRes.status === 404;
      } catch {
        // Backend unreachable — error message below
      }

      if (!backendReachable) {
        const timestamp = new Date().toLocaleString();
        setDownloadError(
          `Backend unreachable at http://127.0.0.1:8001\n\n` +
          `Time: ${timestamp}\n` +
          `URL: POST http://127.0.0.1:8001/api/agents\n\n` +
          `Possible causes:\n` +
          `\u2022 Backend server is not running\n` +
          `\u2022 Port 8001 is blocked by firewall or antivirus\n` +
          `\u2022 Another process is using port 8001\n\n` +
          `Troubleshooting:\n` +
          `1. Run start.bat to start the backend\n` +
          `2. Check if lmworld.exe or python is using port 8001\n` +
          `3. Allow Python through Windows Firewall\n` +
          `4. Restart PC if port was held by crashed process`
        );
        return;
      }

      const payload = {
        name: form.name,
        description: form.system_prompt,
        model_name: form.model_name,
        model_provider: form.model_provider,
        device: form.device,
        is_director: form.role === "director",
        mcp_tools: form.mcp_tools,
        // Workers are expected to create/edit user-requested files. Without
        // this explicit flag the backend schema defaults to read-only and the
        // Worker can only ask the Director for permission instead of writing.
        perm_read: true,
        perm_write: true,
        perm_execute: false,
        perm_mcp: form.mcp_tools.length > 0,
        perm_mode: false,
        perm_subtasks: form.role === "director",
        perm_question: true,
      };

      const res = await fetch("http://127.0.0.1:8001/api/agents", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        isHiringRef.current = false;
        setPullProgress(null);
        setSuccess(true);
        setForm({
          name: "",
          role: "worker",
          model_name: recommendedModels[0]?.id || "",
          model_provider: "ollama",
          device: "auto",
          system_prompt: "",
          auto_accept: false,
          mcp_tools: [],
        });
        setTimeout(() => setSuccess(false), 3000);
      } else {
        let detail = `HTTP ${res.status}`;
        try {
          const body = await res.json();
          if (body?.detail) detail = body.detail;
        } catch {
          /* ignore */
        }
        setDownloadError(detail);
      }
    } catch (err) {
      const timestamp = new Date().toLocaleString();
      const url = "http://127.0.0.1:8001/api/agents";
      const method = "POST";
      const errMsg = err instanceof Error ? err.message : String(err);

      let errorType = "Unknown Error";
      let hint = "";
      if (errMsg.includes("Failed to fetch") || errMsg === "Failed to load") {
        errorType = "Network Error";
        hint =
          `\nPossible causes:\n` +
          `\u2022 Internet connection lost\n` +
          `\u2022 CORS policy blocking request\n` +
          `\u2022 Server crashed mid-request\n` +
          `\u2022 VPN or proxy interference`;
      } else if (errMsg.includes("timeout") || errMsg.includes("timed out")) {
        errorType = "Timeout";
        hint =
          `\nPossible causes:\n` +
          `\u2022 Server is overloaded\n` +
          `\u2022 Large model download taking too long\n` +
          `\u2022 Slow internet connection`;
      } else if (errMsg.includes("ECONNREFUSED") || errMsg.includes("refused")) {
        errorType = "Connection Refused";
        hint =
          `\nPossible causes:\n` +
          `\u2022 Server just crashed\n` +
          `\u2022 Port 8001 was closed unexpectedly`;
      } else {
        hint =
          `\nThis is an unexpected error.\n` +
          `Check backend/backend.log for details.`;
      }

      setDownloadError(
        `${errorType}\n\n` +
        `Time: ${timestamp}\n` +
        `URL: ${url}\n` +
        `Method: ${method}\n` +
        `Error: ${errMsg}` +
        hint
      );
    } finally {
      setSaving(false);
    }
  };

  const toggleMcpTool = (toolId: string) => {
    setForm((f) => {
      const has = f.mcp_tools.includes(toolId);
      return {
        ...f,
        mcp_tools: has ? f.mcp_tools.filter((t) => t !== toolId) : [...f.mcp_tools, toolId],
      };
    });
  };

  const selectedModel = recommendedModels.find((m) => m.id === form.model_name);
  const vram = filterOptions?.hardware?.gpu?.available ? filterOptions.hardware.gpu.total_vram_gb : null;
  const ram = filterOptions?.hardware?.cpu?.total_ram_gb ?? 0;

  return (
    <div className="space-y-6 animate-slide-up max-w-4xl">
      {/* Model pull progress bar */}
      {pullProgress ? (
        <div className="fixed bottom-6 right-6 w-80 bg-gray-900 border border-blue-800 rounded-2xl p-4 shadow-2xl z-50">
          <div className="flex items-center gap-2 mb-2">
            <Loader2 className="w-4 h-4 text-blue-400 animate-spin" />
            <span className="text-sm text-blue-300 font-medium">
              Downloading {pullProgress.model}
            </span>
          </div>
          <div className="w-full bg-gray-800 rounded-full h-3 mb-1">
            <div
              className="bg-blue-500 h-3 rounded-full transition-all duration-300"
              style={{ width: `${Math.min(pullProgress.percent, 100)}%` }}
            />
          </div>
          <p className="text-xs text-gray-400">{pullProgress.info}</p>
        </div>
      ) : null}
      <div>
        <h2 className="text-3xl font-bold text-white">Hire Agent</h2>
        <p className="text-gray-400 mt-1">Recruit a new AI agent for your team</p>
      </div>

      {hfAuth && (
        <div className="bg-gray-900 border border-gray-800 rounded-2xl p-5 space-y-4">
          <div className="flex items-center gap-2 text-gray-300">
            <Key size={18} className="text-blue-400" />
            <h3 className="font-semibold">HuggingFace Configuration</h3>
          </div>
          {hfAuth.authenticated ? (
            <div className="flex items-center gap-2 text-green-400 text-sm">
              <Check size={16} />
              <span>Authenticated — models will be fetched from HuggingFace Hub</span>
            </div>
          ) : (
            <form onSubmit={handleSetToken} className="space-y-3">
              <p className="text-sm text-gray-400">
                HuggingFace token is required to browse and download models. Get your token at{" "}
                <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:underline">
                  huggingface.co/settings/tokens
                </a>
              </p>
              <div className="flex gap-2">
                <input
                  type="password"
                  value={hfTokenInput}
                  onChange={(e) => setHfTokenInput(e.target.value)}
                  placeholder="hf_..."
                  className="flex-1 bg-gray-950 border border-gray-700 rounded-xl px-4 py-2 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 text-sm"
                />
                <button
                  type="submit"
                  disabled={hfTokenSaving || !hfTokenInput.trim()}
                  className="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-xl text-sm transition-colors disabled:opacity-50"
                >
                  {hfTokenSaving ? <Loader2 size={16} className="animate-spin" /> : "Save Token"}
                </button>
              </div>
            </form>
          )}
          {hfAuth.models_dir && (
            <div className="flex items-center gap-2 text-sm text-gray-500">
              <Folder size={14} />
              <span>Models directory: {hfAuth.models_dir}</span>
            </div>
          )}
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500" />
        </div>
      ) : (
        <>
          {filterOptions && (
            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
                <div className="flex items-center gap-2 text-gray-400 mb-2">
                  <MemoryStick size={18} />
                  <span className="text-sm">System RAM</span>
                </div>
                <p className="text-2xl font-bold text-white">{ram.toFixed(1)} GB</p>
              </div>
              <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
                <div className="flex items-center gap-2 text-gray-400 mb-2">
                  <HardDrive size={18} />
                  <span className="text-sm">GPU VRAM</span>
                </div>
                <p className="text-2xl font-bold text-white">{vram ? `${vram} GB` : "N/A"}</p>
              </div>
              <div className="bg-gray-900 border border-gray-800 rounded-xl p-4">
                <div className="flex items-center gap-2 text-gray-400 mb-2">
                  <Cpu size={18} />
                  <span className="text-sm">GPU Available</span>
                </div>
                <p className={`text-2xl font-bold ${filterOptions.hardware?.gpu?.available ? "text-green-400" : "text-red-400"}`}>
                  {filterOptions.hardware?.gpu?.available ? "Yes" : "No"}
                </p>
              </div>
            </div>
          )}

          <form onSubmit={handleSubmit} className="bg-gray-900 border border-gray-800 rounded-2xl p-6 space-y-5">
            {success && (
              <div className="bg-green-500/10 border border-green-500/30 rounded-xl p-4 flex items-center gap-3 text-green-400">
                <Check size={20} />
                <span>Agent hired successfully!</span>
              </div>
            )}

            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <div className="space-y-2">
                <label className="text-sm font-medium text-gray-300">Agent Name</label>
                <input
                  type="text"
                  required
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  placeholder="e.g. Alice"
                  className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                />
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium text-gray-300">Role</label>
                <select
                  value={form.role}
                  onChange={(e) => setForm({ ...form, role: e.target.value })}
                  className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                >
                  <option value="worker">Worker</option>
                  <option value="director">Director</option>
                </select>
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-gray-300">Model</label>
              {recommendedModels.length === 0 ? (
                <div className="text-sm text-red-400 bg-red-500/10 border border-red-500/20 rounded-xl px-4 py-3">
                  No models found. Check backend connection or hardware detection.
                </div>
              ) : (
                <select
                  value={
                    recommendedModels.some((m) => m.id === form.model_name)
                      ? form.model_name
                      : ""
                  }
                  onChange={(e) => setForm({ ...form, model_name: e.target.value })}
                  className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                >
                  <option value="" disabled>
                    Installed models…
                  </option>
                  {recommendedModels.map((model) => (
                    <option key={model.id} value={model.id}>
                      {model.supports_tools === false ? "⚠️ " : ""}{model.id} — {model.tags?.join(", ") || "general"}
                    </option>
                  ))}
                </select>
              )}
              {form.model_name && (
                <div className="flex items-center gap-2 text-xs bg-blue-500/10 border border-blue-500/20 text-blue-200 rounded-lg px-3 py-2">
                  <Check size={14} className="text-blue-400 shrink-0" />
                  <span className="truncate">
                    Selected:{" "}
                    <span className="font-medium text-white inline-flex items-center gap-1">
                      {form.model_name}
                      {selectedModel?.supports_tools === false && (
                        <span
                          title="Модель не поддерживает нативные инструменты. Возможны ошибки. Агент будет использовать нестабильный текстовый парсинг."
                          className="inline-flex items-center text-yellow-300"
                        >
                          <AlertTriangle size={14} />
                        </span>
                      )}
                    </span>
                  </span>
                  <button
                    type="button"
                    onClick={() => setForm((f) => ({ ...f, model_name: "" }))}
                    className="ml-auto text-blue-300 hover:text-white"
                  >
                    clear
                  </button>
                </div>
              )}
              {selectedModel?.supports_tools === false && (
                <div
                  title="Модель не поддерживает нативные инструменты. Возможны ошибки. Агент будет использовать нестабильный текстовый парсинг."
                  className="flex items-start gap-2 text-xs bg-yellow-500/10 border border-yellow-500/30 text-yellow-200 rounded-lg px-3 py-2"
                >
                  <AlertTriangle size={14} className="text-yellow-300 shrink-0 mt-0.5" />
                  <span>
                    Модель не поддерживает нативные инструменты. Возможны ошибки: агент будет использовать text-tool fallback.
                  </span>
                </div>
              )}
              {selectedModel && (
                <div className="flex flex-wrap gap-2 mt-2">
                  {Object.entries(selectedModel.size_gb).map(([q, size]) => (
                    <span
                      key={q}
                      className={`px-2 py-1 text-xs rounded-lg border ${
                        (vram && size <= vram) || (!vram && size <= ram)
                          ? "bg-green-500/10 text-green-400 border-green-500/20"
                          : "bg-red-500/10 text-red-400 border-red-500/20"
                      }`}
                    >
                      {q.toUpperCase()}: {size} GB
                    </span>
                  ))}
                </div>
              )}
            </div>

            <div className="space-y-3">
              <label className="text-sm font-medium text-gray-300 flex items-center gap-2">
                <Search size={16} className="text-blue-400" />
                Search HuggingFace GGUF Models
              </label>
              <div className="flex gap-2">
                <input
                  type="text"
                  value={hfSearch}
                  onChange={(e) => setHfSearch(e.target.value)}
                  placeholder="e.g. qwen gguf, mistral gguf, llama gguf..."
                  className="flex-1 bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                />
                <button
                  type="button"
                  onClick={searchHfModels}
                  disabled={hfLoading}
                  className="bg-blue-500 hover:bg-blue-600 text-white px-4 py-3 rounded-xl transition-colors disabled:opacity-50"
                >
                  {hfLoading ? <Loader2 size={18} className="animate-spin" /> : <Search size={18} />}
                </button>
              </div>
              {hfModels.length > 0 && (
                <div className="bg-gray-950 border border-gray-800 rounded-xl p-2 max-h-64 overflow-auto space-y-1">
                  {hfModels.map((model) => {
                    const selected = form.model_name === model.id;
                    return (
                      <button
                        type="button"
                        key={model.id}
                        onClick={() =>
                          setForm((f) => ({ ...f, model_name: model.id }))
                        }
                        className={`w-full text-left flex items-center gap-2 p-2 rounded-lg transition-colors border ${
                          selected
                            ? "bg-blue-500/20 border-blue-500/40"
                            : "border-transparent hover:bg-gray-900"
                        }`}
                      >
                        <span
                          className={`shrink-0 w-5 h-5 rounded-full flex items-center justify-center text-xs ${
                            selected
                              ? "bg-blue-500 text-white"
                              : "border border-gray-600 text-transparent"
                          }`}
                        >
                          <Check size={12} />
                        </span>
                        <div className="min-w-0 flex-1">
                          <div className="text-sm text-white font-medium truncate flex items-center gap-1">
                            <span className="truncate">{model.id}</span>
                            {model.supports_tools === false && (
                              <span
                                title="Модель не поддерживает нативные инструменты. Возможны ошибки. Агент будет использовать нестабильный текстовый парсинг."
                                className="inline-flex items-center text-yellow-300 shrink-0"
                              >
                                <AlertTriangle size={14} />
                              </span>
                            )}
                          </div>
                          <div className="text-xs text-gray-500 truncate">
                            {model.tags?.join(", ")}
                          </div>
                        </div>
                        {selected && (
                          <span className="shrink-0 text-xs text-blue-300 bg-blue-500/10 px-2 py-0.5 rounded-md">
                            selected
                          </span>
                        )}
                      </button>
                    );
                  })}
                </div>
              )}
              {!hfLoading && hfError && (
                <div className="text-sm text-red-400 bg-red-500/5 border border-red-500/30 rounded-xl px-4 py-3">
                  HuggingFace error: {hfError}
                </div>
              )}
              {!hfLoading && !hfError && hfSearch.trim() && hfModels.length === 0 && (
                <div className="text-sm text-gray-500 bg-gray-950 border border-gray-800 rounded-xl px-4 py-3">
                  No GGUF models found for {hfSearch}. Try a different query or search for a repo ending with -GGUF.
                </div>
              )}
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-gray-300">Device</label>
              <select
                value={form.device}
                onChange={(e) => setForm({ ...form, device: e.target.value as "auto" | "gpu" | "cpu" })}
                className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
              >
                <option value="auto">Auto (GPU if available)</option>
                <option value="gpu">GPU (VRAM)</option>
                <option value="cpu">CPU (RAM)</option>
              </select>
              <p className="text-xs text-gray-500">
                {form.device === "gpu" && filterOptions?.hardware?.gpu?.available
                  ? `Will run on GPU with ${filterOptions.hardware.gpu.total_vram_gb} GB VRAM`
                  : form.device === "gpu" && !filterOptions?.hardware?.gpu?.available
                  ? "Warning: No GPU detected, will fallback to CPU"
                  : form.device === "cpu"
                  ? `Will run on CPU with ${ram.toFixed(1)} GB RAM`
                  : filterOptions?.hardware?.gpu?.available
                  ? `Auto: GPU preferred (${filterOptions.hardware.gpu.total_vram_gb} GB VRAM)`
                  : `Auto: CPU only (${ram.toFixed(1)} GB RAM)`}
              </p>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-gray-300">System Prompt</label>
              <textarea
                value={form.system_prompt}
                onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
                placeholder="Describe the agent's personality and instructions..."
                rows={4}
                className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors resize-none"
              />
            </div>

            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => setForm({ ...form, auto_accept: !form.auto_accept })}
                className={`w-12 h-6 rounded-full transition-colors relative ${form.auto_accept ? "bg-blue-500" : "bg-gray-700"}`}
              >
                <div
                  className={`w-5 h-5 rounded-full bg-white absolute top-0.5 transition-transform ${
                    form.auto_accept ? "translate-x-6" : "translate-x-0.5"
                  }`}
                />
              </button>
              <span className="text-sm text-gray-300">Auto-accept tasks</span>
            </div>

            <div className="space-y-3">
              <label className="text-sm font-medium text-gray-300 flex items-center gap-2">
                <Wrench size={16} className="text-purple-400" />
                MCP Tools
              </label>
              {mcpLoading ? (
                <div className="flex items-center gap-2 text-sm text-gray-500">
                  <Loader2 size={16} className="animate-spin" />
                  Loading tools...
                </div>
              ) : mcpTools.length === 0 ? (
                <p className="text-sm text-gray-500 bg-gray-950 border border-gray-800 rounded-xl px-4 py-3">
                  No MCP tools are available.
                </p>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  {mcpTools.map((tool) => (
                    <div
                      key={tool.id}
                      onClick={() => toggleMcpTool(tool.id)}
                      className={`p-3 rounded-xl border cursor-pointer transition-all ${
                        form.mcp_tools.includes(tool.id)
                          ? "bg-purple-500/10 border-purple-500/30 text-purple-300"
                          : "bg-gray-950 border-gray-800 text-gray-400 hover:border-gray-700"
                      }`}
                    >
                      <div className="flex items-center gap-2">
                        <div className={`w-4 h-4 rounded border flex items-center justify-center ${form.mcp_tools.includes(tool.id) ? "bg-purple-500 border-purple-500" : "border-gray-600"}`}>
                          {form.mcp_tools.includes(tool.id) && <Check size={12} className="text-white" />}
                        </div>
                        <span className="text-sm font-medium">{tool.name}</span>
                      </div>
                      <p className="text-xs mt-1 opacity-70">{tool.description}</p>
                    </div>
                  ))}
                </div>
              )}
            </div>

            {downloadError && (
              <div className="bg-red-500/10 border border-red-500/30 rounded-xl p-4 text-red-400 text-sm whitespace-pre-wrap">
                {downloadError}
              </div>
            )}

            <button
              type="submit"
              disabled={saving}
              className="w-full bg-gradient-to-r from-blue-500 to-purple-600 hover:from-blue-600 hover:to-purple-700 text-white font-semibold py-3 rounded-xl transition-all duration-200 flex items-center justify-center gap-2 disabled:opacity-50"
            >
              {saving ? <Loader2 size={20} className="animate-spin" /> : <UserPlus size={20} />}
              {saving ? "Hiring..." : "Hire Agent"}
            </button>
          </form>
        </>
      )}
    </div>
  );
}
