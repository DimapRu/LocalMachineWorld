"use client";

import { useEffect, useState } from "react";
import { Key, Check, Loader2, Folder, HardDrive, Cpu, HelpCircle, X, Download, Server, ServerOff, MonitorCog } from "lucide-react";

interface HFAuthStatus {
  authenticated: boolean;
  models_dir: string;
}

interface ResourceInfo {
  ram_total_gb: number;
  ram_used_gb: number;
  ram_percent: number;
  vram_total_gb: number | null;
  vram_used_gb: number | null;
  gpu_available: boolean;
}

interface OllamaInfo {
  installed: boolean;
  path: string | null;
  version: string | null;
  running: boolean;
}

type SettingsTab = "general" | "runtime" | "os";
type AgentOsMode = "auto" | "windows" | "linux" | "macos";
type AgentOsName = "windows" | "linux" | "macos" | "unknown";

interface AgentOsInfo {
  mode: AgentOsMode;
  detected_os: AgentOsName;
  selected_os: AgentOsName;
  source: "auto" | "manual";
  label: string;
  shell: string;
  path_examples: string[];
  prompt_context: string;
}

const osOptions: Array<{ mode: AgentOsMode; title: string; description: string }> = [
  { mode: "auto", title: "Auto detect", description: "Use the OS detected by the backend on this machine." },
  { mode: "windows", title: "Windows", description: "Tell agents to use Windows paths and cmd/PowerShell-compatible commands." },
  { mode: "linux", title: "Linux", description: "Tell agents to use Linux paths and POSIX shell commands." },
  { mode: "macos", title: "macOS", description: "Tell agents to use macOS paths and POSIX shell commands." },
];

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState<SettingsTab>("general");
  const [hfAuth, setHfAuth] = useState<HFAuthStatus | null>(null);
  const [hfTokenInput, setHfTokenInput] = useState("");
  const [hfTokenSaving, setHfTokenSaving] = useState(false);
  const [hfSuccess, setHfSuccess] = useState(false);
  const [resources, setResources] = useState<ResourceInfo | null>(null);
  const [showHfGuide, setShowHfGuide] = useState(false);
  const [ollamaInfo, setOllamaInfo] = useState<OllamaInfo | null>(null);
  const [ollamaInstalling, setOllamaInstalling] = useState(false);
  const [agentOs, setAgentOs] = useState<AgentOsInfo | null>(null);
  const [agentOsSaving, setAgentOsSaving] = useState(false);
  const [agentOsSaved, setAgentOsSaved] = useState(false);

  useEffect(() => {
    // Load HF auth status
    fetch("http://127.0.0.1:8001/api/hf-models/auth-status")
      .then((r) => r.json())
      .then((data: HFAuthStatus) => setHfAuth(data))
      .catch((err) => console.error("Failed to load HF auth status:", err));

    // Load system resources
    fetch("http://127.0.0.1:8001/api/resources")
      .then((r) => r.json())
      .then((data) => setResources(data))
      .catch((err) => console.error("Failed to load resources:", err));

    // Load Ollama and OS info
    loadOllamaInfo();
    loadAgentOs();
  }, []);

  const loadOllamaInfo = () => {
    fetch("http://127.0.0.1:8001/api/ollama-info")
      .then((r) => r.json())
      .then((data: OllamaInfo) => setOllamaInfo(data))
      .catch((err) => console.error("Failed to load Ollama info:", err));
  };

  const loadAgentOs = () => {
    fetch("http://127.0.0.1:8001/api/system/os")
      .then((r) => r.json())
      .then((data: AgentOsInfo) => setAgentOs(data))
      .catch((err) => console.error("Failed to load agent OS context:", err));
  };

  const handleInstallOllama = async () => {
    setOllamaInstalling(true);
    try {
      const res = await fetch("http://127.0.0.1:8001/api/ollama-install", {
        method: "POST",
      });
      const data = await res.json();
      if (data.status === "installed" || data.status === "already_installed") {
        loadOllamaInfo();
      } else if (data.error) {
        alert(`Installation failed: ${data.error}`);
      }
    } catch (err) {
      console.error("Failed to install Ollama:", err);
      alert("Failed to install Ollama. Check console for details.");
    } finally {
      setOllamaInstalling(false);
    }
  };

  const handleAgentOsMode = async (mode: AgentOsMode) => {
    setAgentOsSaving(true);
    setAgentOsSaved(false);
    try {
      const res = await fetch("http://127.0.0.1:8001/api/system/os", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!res.ok) {
        throw new Error(await res.text());
      }
      const data: AgentOsInfo = await res.json();
      setAgentOs(data);
      setAgentOsSaved(true);
      setTimeout(() => setAgentOsSaved(false), 3000);
    } catch (err) {
      console.error("Failed to save agent OS context:", err);
      alert("Failed to save OS setting. Check backend logs for details.");
    } finally {
      setAgentOsSaving(false);
    }
  };

  const handleSetHfToken = async (e: React.FormEvent) => {
    e.preventDefault();
    setHfTokenSaving(true);
    setHfSuccess(false);
    try {
      const res = await fetch("http://127.0.0.1:8001/api/hf-models/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: hfTokenInput }),
      });
      if (res.ok) {
        const data = await res.json();
        setHfAuth({ authenticated: data.authenticated, models_dir: hfAuth?.models_dir || "" });
        setHfSuccess(true);
        setTimeout(() => setHfSuccess(false), 3000);
      }
    } catch (err) {
      console.error("Failed to set HF token:", err);
    } finally {
      setHfTokenSaving(false);
    }
  };

  const handleClearHfToken = async () => {
    setHfTokenSaving(true);
    try {
      await fetch("http://127.0.0.1:8001/api/hf-models/auth", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token: "" }),
      });
      setHfAuth({ authenticated: false, models_dir: hfAuth?.models_dir || "" });
    } catch (err) {
      console.error("Failed to clear HF token:", err);
    } finally {
      setHfTokenSaving(false);
    }
  };

  const tabClass = (tab: SettingsTab) =>
    `px-4 py-2 rounded-xl text-sm font-medium transition-colors ${
      activeTab === tab
        ? "bg-blue-500 text-white"
        : "bg-gray-900 text-gray-400 hover:text-white hover:bg-gray-800 border border-gray-800"
    }`;

  return (
    <div className="space-y-6 animate-slide-up">
      <div>
        <h2 className="text-3xl font-bold text-white">Settings</h2>
        <p className="text-gray-400 mt-1">Configure your lmWorld preferences</p>
      </div>

      <div className="flex flex-wrap gap-2">
        <button onClick={() => setActiveTab("general")} className={tabClass("general")}>General</button>
        <button onClick={() => setActiveTab("runtime")} className={tabClass("runtime")}>Runtime</button>
        <button onClick={() => setActiveTab("os")} className={tabClass("os")}>Current OS</button>
      </div>

      {activeTab === "general" && (
        <>
          {/* HuggingFace Token Section */}
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6">
            <div className="flex items-center gap-3 mb-4">
              <Key size={24} className="text-blue-400" />
              <h3 className="text-xl font-semibold text-white">HuggingFace API Token</h3>
              <button
                onClick={() => setShowHfGuide(true)}
                className="ml-auto text-gray-500 hover:text-blue-400 transition-colors p-1"
                title="Token permissions guide"
              >
                <HelpCircle size={20} />
              </button>
            </div>
            
            <p className="text-gray-400 text-sm mb-4">
              Required for searching and downloading models from HuggingFace Hub. 
              Get your token at{" "}
              <a 
                href="https://huggingface.co/settings/tokens" 
                target="_blank" 
                rel="noopener noreferrer"
                className="text-blue-400 hover:text-blue-300 underline"
              >
                huggingface.co/settings/tokens
              </a>
            </p>

            {hfAuth?.authenticated ? (
              <div className="space-y-4">
                <div className="flex items-center gap-2 text-green-400 bg-green-500/10 border border-green-500/20 rounded-xl px-4 py-3">
                  <Check size={20} />
                  <span>HuggingFace token configured</span>
                </div>
                <button
                  onClick={handleClearHfToken}
                  disabled={hfTokenSaving}
                  className="bg-red-500/10 hover:bg-red-500/20 border border-red-500/30 text-red-400 px-4 py-2 rounded-xl text-sm transition-colors disabled:opacity-50"
                >
                  {hfTokenSaving ? <Loader2 size={16} className="animate-spin" /> : "Clear Token"}
                </button>
              </div>
            ) : (
              <form onSubmit={handleSetHfToken} className="space-y-4">
                <div className="flex gap-2">
                  <input
                    type="password"
                    value={hfTokenInput}
                    onChange={(e) => setHfTokenInput(e.target.value)}
                    placeholder="hf_xxxxxxxxxxxxxxxxxxxxx"
                    className="flex-1 bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white placeholder-gray-500 focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500 transition-colors"
                  />
                  <button
                    type="submit"
                    disabled={hfTokenSaving || !hfTokenInput.trim()}
                    className="bg-blue-500 hover:bg-blue-600 text-white px-6 py-3 rounded-xl transition-colors disabled:opacity-50 flex items-center gap-2"
                  >
                    {hfTokenSaving ? <Loader2 size={18} className="animate-spin" /> : "Save"}
                    {hfSuccess && <Check size={18} />}
                  </button>
                </div>
                {hfSuccess && (
                  <p className="text-green-400 text-sm">Token saved successfully!</p>
                )}
              </form>
            )}

            {hfAuth?.models_dir && (
              <div className="flex items-center gap-2 mt-4 text-sm text-gray-500">
                <Folder size={14} />
                <span>Models directory: {hfAuth.models_dir}</span>
              </div>
            )}
          </div>
        </>
      )}

      {activeTab === "runtime" && (
        <>
          {/* Ollama Installation Section */}
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6">
            <div className="flex items-center gap-3 mb-4">
              {ollamaInfo?.installed ? (
                <Server size={24} className="text-green-400" />
              ) : (
                <ServerOff size={24} className="text-red-400" />
              )}
              <h3 className="text-xl font-semibold text-white">Ollama (Local LLM Engine)</h3>
            </div>

            {!ollamaInfo ? (
              <div className="flex items-center gap-2 text-gray-400 text-sm">
                <Loader2 size={16} className="animate-spin" />
                Checking...
              </div>
            ) : !ollamaInfo.installed ? (
              <div className="space-y-3">
                <p className="text-gray-400 text-sm">
                  Ollama is not installed. lmWorld can auto-download it into the project folder (~1.3 GB).
                </p>
                <button
                  onClick={handleInstallOllama}
                  disabled={ollamaInstalling}
                  className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 disabled:cursor-not-allowed text-white rounded-lg font-medium transition-colors"
                >
                  {ollamaInstalling ? (
                    <>
                      <Loader2 size={18} className="animate-spin" />
                      Downloading & Installing...
                    </>
                  ) : (
                    <>
                      <Download size={18} />
                      Download Ollama
                    </>
                  )}
                </button>
              </div>
            ) : (
              <div className="space-y-2 text-sm">
                <div className="flex items-center gap-2">
                  <span className="text-gray-400">Version:</span>
                  <span className="text-white font-mono">{ollamaInfo.version || "unknown"}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-gray-400">Path:</span>
                  <span className="text-gray-300 font-mono text-xs">{ollamaInfo.path}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-gray-400">Status:</span>
                  <span className={ollamaInfo.running ? "text-green-400" : "text-yellow-400"}>
                    {ollamaInfo.running ? "● Running on :11434" : "○ Not running (will start on next Hire Agent)"}
                  </span>
                </div>
              </div>
            )}
          </div>

          {/* System Resources Section */}
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6">
            <div className="flex items-center gap-3 mb-4">
              <HardDrive size={24} className="text-purple-400" />
              <h3 className="text-xl font-semibold text-white">System Resources</h3>
            </div>

            {resources ? (
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <div className="bg-gray-800/50 rounded-xl p-4">
                  <div className="flex items-center gap-2 text-gray-400 mb-2">
                    <Cpu size={16} />
                    <span className="text-sm">CPU RAM</span>
                  </div>
                  <p className="text-2xl font-bold text-white">{resources.ram_total_gb.toFixed(1)} GB</p>
                  <div className="mt-2 h-2 bg-gray-700 rounded-full overflow-hidden">
                    <div 
                      className="h-full bg-blue-500" 
                      style={{ width: `${resources.ram_percent}%` }}
                    />
                  </div>
                  <p className="text-xs text-gray-500 mt-1">
                    {resources.ram_used_gb.toFixed(1)} GB used ({resources.ram_percent.toFixed(1)}%)
                  </p>
                </div>

                <div className="bg-gray-800/50 rounded-xl p-4">
                  <div className="flex items-center gap-2 text-gray-400 mb-2">
                    <HardDrive size={16} />
                    <span className="text-sm">GPU VRAM</span>
                  </div>
                  {resources.gpu_available ? (
                    <>
                      <p className="text-2xl font-bold text-white">{resources.vram_total_gb?.toFixed(1)} GB</p>
                      <div className="mt-2 h-2 bg-gray-700 rounded-full overflow-hidden">
                        <div 
                          className="h-full bg-purple-500" 
                          style={{ width: `${((resources.vram_used_gb || 0) / (resources.vram_total_gb || 1)) * 100}%` }}
                        />
                      </div>
                      <p className="text-xs text-gray-500 mt-1">
                        {resources.vram_used_gb?.toFixed(1)} GB used
                      </p>
                    </>
                  ) : (
                    <p className="text-xl text-gray-500">N/A</p>
                  )}
                </div>

                <div className="bg-gray-800/50 rounded-xl p-4">
                  <div className="flex items-center gap-2 text-gray-400 mb-2">
                    <span className="text-sm">GPU Status</span>
                  </div>
                  <p className={`text-2xl font-bold ${resources.gpu_available ? "text-green-400" : "text-red-400"}`}>
                    {resources.gpu_available ? "Available" : "Not Detected"}
                  </p>
                </div>
              </div>
            ) : (
              <div className="flex items-center justify-center h-32">
                <Loader2 size={24} className="animate-spin text-gray-500" />
              </div>
            )}
          </div>

          {/* Ollama Configuration */}
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6">
            <div className="flex items-center gap-3 mb-4">
              <Cpu size={24} className="text-amber-400" />
              <h3 className="text-xl font-semibold text-white">Ollama Configuration</h3>
            </div>

            <div className="space-y-3">
              <div className="flex items-center justify-between bg-gray-800/50 rounded-xl px-4 py-3">
                <span className="text-gray-400">GPU Server</span>
                <span className="text-white font-mono text-sm">http://localhost:11434</span>
              </div>
              <div className="flex items-center justify-between bg-gray-800/50 rounded-xl px-4 py-3">
                <span className="text-gray-400">CPU Server</span>
                <span className="text-white font-mono text-sm">http://localhost:11435</span>
              </div>
            </div>

            <p className="text-gray-500 text-sm mt-4">
              Ollama servers are configured in backend/.env file
            </p>
          </div>
        </>
      )}

      {activeTab === "os" && (
        <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6">
          <div className="flex items-center gap-3 mb-4">
            <MonitorCog size={24} className="text-cyan-400" />
            <div>
              <h3 className="text-xl font-semibold text-white">Current OS for Agents</h3>
              <p className="text-sm text-gray-400">This context is injected into Director and Worker prompts before they create files or run commands.</p>
            </div>
          </div>

          {!agentOs ? (
            <div className="flex items-center gap-2 text-gray-400 text-sm">
              <Loader2 size={16} className="animate-spin" />
              Loading OS context...
            </div>
          ) : (
            <div className="space-y-5">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                <div className="bg-gray-800/50 rounded-xl p-4">
                  <p className="text-xs uppercase tracking-wide text-gray-500 mb-1">Detected backend OS</p>
                  <p className="text-white font-semibold">{agentOs.detected_os}</p>
                </div>
                <div className="bg-gray-800/50 rounded-xl p-4">
                  <p className="text-xs uppercase tracking-wide text-gray-500 mb-1">Sent to agents</p>
                  <p className="text-cyan-300 font-semibold">{agentOs.label}</p>
                </div>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {osOptions.map((option) => {
                  const selected = agentOs.mode === option.mode;
                  return (
                    <button
                      key={option.mode}
                      onClick={() => handleAgentOsMode(option.mode)}
                      disabled={agentOsSaving}
                      className={`text-left rounded-xl border p-4 transition-colors disabled:opacity-60 ${
                        selected
                          ? "border-cyan-400 bg-cyan-500/10"
                          : "border-gray-800 bg-gray-950 hover:border-gray-600"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-white font-semibold">{option.title}</span>
                        {selected && <Check size={18} className="text-cyan-300" />}
                      </div>
                      <p className="text-sm text-gray-400 mt-1">{option.description}</p>
                    </button>
                  );
                })}
              </div>

              <div className="bg-gray-950 border border-gray-800 rounded-xl p-4 space-y-3">
                <div>
                  <p className="text-xs uppercase tracking-wide text-gray-500 mb-1">Shell rule</p>
                  <p className="text-sm text-gray-300">{agentOs.shell}</p>
                </div>
                <div>
                  <p className="text-xs uppercase tracking-wide text-gray-500 mb-1">Path examples</p>
                  <ul className="list-disc list-inside space-y-1 text-sm text-gray-300">
                    {agentOs.path_examples.map((item) => (
                      <li key={item}>{item}</li>
                    ))}
                  </ul>
                </div>
              </div>

              <div className="bg-blue-500/10 border border-blue-500/20 rounded-xl p-4">
                <p className="text-sm text-blue-200">
                  Agents will now receive this OS context in every Director and Worker prompt, so they should stop using Linux/macOS commands on Windows and vice versa.
                </p>
              </div>

              <div className="flex items-center gap-3 text-sm">
                {agentOsSaving && (
                  <span className="flex items-center gap-2 text-gray-400"><Loader2 size={16} className="animate-spin" /> Saving...</span>
                )}
                {agentOsSaved && (
                  <span className="flex items-center gap-2 text-green-400"><Check size={16} /> OS setting saved</span>
                )}
              </div>
            </div>
          )}
        </div>
      )}

      {/* HuggingFace Token Permissions Guide Modal */}
      {showHfGuide && (
        <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4">
          <div className="bg-gray-900 border border-gray-700 rounded-2xl max-w-lg w-full max-h-[80vh] overflow-y-auto">
            <div className="flex items-center justify-between p-6 border-b border-gray-800">
              <h3 className="text-xl font-bold text-white">Token Permissions Guide</h3>
              <button
                onClick={() => setShowHfGuide(false)}
                className="text-gray-400 hover:text-white transition-colors p-1"
              >
                <X size={20} />
              </button>
            </div>
            
            <div className="p-6 space-y-6">
              <p className="text-gray-400">
                When creating a HuggingFace token for lmWorld, you only need to enable specific permissions.
                <strong className="text-white"> Avoid selecting all permissions</strong> — this is unnecessary and may cause issues.
              </p>

              <div className="space-y-4">
                <div className="bg-green-500/10 border border-green-500/30 rounded-xl p-4">
                  <h4 className="text-green-400 font-semibold mb-2">Required Permissions</h4>
                  <ul className="space-y-2 text-sm text-gray-300">
                    <li className="flex items-start gap-2">
                      <Check size={16} className="text-green-400 mt-0.5 flex-shrink-0" />
                      <span><strong>Inference</strong> — Make calls to Inference Providers</span>
                    </li>
                    <li className="flex items-start gap-2">
                      <Check size={16} className="text-green-400 mt-0.5 flex-shrink-0" />
                      <span><strong>Repositories</strong> — Read access to public gated repos, Read access to personal namespace</span>
                    </li>
                  </ul>
                </div>

                <div className="space-y-3">
                  <h4 className="text-white font-semibold">Steps to create a proper token:</h4>
                  <ol className="space-y-3 text-sm text-gray-300 list-decimal list-inside">
                    <li>Go to <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:underline">huggingface.co/settings/tokens</a></li>
                    <li>Click <strong className="text-white">New token</strong></li>
                    <li>Enter a name (e.g., lmWorld)</li>
                    <li>Select type: <strong className="text-white">Fine-grained</strong></li>
                    <li>Under <strong className="text-white">Repository permissions</strong>:
                      <ul className="ml-4 mt-1 space-y-1 list-disc list-inside">
                        <li>Set <strong>Resources</strong> to No restriction</li>
                        <li>Enable <strong>Repositories: Read</strong> access</li>
                      </ul>
                    </li>
                    <li>Under <strong className="text-white">Inference</strong>:
                      <ul className="ml-4 mt-1 space-y-1 list-disc list-inside">
                        <li>Enable <strong>Make calls to Inference Providers</strong></li>
                      </ul>
                    </li>
                    <li>Click <strong className="text-white">Create</strong> and copy the token</li>
                  </ol>
                </div>

                <div className="bg-amber-500/10 border border-amber-500/30 rounded-xl p-4">
                  <h4 className="text-amber-400 font-semibold mb-2">Important</h4>
                  <p className="text-sm text-gray-300">
                    {"You don't need Write permissions. A read-only token is sufficient for searching and downloading models."}
                    {" For public models, you may not need any token at all."}
                  </p>
                </div>
              </div>

              <button
                onClick={() => setShowHfGuide(false)}
                className="w-full bg-blue-500 hover:bg-blue-600 text-white py-3 rounded-xl transition-colors font-medium"
              >
                Got it
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
