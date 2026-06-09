"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Send, Cpu, Zap, AlertTriangle, Loader2, Trash2, ClipboardList } from "lucide-react";
import { processAIResponse, stripAIActionBlocks } from "@/lib/aiResponseInterceptor";

interface DirectorInfo {
  id: string;
  name: string;
  description: string;
  model_name: string;
  device: string;
  host: string;
  model_loaded: boolean;
  runtime: string;
}

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  created_at?: string;
}

interface BoardTaskInfo {
  id: string;
  title: string;
  description: string;
  status: string;
}

const API_BASE = "http://127.0.0.1:8001";
const CHAT_CACHE_KEY = "lmworld.director.chat.messages";

const readCachedMessages = (): ChatMessage[] => {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(CHAT_CACHE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (msg): msg is ChatMessage =>
          msg &&
          (msg.role === "user" || msg.role === "assistant") &&
          typeof msg.content === "string"
      )
      .map((msg) =>
        msg.role === "assistant"
          ? { ...msg, content: stripAIActionBlocks(msg.content) || msg.content }
          : msg
      );
  } catch {
    return [];
  }
};

const cacheMessages = (next: ChatMessage[]) => {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(CHAT_CACHE_KEY, JSON.stringify(next.slice(-120)));
};

const clearCachedMessages = () => {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(CHAT_CACHE_KEY);
};

export default function ChatPage() {
  const [director, setDirector] = useState<DirectorInfo | null>(null);
  const [loadingDirector, setLoadingDirector] = useState(true);
  const [directorError, setDirectorError] = useState<string | null>(null);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(true);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);
  const [lastBoardTasks, setLastBoardTasks] = useState<BoardTaskInfo[]>([]);

  const scrollRef = useRef<HTMLDivElement>(null);

  const replaceMessages = useCallback((next: ChatMessage[]) => {
    setMessages(next);
    cacheMessages(next);
  }, []);

  const appendMessages = (items: ChatMessage[]) => {
    setMessages((current) => {
      const nextMessages = [...current, ...items];
      cacheMessages(nextMessages);
      return nextMessages;
    });
  };

  const loadHistory = useCallback(async (cancelledRef?: { current: boolean }) => {
    setLoadingHistory(true);
    const cached = readCachedMessages();
    if (!cancelledRef?.current && cached.length > 0) {
      setMessages(cached);
    }
    try {
      const resp = await fetch(`${API_BASE}/api/chat/history`);
      if (!resp.ok) return;
      const data: { messages?: ChatMessage[] } = await resp.json();
      const backendMessages = (data.messages || []).map((msg) =>
        msg.role === "assistant"
          ? { ...msg, content: stripAIActionBlocks(msg.content) || msg.content }
          : msg
      );
      if (!cancelledRef?.current && backendMessages.length > 0) {
        replaceMessages(backendMessages);
      } else if (!cancelledRef?.current && cached.length > 0) {
        setMessages(cached);
      }
    } finally {
      if (!cancelledRef?.current) setLoadingHistory(false);
    }
  }, [replaceMessages]);

  useEffect(() => {
    const cancelledRef = { current: false };
    const load = async () => {
      setLoadingDirector(true);
      setDirectorError(null);
      try {
        const [directorResp] = await Promise.all([
          fetch(`${API_BASE}/api/chat/director`),
          loadHistory(cancelledRef),
        ]);
        if (!directorResp.ok) {
          const data = await directorResp.json().catch(() => ({}));
          throw new Error(data.detail || `HTTP ${directorResp.status}`);
        }
        const data: DirectorInfo = await directorResp.json();
        if (!cancelledRef.current) setDirector(data);
      } catch (e) {
        if (!cancelledRef.current) {
          setDirectorError(
            e instanceof Error ? e.message : "Failed to load director"
          );
        }
      } finally {
        if (!cancelledRef.current) setLoadingDirector(false);
      }
    };
    load();
    return () => {
      cancelledRef.current = true;
    };
  }, [loadHistory]);

  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages, sending]);

  const sendMessage = async () => {
    const text = input.trim();
    if (!text || sending || !director) return;
    setSendError(null);
    setLastBoardTasks([]);
    const userMsg: ChatMessage = { role: "user", content: text };
    const optimisticHistory = [...messages, userMsg];
    cacheMessages(optimisticHistory);
    appendMessages([userMsg]);
    setInput("");
    setSending(true);
    try {
      const resp = await fetch(`${API_BASE}/api/chat/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text, history: optimisticHistory }),
      });
      if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        let detail = text;
        try {
          const data = JSON.parse(text);
          detail = data.detail || data.message || text;
        } catch {
          // Keep raw response text when backend did not return JSON.
        }
        throw new Error(detail || `Backend returned HTTP ${resp.status}`);
      }
      const data: { reply: string; board_tasks?: BoardTaskInfo[]; history?: ChatMessage[] } = await resp.json();
      const intercepted = await processAIResponse(data.reply || "");
      const changedBoardTasks = [...(data.board_tasks || []), ...intercepted.boardTasks];
      setLastBoardTasks(changedBoardTasks);

      if (data.history && data.history.length > 0) {
        const cleanedHistory = data.history.map((message, index) => {
          if (message.role !== "assistant") return message;
          if (index === data.history!.length - 1) {
            return { ...message, content: intercepted.visibleText || stripAIActionBlocks(message.content) || "(empty response)" };
          }
          return { ...message, content: stripAIActionBlocks(message.content) || message.content };
        });
        replaceMessages(cleanedHistory);
      } else {
        appendMessages([{ role: "assistant", content: intercepted.visibleText || "(empty response)" }]);
      }
    } catch (e) {
      const message = e instanceof Error ? e.message : "Send failed";
      setSendError(
        message === "Failed to fetch"
          ? "Backend is unreachable. Start/restart lmWorld backend, then try again. Offline mode still requires the local backend and local model runtime to be running."
          : message
      );
    } finally {
      setSending(false);
    }
  };

  const clearContext = async () => {
    if (sending) return;
    await fetch(`${API_BASE}/api/chat/history`, { method: "DELETE" });
    replaceMessages([]);
    clearCachedMessages();
    setLastBoardTasks([]);
    setSendError(null);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  };

  const deviceBadge = (device: string) => {
    if (device === "gpu") {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-emerald-500/15 text-emerald-300 text-xs">
          <Zap className="w-3 h-3" /> GPU
        </span>
      );
    }
    if (device === "cpu") {
      return (
        <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-blue-500/15 text-blue-300 text-xs">
          <Cpu className="w-3 h-3" /> CPU
        </span>
      );
    }
    return (
      <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md bg-purple-500/15 text-purple-300 text-xs">
        AUTO
      </span>
    );
  };

  return (
    <div className="flex flex-col h-[calc(100vh-3rem)] max-w-5xl mx-auto">
      <div className="border border-gray-800 rounded-2xl bg-gray-900/60 p-4 mb-4">
        {loadingDirector ? (
          <div className="flex items-center gap-2 text-gray-400 text-sm">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading project architect…
          </div>
        ) : directorError ? (
          <div className="flex items-center gap-2 text-amber-300 text-sm">
            <AlertTriangle className="w-4 h-4" />
            {directorError}
          </div>
        ) : director ? (
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-2">
                <div className="text-base font-semibold text-white truncate">
                  {director.name} · Project Architect
                </div>
                {deviceBadge(director.device)}
                <span
                  className={`text-xs px-2 py-0.5 rounded-md ${
                    director.runtime === "llamacpp"
                      ? "bg-blue-500/15 text-blue-300"
                      : "bg-purple-500/15 text-purple-300"
                  }`}
                  title={
                    director.runtime === "llamacpp"
                      ? "Running via llama-server (llama.cpp)"
                      : "Running via Ollama"
                  }
                >
                  {director.runtime === "llamacpp" ? "llama.cpp" : "ollama"}
                </span>
                <span
                  className={`text-xs px-2 py-0.5 rounded-md ${
                    director.model_loaded
                      ? "bg-green-500/15 text-green-300"
                      : "bg-yellow-500/15 text-yellow-300"
                  }`}
                  title={director.host}
                >
                  {director.model_loaded ? "model loaded" : "warming up…"}
                </span>
                <span className="text-xs px-2 py-0.5 rounded-md bg-cyan-500/15 text-cyan-300">
                  persistent context
                </span>
              </div>
              <div className="text-xs text-gray-400 mt-1 truncate">
                {director.model_name} · {director.host}
              </div>
              <p className="text-xs text-gray-500 mt-2 max-w-3xl">
                Discuss the project here. When you give a real task, the Director will plan it, propose project structure, and push tasks/subtasks to the Board automatically.
              </p>
            </div>
            <button
              onClick={clearContext}
              disabled={sending || messages.length === 0}
              className="shrink-0 inline-flex items-center gap-1 text-xs bg-gray-950 hover:bg-gray-800 disabled:opacity-40 disabled:hover:bg-gray-950 border border-gray-800 text-gray-300 rounded-lg px-3 py-2"
              title="Clear saved chat context only"
            >
              <Trash2 className="w-3 h-3" /> Clear context
            </button>
          </div>
        ) : null}
      </div>

      {lastBoardTasks.length > 0 && (
        <div className="mb-3 border border-amber-500/25 bg-amber-500/10 rounded-2xl p-3 text-sm text-amber-100">
          <div className="flex items-center gap-2 font-medium">
            <ClipboardList className="w-4 h-4" /> Board updated: {lastBoardTasks.length} item{lastBoardTasks.length === 1 ? "" : "s"} changed
          </div>
          <div className="mt-2 flex flex-wrap gap-2">
            {lastBoardTasks.slice(0, 6).map((task) => (
              <span key={task.id} className="text-xs bg-gray-950/70 border border-amber-500/20 rounded-lg px-2 py-1">
                {task.title}
              </span>
            ))}
          </div>
        </div>
      )}

      <div ref={scrollRef} className="flex-1 overflow-y-auto pr-1 space-y-3">
        {loadingHistory ? (
          <div className="h-full flex items-center justify-center text-gray-500 text-sm gap-2">
            <Loader2 className="w-4 h-4 animate-spin" /> Loading saved context…
          </div>
        ) : messages.length === 0 && !sending ? (
          <div className="h-full flex items-center justify-center text-center text-gray-500 text-sm px-6">
            <div>
              <div className="text-white font-semibold mb-2">Start with a project goal.</div>
              <div className="max-w-xl">
                Example: “We are building a calculator app. Plan the architecture and split the work into subtasks.”
              </div>
            </div>
          </div>
        ) : (
          messages.map((msg, i) => (
            <div key={`${msg.created_at || i}-${msg.role}`} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div
                className={`max-w-[82%] rounded-2xl px-4 py-2 text-sm whitespace-pre-wrap break-words ${
                  msg.role === "user"
                    ? "bg-cyan-600 text-white"
                    : "bg-gray-800 text-gray-100"
                }`}
              >
                {msg.content}
              </div>
            </div>
          ))
        )}
        {sending && (
          <div className="flex justify-start">
            <div className="bg-gray-800 text-gray-300 rounded-2xl px-4 py-2 text-sm flex items-center gap-2">
              <Loader2 className="w-3 h-3 animate-spin" /> architect is planning…
            </div>
          </div>
        )}
      </div>

      {sendError && (
        <div className="mt-2 text-xs text-red-400 flex items-center gap-1">
          <AlertTriangle className="w-3 h-3" />
          {sendError}
        </div>
      )}

      <div className="mt-3 border border-gray-800 rounded-2xl bg-gray-900/60 p-2 flex items-end gap-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          rows={1}
          disabled={!director || sending}
          placeholder={
            director
              ? `Describe a project/task for ${director.name}… (Enter to send, Shift+Enter for newline)`
              : "Waiting for director…"
          }
          className="flex-1 bg-transparent resize-none outline-none text-sm text-gray-100 placeholder-gray-500 px-2 py-2 max-h-40"
        />
        <button
          onClick={sendMessage}
          disabled={!director || sending || !input.trim()}
          className="shrink-0 inline-flex items-center gap-1 bg-cyan-600 hover:bg-cyan-500 disabled:bg-gray-700 disabled:text-gray-400 text-white text-sm font-medium rounded-xl px-3 py-2"
        >
          {sending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          Send
        </button>
      </div>
    </div>
  );
}
