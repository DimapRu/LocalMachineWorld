import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "lmWorld - AI Agent Office",
  description: "Interactive Local AI Agent Orchestrator",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="ru">
      <body className="bg-gray-950 text-gray-100 min-h-screen">
        {children}
      </body>
    </html>
  );
}
