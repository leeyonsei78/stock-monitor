import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";
import MarketStatus from "@/components/MarketStatus";

export const metadata: Metadata = {
  title: "주식 자동매매 대시보드",
  description: "KIS Open API 기반 실시간 신호 모니터링",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko" className="dark">
      <body className="min-h-screen bg-gray-900">
        <header className="sticky top-0 z-40 bg-gray-900/80 backdrop-blur border-b border-gray-700">
          <div className="max-w-7xl mx-auto px-4 h-14 flex items-center justify-between">
            <nav className="flex items-center gap-6 text-sm font-medium">
              <Link href="/" className="text-white font-bold text-base">📊 주식 대시보드</Link>
              <Link href="/" className="text-gray-400 hover:text-white transition-colors">대시보드</Link>
              <Link href="/history" className="text-gray-400 hover:text-white transition-colors">신호 히스토리</Link>
            </nav>
            <MarketStatus />
          </div>
        </header>
        <main className="max-w-7xl mx-auto px-4 py-6">{children}</main>
      </body>
    </html>
  );
}
