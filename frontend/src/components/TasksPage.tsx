"use client";

import { ReactNode, useEffect, useMemo, useState } from "react";
import {
  Activity,
  Bot,
  BrainCircuit,
  CheckCircle,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  Cpu,
  FileText,
  HardDriveDownload,
  PauseCircle,
  PlayCircle,
  RefreshCw,
  Terminal,
  User,
  Wrench,
} from "lucide-react";

interface Task {
  id: string;
  title: string;
  description: string;
  status: string;
  assigned_agent_id: string | null;
  result: string;
  logs: string[];
  created_at: string;
}

interface DispatcherEvent {
  time: string;
  type: string;
  title: string;
  action: string;
  meta?: Record<string, unknown>;
}

interface ToolActivity {
  action: string;
  path?: string;
  command?: string;
  success?: boolean;
  time: string;
}

interface DispatcherStatus {
  running: boolean;
  action: string;
  status: string;
  active_agent?: { id: string; name: string; role: string } | null;
  active_model?: string | null;
  active_runtime?: string | null;
  active_task?: { id?: string; title?: string; status?: string } | null;
  current_files?: ToolActivity[];
  boss_summary?: string;
  last_update?: string;
  events?: DispatcherEvent[];
  poll_interval?: number;
  readit_integrations?: Record<string, string>;
}

interface ResourceInfo {
  ram_total_gb: number;
  ram_used_gb: number;
  ram_percent: number;
  vram_total_gb?: number | null;
  vram_used_gb?: number | null;
  vram_percent?: number | null;
  gpu_available: boolean;
}

const API = "http://127.0.0.1:8001";

const columns = [
  { id: "todo", label: "Backlog", color: "bg-blue-500" },
  { id: "in_progress", label: "Working", color: "bg-amber-500" },
  { id: "review", label: "Review", color: "bg-purple-500" },
  { id: "done", label: "Done", color: "bg-emerald-500" },
];

const actionLabels: Record<string, string> = {
  idle: "Idle",
  stopped: "Stopped",
  scanning_board: "Scanning Board",
  delegating: "Delegating",
  assigned: "Assigned",
  preparing_prompt: "Preparing prompt",
  running_inference: "Running model",
  inference_complete: "Saving result",
  completed: "Completed",
  error: "Error",
  runtime_unload: "Runtime unloaded",
};

export default function TasksPage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [dispatcher, setDispatcher] = useState<DispatcherStatus | null>(null);
  const [resources, setResources] = useState<ResourceInfo | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);

  const fetchBoard = async () => {
    try {
      const [tasksRes, statusRes, resourcesRes] = await Promise.all([
        fetch(`${API}/api/tasks`),
        fetch(`${API}/api/dispatcher/status`),
        fetch(`${API}/api/resources`),
      ]);
      if (tasksRes.ok) setTasks(await tasksRes.json());
      if (statusRes.ok) setDispatcher(await statusRes.json());
      if (resourcesRes.ok) setResources(await resourcesRes.json());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchBoard();
    const interval = setInterval(fetchBoard, 2500);

    const ws = new WebSocket("ws://127.0.0.1:8001/ws/office");
    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        if (message.type === "dispatcher_activity") {
          setDispatcher(message.activity);
        }
      } catch {
        // Ignore malformed local websocket events.
      }
    };

    return () => {
      clearInterval(interval);
      ws.close();
    };
  }, []);

  const stats = useMemo(() => {
    const byStatus = Object.fromEntries(columns.map((col) => [col.id, tasks.filter((task) => task.status === col.id).length]));
    return {
      total: tasks.length,
      active: tasks.filter((task) => task.status !== "done").length,
      working: byStatus.in_progress || 0,
      done: byStatus.done || 0,
    };
  }, [tasks]);

  const runControl = async (endpoint: string) => {
    setBusy(true);
    try {
      const response = await fetch(`${API}${endpoint}`, { method: "POST" });
      if (response.ok) {
        const data = await response.json();
        if (data.activity) setDispatcher(data.activity);
      }
      await fetchBoard();
    } finally {
      setBusy(false);
    }
  };

  const moveTask = async (taskId: string, newStatus: string) => {
    await fetch(`${API}/api/tasks/${taskId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: newStatus }),
    });
    fetchBoard();
  };

  const deleteTask = async (taskId: string) => {
    await fetch(`${API}/api/tasks/${taskId}`, { method: "DELETE" });
    setSelectedTask(null);
    fetchBoard();
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-amber-500" />
      </div>
    );
  }

  const activeAction = dispatcher?.action || "idle";
  const activeTaskTitle = dispatcher?.active_task?.title || "No active Board task";
  const currentFiles = dispatcher?.current_files || [];
  const events = dispatcher?.events || [];
  const readitIntegrations = dispatcher?.readit_integrations || {};

  return (
    <div className="space-y-6 animate-slide-up">
      <div className="flex flex-col xl:flex-row xl:items-center justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold text-white flex items-center gap-3">
            <ClipboardList className="text-amber-400" />
            Board
          </h2>
          <div className="text-sm text-gray-500 mt-1">Source of truth for agents, models, tools, and dispatch state.</div>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            disabled={busy || dispatcher?.running}
            onClick={() => runControl("/api/worker/start")}
            className="bg-emerald-600 hover:bg-emerald-500 disabled:bg-gray-800 disabled:text-gray-500 text-white px-4 py-2 rounded-xl flex items-center gap-2 transition-colors"
          >
            <PlayCircle size={18} /> Start worker
          </button>
          <button
            disabled={busy || !dispatcher?.running}
            onClick={() => runControl("/api/worker/stop")}
            className="bg-amber-600 hover:bg-amber-500 disabled:bg-gray-800 disabled:text-gray-500 text-white px-4 py-2 rounded-xl flex items-center gap-2 transition-colors"
          >
            <PauseCircle size={18} /> Stop worker
          </button>
          <button
            disabled={busy}
            onClick={() => runControl("/api/runtime/unload")}
            className="bg-gray-900 hover:bg-gray-800 border border-gray-800 text-gray-200 px-4 py-2 rounded-xl flex items-center gap-2 transition-colors"
          >
            <HardDriveDownload size={18} /> Unload models
          </button>
          <button
            disabled={busy}
            onClick={fetchBoard}
            className="bg-gray-900 hover:bg-gray-800 border border-gray-800 text-gray-200 px-4 py-2 rounded-xl flex items-center gap-2 transition-colors"
          >
            <RefreshCw size={18} className={busy ? "animate-spin" : ""} /> Refresh
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <section className="xl:col-span-2 bg-gray-900/70 border border-gray-800 rounded-2xl p-5">
          <div className="flex items-start justify-between gap-4 mb-5">
            <div>
              <div className="flex items-center gap-2 text-white font-semibold text-lg">
                <Activity className="text-amber-400" size={20} /> Task dispatcher
              </div>
              <div className="text-sm text-gray-400 mt-1">{dispatcher?.status || "Waiting for backend dispatcher status"}</div>
            </div>
            <span className={`px-3 py-1 rounded-full text-xs border ${dispatcher?.running ? "bg-emerald-500/10 text-emerald-300 border-emerald-500/30" : "bg-gray-800 text-gray-400 border-gray-700"}`}>
              {dispatcher?.running ? "worker online" : "worker stopped"}
            </span>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
            <InfoTile icon={<Bot size={18} />} label="Active model" value={dispatcher?.active_model || "none"} accent="text-cyan-300" />
            <InfoTile icon={<Cpu size={18} />} label="Runtime" value={dispatcher?.active_runtime || "unknown"} accent="text-purple-300" />
            <InfoTile icon={<User size={18} />} label="Agent" value={dispatcher?.active_agent?.name || "none"} accent="text-blue-300" />
            <InfoTile icon={<Wrench size={18} />} label="Action" value={actionLabels[activeAction] || activeAction} accent="text-amber-300" />
          </div>

          <div className="mt-4 grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="bg-gray-950 border border-gray-800 rounded-xl p-4">
              <div className="text-xs text-gray-500 uppercase tracking-wide mb-2">Current Board card</div>
              <div className="text-white font-medium">{activeTaskTitle}</div>
              <div className="text-xs text-gray-500 mt-2">Last update: {dispatcher?.last_update ? new Date(dispatcher.last_update).toLocaleString() : "n/a"}</div>
            </div>
            <div className="bg-gray-950 border border-gray-800 rounded-xl p-4">
              <div className="flex items-center gap-2 text-sm font-semibold text-amber-300 mb-2">
                <BrainCircuit size={16} /> Boss activity summary
              </div>
              <p className="text-sm text-gray-300 leading-relaxed">
                {dispatcher?.boss_summary || "Boss is waiting for dispatcher data."}
              </p>
              <p className="text-xs text-gray-600 mt-2">Shows a safe summary of the plan and observable decisions, not hidden chain-of-thought.</p>
            </div>
          </div>
        </section>

        <section className="bg-gray-900/70 border border-gray-800 rounded-2xl p-5">
          <div className="flex items-center gap-2 text-white font-semibold mb-4">
            <Cpu className="text-cyan-400" size={20} /> Systems
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Stat label="Tasks" value={stats.total.toString()} />
            <Stat label="Active" value={stats.active.toString()} />
            <Stat label="Working" value={stats.working.toString()} />
            <Stat label="Done" value={stats.done.toString()} />
          </div>
          <div className="mt-4 space-y-3">
            <Meter label="RAM" value={resources?.ram_percent ?? 0} detail={resources ? `${resources.ram_used_gb.toFixed(1)} / ${resources.ram_total_gb.toFixed(1)} GB` : "n/a"} />
            <Meter label="VRAM" value={resources?.vram_percent ?? 0} detail={resources?.vram_total_gb ? `${(resources.vram_used_gb || 0).toFixed(1)} / ${resources.vram_total_gb.toFixed(1)} GB` : "not detected"} />
          </div>
          <div className="mt-4 rounded-xl border border-amber-500/20 bg-amber-500/5 p-3">
            <div className="text-xs uppercase tracking-wide text-amber-300 font-semibold mb-1">READIT integrations</div>
            <div className="text-xs text-gray-400 leading-relaxed">
              llama.cpp GGUF fallback, llama-swap-style unload, MCP file/terminal telemetry, LangGraph-style worker loop, and Letta-style Board memory are wired into this dispatcher. vLLM is marked as a planned high-VRAM runtime option, not falsely shown as installed.
            </div>
          </div>
        </section>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4">
        <section className="xl:col-span-2 grid grid-cols-1 md:grid-cols-2 2xl:grid-cols-4 gap-4">
          {columns.map((col) => {
            const columnTasks = tasks.filter((task) => task.status === col.id);
            const columnIndex = columns.findIndex((item) => item.id === col.id);
            return (
              <div key={col.id} className="bg-gray-900/50 border border-gray-800 rounded-2xl p-4 min-h-[360px]">
                <div className="flex items-center gap-2 mb-4">
                  <div className={`w-3 h-3 rounded-full ${col.color}`} />
                  <h3 className="font-semibold text-white">{col.label}</h3>
                  <span className="ml-auto text-sm text-gray-500">{columnTasks.length}</span>
                </div>

                <div className="space-y-3">
                  {columnTasks.length === 0 ? (
                    <div className="text-xs text-gray-600 border border-dashed border-gray-800 rounded-xl p-4 text-center">empty</div>
                  ) : (
                    columnTasks.map((task) => (
                      <div
                        key={task.id}
                        onClick={() => setSelectedTask(task)}
                        className={`bg-gray-900 border rounded-xl p-3 hover:border-gray-700 transition-colors cursor-pointer ${dispatcher?.active_task?.id === task.id ? "border-amber-500/60" : "border-gray-800"}`}
                      >
                        <h4 className="font-medium text-white text-sm mb-2">{task.title}</h4>
                        <p className="text-xs text-gray-400 mb-3 line-clamp-3">{task.description || "No description"}</p>
                        <div className="flex items-center justify-between text-xs text-gray-500">
                          <span>{new Date(task.created_at).toLocaleDateString()}</span>
                          {task.result && <CheckCircle size={13} className="text-emerald-400" />}
                        </div>
                        <div className="flex gap-1 mt-3">
                          {columnIndex > 0 && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                moveTask(task.id, columns[columnIndex - 1].id);
                              }}
                              className="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-2 py-1 rounded transition-colors"
                            >
                              <ChevronLeft size={14} />
                            </button>
                          )}
                          {columnIndex < columns.length - 1 && (
                            <button
                              onClick={(e) => {
                                e.stopPropagation();
                                moveTask(task.id, columns[columnIndex + 1].id);
                              }}
                              className="text-xs bg-gray-800 hover:bg-gray-700 text-gray-300 px-2 py-1 rounded transition-colors"
                            >
                              <ChevronRight size={14} />
                            </button>
                          )}
                        </div>
                      </div>
                    ))
                  )}
                </div>
              </div>
            );
          })}
        </section>

        <aside className="space-y-4">
          <section className="bg-gray-900/70 border border-gray-800 rounded-2xl p-4">
            <div className="flex items-center gap-2 text-white font-semibold mb-3">
              <BrainCircuit className="text-amber-400" size={18} /> READIT stack
            </div>
            <div className="space-y-2 max-h-72 overflow-y-auto">
              {Object.keys(readitIntegrations).length === 0 ? (
                <div className="text-sm text-gray-500">Waiting for integration status from backend.</div>
              ) : (
                Object.entries(readitIntegrations).map(([name, description]) => {
                  const planned = description.toLowerCase().includes("planned");
                  return (
                    <div key={name} className="bg-gray-950 border border-gray-800 rounded-xl p-3">
                      <div className="flex items-center justify-between gap-2 mb-1">
                        <span className="text-sm font-semibold text-white">{name.replaceAll("_", ".")}</span>
                        <span className={`text-[10px] px-2 py-0.5 rounded-full border ${planned ? "text-amber-300 border-amber-500/30 bg-amber-500/10" : "text-emerald-300 border-emerald-500/30 bg-emerald-500/10"}`}>
                          {planned ? "planned" : "wired"}
                        </span>
                      </div>
                      <div className="text-xs text-gray-500 leading-relaxed">{description}</div>
                    </div>
                  );
                })
              )}
            </div>
          </section>

          <section className="bg-gray-900/70 border border-gray-800 rounded-2xl p-4">
            <div className="flex items-center gap-2 text-white font-semibold mb-3">
              <FileText className="text-cyan-400" size={18} /> Files / tools
            </div>
            <div className="space-y-2 max-h-64 overflow-y-auto">
              {currentFiles.length === 0 ? (
                <div className="text-sm text-gray-500">No file or terminal actions yet.</div>
              ) : (
                currentFiles.slice().reverse().map((item, index) => (
                  <div key={`${item.time}-${index}`} className="bg-gray-950 border border-gray-800 rounded-xl p-3">
                    <div className="flex items-center gap-2 text-sm text-gray-300">
                      {item.command ? <Terminal size={14} className="text-purple-300" /> : <FileText size={14} className="text-cyan-300" />}
                      <span className="font-medium">{item.action}</span>
                      <span className={item.success === false ? "text-red-400" : "text-emerald-400"}>{item.success === false ? "failed" : "ok"}</span>
                    </div>
                    <div className="text-xs text-gray-500 mt-1 break-all">{item.path || item.command}</div>
                  </div>
                ))
              )}
            </div>
          </section>

          <section className="bg-gray-900/70 border border-gray-800 rounded-2xl p-4">
            <div className="flex items-center gap-2 text-white font-semibold mb-3">
              <Activity className="text-amber-400" size={18} /> Live log
            </div>
            <div className="space-y-2 max-h-80 overflow-y-auto">
              {events.length === 0 ? (
                <div className="text-sm text-gray-500">No dispatcher events yet.</div>
              ) : (
                events.slice(0, 12).map((event, index) => (
                  <div key={`${event.time}-${index}`} className="border-l border-gray-800 pl-3 py-1">
                    <div className="text-sm text-gray-300">{event.title}</div>
                    <div className="text-xs text-gray-600">{new Date(event.time).toLocaleTimeString()} · {event.type}</div>
                  </div>
                ))
              )}
            </div>
          </section>
        </aside>
      </div>

      {selectedTask && (
        <div className="fixed inset-0 bg-black/60 backdrop-blur-sm flex items-center justify-center z-50 p-4">
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6 max-w-2xl w-full max-h-[80vh] overflow-y-auto">
            <div className="flex items-start justify-between mb-4">
              <div>
                <h3 className="text-xl font-bold text-white">{selectedTask.title}</h3>
                <span className="text-xs px-2 py-0.5 rounded border mt-2 inline-block bg-gray-800 text-gray-300 border-gray-700">
                  {selectedTask.status.replace("_", " ")}
                </span>
              </div>
              <button onClick={() => setSelectedTask(null)} className="text-gray-400 hover:text-white transition-colors text-2xl">×</button>
            </div>
            <p className="text-gray-300 mb-4 whitespace-pre-wrap">{selectedTask.description || "No description"}</p>
            {selectedTask.result && (
              <div className="bg-gray-950 border border-gray-800 rounded-xl p-4 mb-4">
                <h4 className="text-sm font-semibold text-green-400 mb-2 flex items-center gap-2">
                  <CheckCircle size={16} /> Result
                </h4>
                <pre className="text-sm text-gray-300 whitespace-pre-wrap font-mono">{selectedTask.result}</pre>
              </div>
            )}
            {selectedTask.logs && selectedTask.logs.length > 0 && (
              <div className="bg-gray-950 border border-gray-800 rounded-xl p-4 mb-4">
                <h4 className="text-sm font-semibold text-gray-400 mb-2">Logs</h4>
                <div className="space-y-1">
                  {selectedTask.logs.map((log, i) => (
                    <div key={i} className="text-xs text-gray-500 font-mono">{log}</div>
                  ))}
                </div>
              </div>
            )}
            <div className="flex justify-between gap-2 pt-2">
              <button
                onClick={() => deleteTask(selectedTask.id)}
                className="bg-red-500/10 hover:bg-red-500/20 text-red-300 border border-red-500/30 px-4 py-2 rounded-xl text-sm"
              >
                Delete card
              </button>
              <button onClick={() => setSelectedTask(null)} className="bg-gray-800 hover:bg-gray-700 text-gray-200 px-4 py-2 rounded-xl text-sm">
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function InfoTile({ icon, label, value, accent }: { icon: ReactNode; label: string; value: string; accent: string }) {
  return (
    <div className="bg-gray-950 border border-gray-800 rounded-xl p-3">
      <div className="flex items-center gap-2 text-xs text-gray-500 uppercase tracking-wide">{icon}{label}</div>
      <div className={`text-sm font-semibold mt-2 truncate ${accent}`}>{value}</div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-950 border border-gray-800 rounded-xl p-3">
      <div className="text-xs text-gray-500 uppercase tracking-wide">{label}</div>
      <div className="text-2xl font-bold text-white mt-1">{value}</div>
    </div>
  );
}

function Meter({ label, value, detail }: { label: string; value: number; detail: string }) {
  const safeValue = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0));
  return (
    <div>
      <div className="flex justify-between text-xs mb-1">
        <span className="text-gray-400">{label}</span>
        <span className="text-gray-500">{detail}</span>
      </div>
      <div className="h-2 bg-gray-950 rounded-full overflow-hidden border border-gray-800">
        <div className="h-full bg-gradient-to-r from-cyan-500 to-amber-500" style={{ width: `${safeValue}%` }} />
      </div>
    </div>
  );
}
