"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Bot, BrainCircuit, CheckCircle2, RefreshCw, Search, Terminal, Wrench } from "lucide-react";

interface OpsLogEntry {
  id: string;
  timestamp: string;
  source: string;
  level: "INFO" | "WARNING" | "ERROR" | string;
  message: string;
  error_details: string;
  action_type: string;
  status: string;
  correlation_id: string | null;
  agent_id: string | null;
  task_id: string | null;
  payload: Record<string, unknown>;
}

interface OpsLogResponse {
  entries: OpsLogEntry[];
}

const API = "http://127.0.0.1:8001";

const levelStyles: Record<string, string> = {
  INFO: "border-blue-500/30 bg-blue-500/10 text-blue-200",
  WARNING: "border-amber-500/30 bg-amber-500/10 text-amber-200",
  ERROR: "border-red-500/30 bg-red-500/10 text-red-200",
};

const sourceIcon = (entry: OpsLogEntry) => {
  const source = entry.source.toLowerCase();
  const action = entry.action_type.toLowerCase();
  if (entry.level === "ERROR") return <AlertTriangle size={18} className="text-red-300" />;
  if (source.includes("worker") || action.includes("worker")) return <Bot size={18} className="text-emerald-300" />;
  if (source.includes("director") || action.includes("director")) return <BrainCircuit size={18} className="text-purple-300" />;
  if (action.includes("tool")) return <Wrench size={18} className="text-cyan-300" />;
  return <Terminal size={18} className="text-gray-300" />;
};

const formatTime = (value: string) => {
  if (!value) return "unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
};

const compactJson = (value: Record<string, unknown>) => {
  const keys = Object.keys(value || {});
  if (keys.length === 0) return "";
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
};

export default function AgentLogsPage() {
  const [entries, setEntries] = useState<OpsLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [level, setLevel] = useState("all");
  const [query, setQuery] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);

  const fetchLogs = useCallback(async () => {
    try {
      setError(null);
      const params = new URLSearchParams({ limit: "400" });
      if (level !== "all") params.set("level", level);
      if (query.trim()) params.set("q", query.trim());
      const response = await fetch(`${API}/api/logs/ops?${params.toString()}`);
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${response.status}`);
      }
      const data: OpsLogResponse = await response.json();
      setEntries(data.entries || []);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load logs");
    } finally {
      setLoading(false);
    }
  }, [level, query]);

  useEffect(() => {
    fetchLogs();
    if (!autoRefresh) return;
    const timer = window.setInterval(fetchLogs, 2500);
    return () => window.clearInterval(timer);
  }, [autoRefresh, fetchLogs]);

  useEffect(() => {
    const timer = window.setTimeout(fetchLogs, 350);
    return () => window.clearTimeout(timer);
  }, [fetchLogs]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [entries.length]);

  const stats = useMemo(() => {
    return {
      total: entries.length,
      errors: entries.filter((entry) => entry.level === "ERROR").length,
      warnings: entries.filter((entry) => entry.level === "WARNING").length,
      workers: entries.filter((entry) => entry.source.toLowerCase().includes("worker") || entry.action_type.toLowerCase().includes("worker")).length,
    };
  }, [entries]);

  return (
    <div className="space-y-6 animate-slide-up">
      <div className="flex flex-col xl:flex-row xl:items-center justify-between gap-4">
        <div>
          <h2 className="text-3xl font-bold text-white flex items-center gap-3">
            <BrainCircuit className="text-purple-400" />
            Agent Logs
          </h2>
          <div className="text-sm text-gray-500 mt-1">
            Live operational chat between Director, Workers, tools, and backend errors.
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          <button
            onClick={() => setAutoRefresh((value) => !value)}
            className={`px-4 py-2 rounded-xl border flex items-center gap-2 transition-colors ${
              autoRefresh
                ? "bg-emerald-600/20 border-emerald-500/30 text-emerald-200"
                : "bg-gray-900 border-gray-800 text-gray-400 hover:text-white"
            }`}
          >
            <RefreshCw size={18} className={autoRefresh ? "animate-spin" : ""} />
            Auto refresh
          </button>
          <button
            onClick={fetchLogs}
            className="px-4 py-2 rounded-xl bg-gray-900 hover:bg-gray-800 border border-gray-800 text-gray-200 flex items-center gap-2 transition-colors"
          >
            <RefreshCw size={18} /> Refresh
          </button>
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat label="Events" value={stats.total.toString()} accent="text-blue-300" />
        <Stat label="Worker events" value={stats.workers.toString()} accent="text-emerald-300" />
        <Stat label="Warnings" value={stats.warnings.toString()} accent="text-amber-300" />
        <Stat label="Errors" value={stats.errors.toString()} accent="text-red-300" />
      </div>

      <div className="bg-gray-900/70 border border-gray-800 rounded-2xl p-4 flex flex-col md:flex-row gap-3">
        <div className="relative flex-1">
          <Search size={18} className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search messages, errors, actions..."
            className="w-full bg-gray-950 border border-gray-800 rounded-xl pl-10 pr-4 py-3 text-white placeholder-gray-600 focus:outline-none focus:border-purple-500"
          />
        </div>
        <select
          value={level}
          onChange={(event) => setLevel(event.target.value)}
          className="bg-gray-950 border border-gray-800 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-purple-500"
        >
          <option value="all">All levels</option>
          <option value="INFO">Info</option>
          <option value="WARNING">Warnings</option>
          <option value="ERROR">Errors</option>
        </select>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-200 rounded-2xl p-4 flex items-start gap-3">
          <AlertTriangle className="mt-0.5" size={20} />
          <div>
            <div className="font-semibold">Cannot load operational logs</div>
            <div className="text-sm text-red-200/80 mt-1">{error}</div>
          </div>
        </div>
      )}

      <section className="bg-gray-900/70 border border-gray-800 rounded-2xl overflow-hidden">
        <div className="border-b border-gray-800 px-5 py-4 flex items-center justify-between">
          <div>
            <h3 className="text-lg font-semibold text-white">Director / Worker conversation stream</h3>
            <p className="text-xs text-gray-500 mt-1">Newest entries are at the bottom, like a chat.</p>
          </div>
          {loading && <div className="text-sm text-gray-500">Loading...</div>}
        </div>

        <div className="max-h-[calc(100vh-360px)] min-h-[420px] overflow-y-auto p-5 space-y-4">
          {!loading && entries.length === 0 ? (
            <div className="h-64 flex flex-col items-center justify-center text-center text-gray-500">
              <Terminal size={36} className="mb-3" />
              <div className="font-medium text-gray-300">No operational logs yet</div>
              <div className="text-sm mt-1">Start a chat or Board task to see Director/Worker thoughts and errors here.</div>
            </div>
          ) : (
            entries.map((entry) => {
              const payload = compactJson(entry.payload);
              const style = levelStyles[entry.level] || "border-gray-700 bg-gray-800/60 text-gray-200";
              return (
                <article key={entry.id} className={`border rounded-2xl p-4 ${style}`}>
                  <div className="flex items-start gap-3">
                    <div className="mt-1">{sourceIcon(entry)}</div>
                    <div className="flex-1 min-w-0">
                      <div className="flex flex-wrap items-center gap-2 text-xs text-gray-400">
                        <span className="font-semibold text-gray-200">{entry.source || "Backend"}</span>
                        <span>{formatTime(entry.timestamp)}</span>
                        {entry.action_type && <span className="px-2 py-0.5 rounded-full bg-gray-950/60 border border-gray-700">{entry.action_type}</span>}
                        {entry.status && (
                          <span className="px-2 py-0.5 rounded-full bg-gray-950/60 border border-gray-700 flex items-center gap-1">
                            {entry.status === "success" && <CheckCircle2 size={12} className="text-emerald-300" />}
                            {entry.status}
                          </span>
                        )}
                        {entry.task_id && <span className="text-gray-500">task {entry.task_id.slice(0, 8)}</span>}
                      </div>

                      <div className="mt-2 whitespace-pre-wrap break-words text-sm leading-relaxed text-gray-100">
                        {entry.message || "(empty message)"}
                      </div>

                      {entry.error_details && (
                        <pre className="mt-3 bg-red-950/40 border border-red-500/20 rounded-xl p-3 text-xs text-red-100 overflow-x-auto whitespace-pre-wrap">
                          {entry.error_details}
                        </pre>
                      )}

                      {payload && (
                        <details className="mt-3 group">
                          <summary className="cursor-pointer text-xs text-gray-400 hover:text-white">Show payload / model output</summary>
                          <pre className="mt-2 bg-gray-950/70 border border-gray-800 rounded-xl p-3 text-xs text-gray-300 overflow-x-auto whitespace-pre-wrap">
                            {payload}
                          </pre>
                        </details>
                      )}
                    </div>
                  </div>
                </article>
              );
            })
          )}
          <div ref={bottomRef} />
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, accent }: { label: string; value: string; accent: string }) {
  return (
    <div className="bg-gray-900/70 border border-gray-800 rounded-2xl p-4">
      <div className="text-xs uppercase tracking-wide text-gray-500">{label}</div>
      <div className={`text-2xl font-bold mt-1 ${accent}`}>{value}</div>
    </div>
  );
}
