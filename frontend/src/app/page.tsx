"use client";

import { useState } from "react";
import Sidebar from "@/components/Sidebar";
import OfficePage from "@/components/OfficePage";
import HirePage from "@/components/HirePage";
import TasksPage from "@/components/TasksPage";
import ChatPage from "@/components/ChatPage";
import AgentProfile from "@/components/AgentProfile";
import SettingsPage from "@/components/SettingsPage";
import AgentLogsPage from "@/components/AgentLogsPage";

export default function Home() {
  const [currentPage, setCurrentPage] = useState<"office" | "hire" | "board" | "chat" | "logs" | "settings" | "profile">("office");
  const [selectedAgentId, setSelectedAgentId] = useState<number | null>(null);

  const handlePageChange = (page: "office" | "hire" | "board" | "chat" | "logs" | "settings") => {
    setCurrentPage(page);
    setSelectedAgentId(null);
  };

  const handleSelectAgent = (agentId: number) => {
    setSelectedAgentId(agentId);
    setCurrentPage("profile");
  };

  const handleBackFromProfile = () => {
    setCurrentPage("office");
    setSelectedAgentId(null);
  };

  return (
    <div className="flex min-h-screen bg-gray-950">
      <Sidebar currentPage={currentPage === "profile" ? "office" : currentPage} onPageChange={handlePageChange} />
      <main className="flex-1 ml-64 p-6 overflow-auto">
        {currentPage === "office" && <OfficePage onSelectAgent={handleSelectAgent} />}
        {currentPage === "hire" && <HirePage />}
        {currentPage === "board" && <TasksPage />}
        {currentPage === "chat" && <ChatPage />}
        {currentPage === "logs" && <AgentLogsPage />}
        {currentPage === "settings" && <SettingsPage />}
        {currentPage === "profile" && selectedAgentId !== null && (
          <AgentProfile agentId={selectedAgentId} onBack={handleBackFromProfile} />
        )}
      </main>
    </div>
  );
}
