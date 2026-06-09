"use client";

import { useEffect, useState, useCallback } from "react";
import {
  Cpu,
  Activity,
  Zap,
  ArrowLeft,
  Save,
  Trash2,
  X,
  Wrench,
  Calendar,
  BarChart3,
} from "lucide-react";

interface Agent {
  id: number;
  name: string;
  role: string;
  model_name: string;
  model_provider: string;
  device: string;
  status: string;
  auto_accept: boolean;
  mcp_tools: string[];
  tasks_completed: number;
  created_at: string;
  last_active: string | null;
  system_prompt: string;
  is_director: boolean;
}

interface Task {
  id: number;
  title: string;
  status: string;
  created_at: string;
  assigned_agent_id: number | null;
}

interface AgentProfileProps {
  agentId: number;
  onBack: () => void;
}

export default function AgentProfile({ agentId, onBack }: AgentProfileProps) {
  const [agent, setAgent] = useState<Agent | null>(null);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [form, setForm] = useState<Partial<Agent>>({});

  const fetchAgent = useCallback(async () => {
    try {
      const res = await fetch(`http://127.0.0.1:8001/api/agents/${agentId}`);
      const data = await res.json();
      setAgent(data);
      setForm(data);

      const tasksRes = await fetch("http://127.0.0.1:8001/api/tasks");
      const allTasks = await tasksRes.json();
      setTasks(allTasks.filter((t: Task) => t.assigned_agent_id === agentId));
    } catch {
      // ignore
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    fetchAgent();
  }, [fetchAgent]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const res = await fetch(`http://127.0.0.1:8001/api/agents/${agentId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      if (res.ok) {
        const updated = await res.json();
        setAgent(updated);
        setEditing(false);
      }
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm("Are you sure you want to fire this agent?")) return;
    await fetch(`http://127.0.0.1:8001/api/agents/${agentId}`, {
      method: "DELETE",
    });
    onBack();
  };

  const getStatusColor = (status: string) => {
    switch (status) {
      case "available":
      case "idle":
        return "bg-emerald-500";
      case "busy":
        return "bg-amber-500";
      case "offline":
        return "bg-red-500";
      default:
        return "bg-gray-500";
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500" />
      </div>
    );
  }

  if (!agent) {
    return (
      <div className="text-center text-gray-400 py-12">
        Agent not found
        <button
          onClick={onBack}
          className="block mx-auto mt-4 text-blue-400 hover:text-blue-300"
        >
          Go back
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-slide-up">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <button
            onClick={onBack}
            className="p-2 bg-gray-900 border border-gray-800 rounded-xl hover:border-gray-700 transition-colors"
          >
            <ArrowLeft size={20} className="text-gray-400" />
          </button>
          <div>
            <h2 className="text-3xl font-bold text-white">{agent.name}</h2>
            <div className="flex items-center gap-2 mt-1">
              <div className={`w-2 h-2 rounded-full ${getStatusColor(agent.status)} animate-pulse`} />
              <span className="text-sm text-gray-400 capitalize">{agent.status}</span>
              {agent.role === "director" && (
                <span className="text-xs bg-yellow-500/10 text-yellow-400 px-2 py-0.5 rounded border border-yellow-500/20">
                  Director
                </span>
              )}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {editing ? (
            <>
              <button
                onClick={handleSave}
                disabled={saving}
                className="bg-emerald-500 hover:bg-emerald-600 text-white px-4 py-2 rounded-xl flex items-center gap-2 transition-colors"
              >
                {saving ? (
                  <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white" />
                ) : (
                  <Save size={16} />
                )}
                Save
              </button>
              <button
                onClick={() => {
                  setEditing(false);
                  setForm(agent);
                }}
                className="bg-gray-800 hover:bg-gray-700 text-white px-4 py-2 rounded-xl flex items-center gap-2 transition-colors"
              >
                <X size={16} />
                Cancel
              </button>
            </>
          ) : (
            <button
              onClick={() => setEditing(true)}
              className="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-xl transition-colors"
            >
              Edit Profile
            </button>
          )}
          <button
            onClick={handleDelete}
            className="bg-red-500/10 hover:bg-red-500/20 text-red-400 border border-red-500/30 px-4 py-2 rounded-xl flex items-center gap-2 transition-colors"
          >
            <Trash2 size={16} />
            Fire
          </button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main Info */}
        <div className="lg:col-span-2 space-y-6">
          {/* Avatar + Quick Stats */}
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6">
            <div className="flex items-start gap-6">
              <div className="w-20 h-20 rounded-2xl bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white font-bold text-3xl">
                {agent.name.charAt(0).toUpperCase()}
              </div>
              <div className="flex-1 grid grid-cols-3 gap-4">
                <div className="bg-gray-950 rounded-xl p-4 text-center">
                  <BarChart3 size={20} className="text-blue-400 mx-auto mb-1" />
                  <p className="text-2xl font-bold text-white">{agent.tasks_completed || 0}</p>
                  <p className="text-xs text-gray-500">Tasks Done</p>
                </div>
                <div className="bg-gray-950 rounded-xl p-4 text-center">
                  <Calendar size={20} className="text-purple-400 mx-auto mb-1" />
                  <p className="text-sm font-bold text-white">
                    {new Date(agent.created_at).toLocaleDateString()}
                  </p>
                  <p className="text-xs text-gray-500">Hired</p>
                </div>
                <div className="bg-gray-950 rounded-xl p-4 text-center">
                  <Activity size={20} className="text-emerald-400 mx-auto mb-1" />
                  <p className="text-sm font-bold text-white">
                    {agent.last_active
                      ? new Date(agent.last_active).toLocaleDateString()
                      : "Never"}
                  </p>
                  <p className="text-xs text-gray-500">Last Active</p>
                </div>
              </div>
            </div>
          </div>

          {/* Details Form */}
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6 space-y-5">
            <h3 className="text-lg font-semibold text-white">Configuration</h3>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
              <div className="space-y-2">
                <label className="text-sm font-medium text-gray-400">Name</label>
                {editing ? (
                  <input
                    type="text"
                    value={form.name || ""}
                    onChange={(e) => setForm({ ...form, name: e.target.value })}
                    className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-blue-500"
                  />
                ) : (
                  <p className="text-white">{agent.name}</p>
                )}
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium text-gray-400">Role</label>
                {editing ? (
                  <select
                    value={form.role || "worker"}
                    onChange={(e) => setForm({ ...form, role: e.target.value })}
                    className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-blue-500"
                  >
                    <option value="worker">Worker</option>
                    <option value="director">Director</option>
                  </select>
                ) : (
                  <div className="flex items-center gap-2">
                    {agent.role === "director" ? (
                      <Zap size={16} className="text-yellow-400" />
                    ) : (
                      <Cpu size={16} className="text-blue-400" />
                    )}
                    <span className="text-white capitalize">{agent.role}</span>
                  </div>
                )}
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium text-gray-400">Model</label>
                {editing ? (
                  <input
                    type="text"
                    value={form.model_name || ""}
                    onChange={(e) => setForm({ ...form, model_name: e.target.value })}
                    className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-blue-500"
                  />
                ) : (
                  <div className="flex items-center gap-2">
                    <Cpu size={16} className="text-blue-400" />
                    <span className="text-white">{agent.model_name}</span>
                  </div>
                )}
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium text-gray-400">Provider</label>
                {editing ? (
                  <select
                    value={form.model_provider || "ollama"}
                    onChange={(e) => setForm({ ...form, model_provider: e.target.value })}
                    className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-blue-500"
                  >
                    <option value="ollama">Ollama</option>
                    <option value="huggingface">HuggingFace</option>
                    <option value="openai">OpenAI</option>
                  </select>
                ) : (
                  <span className="text-white capitalize">{agent.model_provider}</span>
                )}
              </div>

              <div className="space-y-2">
                <label className="text-sm font-medium text-gray-400">Device</label>
                {editing ? (
                  <select
                    value={form.device || "auto"}
                    onChange={(e) => setForm({ ...form, device: e.target.value })}
                    className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-blue-500"
                  >
                    <option value="auto">Auto</option>
                    <option value="gpu">GPU</option>
                    <option value="cpu">CPU</option>
                  </select>
                ) : (
                  <span className={`inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs font-medium ${
                    agent.device === "gpu"
                      ? "bg-purple-500/10 text-purple-400 border border-purple-500/20"
                      : agent.device === "cpu"
                      ? "bg-orange-500/10 text-orange-400 border border-orange-500/20"
                      : "bg-blue-500/10 text-blue-400 border border-blue-500/20"
                  }`}>
                    {agent.device === "gpu" ? "GPU" : agent.device === "cpu" ? "CPU" : "Auto"}
                  </span>
                )}
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-medium text-gray-400">System Prompt</label>
              {editing ? (
                <textarea
                  value={form.system_prompt || ""}
                  onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
                  rows={4}
                  className="w-full bg-gray-950 border border-gray-700 rounded-xl px-4 py-3 text-white focus:outline-none focus:border-blue-500 resize-none"
                />
              ) : (
                <p className="text-gray-300 bg-gray-950 rounded-xl p-4">
                  {agent.system_prompt || "No system prompt configured."}
                </p>
              )}
            </div>

            <div className="flex items-center gap-3">
              <div
                className={`w-12 h-6 rounded-full transition-colors relative ${
                  form.auto_accept ? "bg-blue-500" : "bg-gray-700"
                }`}
              >
                <div
                  className={`w-5 h-5 rounded-full bg-white absolute top-0.5 transition-transform ${
                    form.auto_accept ? "translate-x-6" : "translate-x-0.5"
                  }`}
                />
              </div>
              <span className="text-sm text-gray-300">Auto-accept tasks</span>
            </div>
          </div>

          {/* Tasks History */}
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6">
            <h3 className="text-lg font-semibold text-white mb-4">Recent Tasks</h3>
            {tasks.length === 0 ? (
              <p className="text-gray-500 text-center py-8">No tasks assigned yet</p>
            ) : (
              <div className="space-y-3">
                {tasks.map((task) => (
                  <div
                    key={task.id}
                    className="bg-gray-950 rounded-xl p-4 flex items-center justify-between"
                  >
                    <div>
                      <p className="text-white font-medium">{task.title}</p>
                      <p className="text-xs text-gray-500">
                        {new Date(task.created_at).toLocaleDateString()}
                      </p>
                    </div>
                    <span
                      className={`text-xs px-2 py-1 rounded border ${
                        task.status === "done"
                          ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                          : task.status === "in_progress"
                          ? "bg-amber-500/10 text-amber-400 border-amber-500/20"
                          : "bg-gray-500/10 text-gray-400 border-gray-500/20"
                      }`}
                    >
                      {task.status}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Sidebar */}
        <div className="space-y-6">
          {/* MCP Tools */}
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6">
            <div className="flex items-center gap-2 mb-4">
              <Wrench size={18} className="text-purple-400" />
              <h3 className="text-lg font-semibold text-white">MCP Tools</h3>
            </div>
            {agent.mcp_tools && agent.mcp_tools.length > 0 ? (
              <div className="flex flex-wrap gap-2">
                {agent.mcp_tools.map((tool) => (
                  <span
                    key={tool}
                    className="px-3 py-1.5 bg-blue-500/10 text-blue-400 text-sm rounded-lg border border-blue-500/20"
                  >
                    {tool}
                  </span>
                ))}
              </div>
            ) : (
              <p className="text-gray-500 text-sm">No tools assigned</p>
            )}
          </div>

          {/* Actions */}
          <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6 space-y-3">
            <h3 className="text-lg font-semibold text-white">Actions</h3>
            <button
              onClick={async () => {
                await fetch(`http://127.0.0.1:8001/api/agents/${agentId}/hire`, {
                  method: "POST",
                });
                fetchAgent();
              }}
              className="w-full bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 border border-emerald-500/30 py-2 rounded-xl transition-colors"
            >
              Hire / Activate
            </button>
            <button
              onClick={async () => {
                await fetch(`http://127.0.0.1:8001/api/agents/${agentId}/fire`, {
                  method: "POST",
                });
                fetchAgent();
              }}
              className="w-full bg-red-500/10 hover:bg-red-500/20 text-red-400 border border-red-500/30 py-2 rounded-xl transition-colors"
            >
              Fire / Deactivate
            </button>
            {!agent.is_director && (
              <button
                onClick={async () => {
                  await fetch(`http://127.0.0.1:8001/api/agents/${agentId}/director`, {
                    method: "POST",
                  });
                  fetchAgent();
                }}
                className="w-full bg-yellow-500/10 hover:bg-yellow-500/20 text-yellow-400 border border-yellow-500/30 py-2 rounded-xl transition-colors"
              >
                Promote to Director
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
