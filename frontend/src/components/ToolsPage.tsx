"use client";

import { useEffect, useState } from "react";
import { Wrench, Check, X, Globe, FileText, Code, Play, Terminal, Shield } from "lucide-react";

interface MCPTool {
  id: string;
  name: string;
  description: string;
  command: string;
  args_schema: Record<string, unknown>;
  category: string;
  enabled: boolean;
}

interface ExecuteResult {
  tool_id: string;
  command: string;
  stdout: string;
  stderr: string;
  returncode: number;
  success: boolean;
}

const categoryIcons: Record<string, React.ReactNode> = {
  web: <Globe size={16} className="text-blue-400" />,
  file: <FileText size={16} className="text-amber-400" />,
  dev: <Code size={16} className="text-purple-400" />,
  general: <Wrench size={16} className="text-gray-400" />,
  pentest: <Shield size={16} className="text-red-400" />,
  code: <Code size={16} className="text-emerald-400" />,
  security: <Shield size={16} className="text-orange-400" />,
  git: <Code size={16} className="text-cyan-400" />,
  docker: <Code size={16} className="text-sky-400" />,
  test: <Check size={16} className="text-lime-400" />,
};

const categoryColors: Record<string, string> = {
  web: "bg-blue-500/10 text-blue-400 border-blue-500/20",
  file: "bg-amber-500/10 text-amber-400 border-amber-500/20",
  dev: "bg-purple-500/10 text-purple-400 border-purple-500/20",
  general: "bg-gray-500/10 text-gray-400 border-gray-500/20",
  pentest: "bg-red-500/10 text-red-400 border-red-500/20",
  code: "bg-emerald-500/10 text-emerald-400 border-emerald-500/20",
  security: "bg-orange-500/10 text-orange-400 border-orange-500/20",
  git: "bg-cyan-500/10 text-cyan-400 border-cyan-500/20",
  docker: "bg-sky-500/10 text-sky-400 border-sky-500/20",
  test: "bg-lime-500/10 text-lime-400 border-lime-500/20",
};

export default function ToolsPage() {
  const [tools, setTools] = useState<MCPTool[]>([]);
  const [loading, setLoading] = useState(true);
  const [executing, setExecuting] = useState<string | null>(null);
  const [result, setResult] = useState<ExecuteResult | null>(null);
  const [args, setArgs] = useState<Record<string, string>>({});

  useEffect(() => {
    fetch("http://127.0.0.1:8001/api/mcp-tools")
      .then((r) => r.json())
      .then((data) => {
        setTools(data);
        setLoading(false);
      })
      .catch(() => setLoading(false));
  }, []);

  const handleExecute = async (tool: MCPTool) => {
    setExecuting(tool.id);
    setResult(null);
    try {
      const res = await fetch(`http://127.0.0.1:8001/api/mcp-tools/${tool.id}/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ args: args[tool.id] ? JSON.parse(args[tool.id]) : {} }),
      });
      const data = await res.json();
      setResult(data);
    } catch {
      // ignore
    } finally {
      setExecuting(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 text-blue-500" />
      </div>
    );
  }

  return (
    <div className="space-y-6 animate-slide-up">
      <div>
        <h2 className="text-3xl font-bold text-white">MCP Tools</h2>
        <p className="text-gray-400 mt-1">Available skills and integrations for agents</p>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {tools.map((tool) => (
          <div
            key={tool.id}
            className="bg-gray-900 border border-gray-800 rounded-2xl p-5 hover:border-gray-700 transition-all duration-200"
          >
            <div className="flex items-start justify-between mb-3">
              <div className="flex items-center gap-3">
                <div className="w-10 h-10 rounded-lg bg-gray-800 flex items-center justify-center">
                  {categoryIcons[tool.category] || <Wrench size={16} className="text-gray-400" />}
                </div>
                <div>
                  <h3 className="font-semibold text-white">{tool.name}</h3>
                  <span className={`text-xs px-1.5 py-0.5 rounded border ${categoryColors[tool.category] || categoryColors.general}`}>
                    {tool.category}
                  </span>
                </div>
              </div>
              {tool.enabled ? (
                <div className="flex items-center gap-1 text-xs text-emerald-400 bg-emerald-500/10 px-2 py-1 rounded-lg border border-emerald-500/20">
                  <Check size={12} />
                  Enabled
                </div>
              ) : (
                <div className="flex items-center gap-1 text-xs text-gray-400 bg-gray-500/10 px-2 py-1 rounded-lg border border-gray-500/20">
                  <X size={12} />
                  Disabled
                </div>
              )}
            </div>

            <p className="text-sm text-gray-400 mb-4">{tool.description}</p>

            <div className="space-y-2 mb-4">
              <div className="text-xs text-gray-500">
                <span className="font-medium">Command:</span> <code className="text-gray-300">{tool.command}</code>
              </div>
              {tool.args_schema && Object.keys(tool.args_schema).length > 0 && (
                <div className="text-xs text-gray-500">
                  <span className="font-medium">Args Schema:</span>{" "}
                  <code className="text-gray-300">{JSON.stringify(tool.args_schema)}</code>
                </div>
              )}
            </div>

            {/* Execute section */}
            {tool.enabled && (
              <div className="space-y-2">
                {tool.args_schema && Object.keys(tool.args_schema).length > 0 && (
                  <input
                    type="text"
                    placeholder='{"file": "main.py"}'
                    value={args[tool.id] || ""}
                    onChange={(e) => setArgs({ ...args, [tool.id]: e.target.value })}
                    className="w-full bg-gray-950 border border-gray-700 rounded-xl px-3 py-2 text-xs text-white focus:outline-none focus:border-blue-500"
                  />
                )}
                <button
                  onClick={() => handleExecute(tool)}
                  disabled={executing === tool.id}
                  className="w-full bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/30 py-2 rounded-xl flex items-center justify-center gap-2 transition-colors text-sm"
                >
                  {executing === tool.id ? (
                    <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-blue-400" />
                  ) : (
                    <Play size={14} />
                  )}
                  Run
                </button>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* Result panel */}
      {result && (
        <div className="bg-gray-900 border border-gray-800 rounded-2xl p-6 animate-slide-up">
          <div className="flex items-center gap-2 mb-4">
            <Terminal size={18} className="text-blue-400" />
            <h3 className="text-lg font-semibold text-white">Execution Result</h3>
            {result.success ? (
              <span className="text-xs bg-emerald-500/10 text-emerald-400 px-2 py-0.5 rounded border border-emerald-500/20">Success</span>
            ) : (
              <span className="text-xs bg-red-500/10 text-red-400 px-2 py-0.5 rounded border border-red-500/20">Failed</span>
            )}
          </div>
          <div className="text-xs text-gray-500 mb-2">Command: <code className="text-gray-300">{result.command}</code></div>
          {result.stdout && (
            <pre className="bg-gray-950 rounded-xl p-4 text-sm text-gray-300 overflow-auto max-h-64 mb-2">
              {result.stdout}
            </pre>
          )}
          {result.stderr && (
            <pre className="bg-gray-950 rounded-xl p-4 text-sm text-red-400 overflow-auto max-h-64">
              {result.stderr}
            </pre>
          )}
          <div className="text-xs text-gray-500 mt-2">Exit code: {result.returncode}</div>
        </div>
      )}
    </div>
  );
}
