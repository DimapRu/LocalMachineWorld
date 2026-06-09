"use client";

import { Building2, UserPlus, KanbanSquare, MessageSquare, Settings, ScrollText } from "lucide-react";

type PageId = "office" | "hire" | "board" | "chat" | "logs" | "settings";

interface SidebarProps {
  currentPage: PageId;
  onPageChange: (page: PageId) => void;
}

const navItems = [
  { id: "office" as const, label: "Office", icon: Building2, color: "bg-emerald-500", hover: "hover:bg-emerald-500/20" },
  { id: "hire" as const, label: "Hire Agent", icon: UserPlus, color: "bg-blue-500", hover: "hover:bg-blue-500/20" },
  { id: "board" as const, label: "Board", icon: KanbanSquare, color: "bg-amber-500", hover: "hover:bg-amber-500/20" },
  { id: "chat" as const, label: "Chat", icon: MessageSquare, color: "bg-cyan-500", hover: "hover:bg-cyan-500/20" },
  { id: "logs" as const, label: "Agent Logs", icon: ScrollText, color: "bg-fuchsia-500", hover: "hover:bg-fuchsia-500/20" },
  { id: "settings" as const, label: "Settings", icon: Settings, color: "bg-purple-500", hover: "hover:bg-purple-500/20" },
];

export default function Sidebar({ currentPage, onPageChange }: SidebarProps) {
  return (
    <aside className="fixed left-0 top-0 h-full w-64 bg-gray-900 border-r border-gray-800 flex flex-col z-50">
      <div className="p-6 border-b border-gray-800">
        <h1 className="text-2xl font-bold bg-gradient-to-r from-blue-400 to-purple-400 bg-clip-text text-transparent">
          lmWorld
        </h1>
        <p className="text-xs text-gray-500 mt-1">AI Agent Office</p>
      </div>

      <nav className="flex-1 p-4 space-y-2">
        {navItems.map((item) => {
          const Icon = item.icon;
          const isActive = currentPage === item.id;
          return (
            <button
              key={item.id}
              onClick={() => onPageChange(item.id)}
              className={`w-full flex items-center gap-3 px-4 py-3 rounded-xl transition-all duration-200 ${
                isActive
                  ? `${item.color}/20 text-white border border-${item.color}/30`
                  : `text-gray-400 ${item.hover} hover:text-white border border-transparent`
              }`}
            >
              <div className={`w-2 h-2 rounded-full ${item.color} ${isActive ? "animate-pulse" : ""}`} />
              <Icon size={20} />
              <span className="font-medium">{item.label}</span>
            </button>
          );
        })}
      </nav>

      <div className="p-4 border-t border-gray-800">
        <div className="bg-gray-800/50 rounded-lg p-3">
          <p className="text-xs text-gray-500">Status</p>
          <div className="flex items-center gap-2 mt-1">
            <div className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
            <span className="text-sm text-green-400">Online</span>
          </div>
        </div>
      </div>
    </aside>
  );
}
