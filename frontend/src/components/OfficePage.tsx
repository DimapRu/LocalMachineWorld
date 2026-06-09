"use client";

import { useEffect, useState } from "react";
import { Cpu, Activity, MessageSquare, Zap } from "lucide-react";

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
}

interface OfficePageProps {
  onSelectAgent?: (agentId: number) => void;
}

export default function OfficePage({ onSelectAgent }: OfficePageProps) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("http://127.0.0.1:8001/api/agents")
      .then((r) => r.json())
      .then((data) => {
        setAgents(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  const getStatusColor = (status: string) => {
    switch (status) {
      case "available": return "bg-emerald-500";
      case "busy": return "bg-amber-500";
      case "offline": return "bg-red-500";
      default: return "bg-gray-500";
    }
  };

  const getRoleIcon = (role: string) => {
    switch (role) {
      case "director": return <Zap size={16} className="text-yellow-400" />;
      case "worker": return <Cpu size={16} className="text-blue-400" />;
      default: return <Activity size={16} className="text-gray-400" />;
    }
  };

  return (
    <div className="space-y-6 animate-slide-up">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-3xl font-bold text-white">Office</h2>
          <p className="text-gray-400 mt-1">Your AI agent team workspace</p>
        </div>
        <div className="flex items-center gap-4 bg-gray-900 rounded-xl px-4 py-2 border border-gray-800">
          <div className="flex items-center gap-2">
            <div className="w-3 h-3 rounded-full bg-emerald-500" />
            <span className="text-sm text-gray-300">{agents.filter((a) => a.status === "available").length} Available</span>
          </div>
          <div className="w-px h-4 bg-gray-700" />
          <div className="flex items-center gap-2">
            <div className="w-3 h-3 rounded-full bg-amber-500" />
            <span className="text-sm text-gray-300">{agents.filter((a) => a.status === "busy").length} Busy</span>
          </div>
          <div className="w-px h-4 bg-gray-700" />
          <div className="flex items-center gap-2">
            <div className="w-3 h-3 rounded-full bg-blue-500" />
            <span className="text-sm text-gray-300">{agents.length} Total</span>
          </div>
        </div>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-500" />
        </div>
      ) : agents.length === 0 ? (
        <div className="bg-gray-900 border border-gray-800 rounded-2xl p-12 text-center">
          <div className="w-16 h-16 bg-gray-800 rounded-full flex items-center justify-center mx-auto mb-4">
            <MessageSquare size={32} className="text-gray-500" />
          </div>
          <h3 className="text-xl font-semibold text-white mb-2">No agents yet</h3>
          <p className="text-gray-400">Hire your first AI agent to get started</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {agents.map((agent) => (
            <div
              key={agent.id}
              onClick={() => onSelectAgent?.(agent.id)}
              className="bg-gray-900 border border-gray-800 rounded-2xl p-5 hover:border-gray-700 transition-all duration-200 hover:shadow-lg hover:shadow-blue-500/5 cursor-pointer"
            >
              <div className="flex items-start justify-between mb-4">
                <div className="flex items-center gap-3">
                  <div className="w-12 h-12 rounded-xl bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center text-white font-bold text-lg">
                    {agent.name.charAt(0).toUpperCase()}
                  </div>
                  <div>
                    <h3 className="font-semibold text-white">{agent.name}</h3>
                    <div className="flex items-center gap-1 text-sm text-gray-400">
                      {getRoleIcon(agent.role)}
                      <span className="capitalize">{agent.role}</span>
                    </div>
                  </div>
                </div>
                <div className={`w-3 h-3 rounded-full ${getStatusColor(agent.status)} animate-pulse`} />
              </div>

              <div className="space-y-2">
                <div className="flex items-center gap-2 text-sm">
                  <Cpu size={14} className="text-blue-400" />
                  <span className="text-gray-400">Model:</span>
                  <span className="text-white font-medium">{agent.model_name}</span>
                </div>
                <div className="flex items-center gap-2 text-sm">
                  <Activity size={14} className="text-purple-400" />
                  <span className="text-gray-400">Provider:</span>
                  <span className="text-white font-medium capitalize">{agent.model_provider}</span>
                </div>
                <div className="flex items-center gap-2 text-sm">
                  <span className={`w-2 h-2 rounded-full ${
                    agent.device === "gpu" ? "bg-purple-400" :
                    agent.device === "cpu" ? "bg-orange-400" : "bg-blue-400"
                  }`} />
                  <span className="text-gray-400">Device:</span>
                  <span className="text-white font-medium uppercase">{agent.device}</span>
                </div>
              </div>

              {agent.mcp_tools && agent.mcp_tools.length > 0 && (
                <div className="mt-4 flex flex-wrap gap-1.5">
                  {agent.mcp_tools.map((tool) => (
                    <span
                      key={tool}
                      className="px-2 py-1 bg-blue-500/10 text-blue-400 text-xs rounded-lg border border-blue-500/20"
                    >
                      {tool}
                    </span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
