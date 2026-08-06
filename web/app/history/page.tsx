"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { getSupabase } from "@/lib/supabase";
import type { Signal } from "@/lib/types";
import SignalBadge from "@/components/SignalBadge";

export default function HistoryPage() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [loading, setLoading] = useState(true);
  const [page, setPage] = useState(0);
  const PER = 50;

  useEffect(() => {
    getSupabase()
      .from("stock_signal_log")
      .select("*")
      .neq("signal_type", "보유")
      .order("alerted_at", { ascending: false })
      .range(page * PER, page * PER + PER - 1)
      .then(({ data }) => {
        setSignals((data ?? []) as Signal[]);
        setLoading(false);
      });
  }, [page]);

  return (
    <div>
      <h1 className="text-xl font-bold text-white mb-6">신호 히스토리</h1>

      <div className="bg-gray-800 rounded-xl border border-gray-700 overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-700 text-gray-400 text-xs">
              {["시간(KST)","종목","신호","점수","가격","RSI","거래량","외국인","기관"].map(h => (
                <th key={h} className="px-3 py-3 text-left">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr><td colSpan={9} className="text-center py-12 text-gray-500">로딩 중…</td></tr>
            ) : signals.length === 0 ? (
              <tr><td colSpan={9} className="text-center py-12 text-gray-500">데이터 없음</td></tr>
            ) : signals.map(s => {
              const isBuy = s.signal_type === "매수" || s.signal_type === "강한 매수";
              return (
                <tr key={s.id} className="border-b border-gray-700/50 hover:bg-gray-700/30 transition-colors">
                  <td className="px-3 py-2.5 text-gray-400 text-xs whitespace-nowrap">
                    {new Date(s.alerted_at).toLocaleString("ko-KR", {
                      month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"
                    })}
                  </td>
                  <td className="px-3 py-2.5">
                    <Link href={`/stocks/${s.ticker}`}
                      className="font-medium text-white hover:text-blue-400 transition-colors">
                      {s.name || s.ticker}
                    </Link>
                    <span className="ml-1 text-xs text-gray-500">{s.ticker}</span>
                  </td>
                  <td className="px-3 py-2.5"><SignalBadge type={s.signal_type} /></td>
                  <td className={`px-3 py-2.5 font-mono font-bold ${isBuy?"text-green-400":"text-red-400"}`}>
                    {s.score >= 0 ? "+" : ""}{s.score?.toFixed(3)}
                  </td>
                  <td className="px-3 py-2.5 font-mono text-white">
                    {s.current_price?.toLocaleString()}원
                  </td>
                  <td className="px-3 py-2.5 text-gray-300">{s.rsi?.toFixed(1) ?? "-"}</td>
                  <td className="px-3 py-2.5 text-gray-300">
                    {s.volume_ratio != null ? `${s.volume_ratio.toFixed(1)}x` : "-"}
                  </td>
                  <td className={`px-3 py-2.5 font-mono text-xs ${(s.foreign_net ?? 0) > 0 ? "text-green-400" : "text-red-400"}`}>
                    {s.foreign_net != null ? `${s.foreign_net > 0 ? "+" : ""}${s.foreign_net.toLocaleString()}` : "-"}
                  </td>
                  <td className={`px-3 py-2.5 font-mono text-xs ${(s.institution_net ?? 0) > 0 ? "text-green-400" : "text-red-400"}`}>
                    {s.institution_net != null ? `${s.institution_net > 0 ? "+" : ""}${s.institution_net.toLocaleString()}` : "-"}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* 페이지네이션 */}
      <div className="flex justify-center gap-3 mt-4">
        <button disabled={page === 0} onClick={() => setPage(p => p - 1)}
          className="px-4 py-1.5 rounded bg-gray-700 text-gray-300 disabled:opacity-40 hover:bg-gray-600 transition-colors">
          이전
        </button>
        <span className="text-gray-400 self-center text-sm">{page + 1}페이지</span>
        <button disabled={signals.length < PER} onClick={() => setPage(p => p + 1)}
          className="px-4 py-1.5 rounded bg-gray-700 text-gray-300 disabled:opacity-40 hover:bg-gray-600 transition-colors">
          다음
        </button>
      </div>
    </div>
  );
}
