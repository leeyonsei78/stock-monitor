"use client";
import { useEffect, useState } from "react";
import { getSupabase } from "@/lib/supabase";
import type { Signal } from "@/lib/types";
import SignalCard from "@/components/SignalCard";
import TradeModal from "@/components/TradeModal";

const FILTERS = ["전체", "매수", "매도"] as const;
type Filter = typeof FILTERS[number];

export default function Dashboard() {
  const [signals, setSignals] = useState<Signal[]>([]);
  const [filter, setFilter]   = useState<Filter>("전체");
  const [loading, setLoading] = useState(true);
  const [modal, setModal] = useState<{ signal: Signal; side: "buy" | "sell" } | null>(null);

  // 초기 데이터 로드
  useEffect(() => {
    const sb = getSupabase();
    sb.from("stock_signal_log")
      .select("*")
      .neq("signal_type", "보유")
      .order("alerted_at", { ascending: false })
      .limit(60)
      .then(({ data }) => {
        setSignals((data ?? []) as Signal[]);
        setLoading(false);
      });

    // Realtime 구독
    const ch = sb.channel("dashboard-signals")
      .on(
        "postgres_changes",
        { event: "INSERT", schema: "public", table: "stock_signal_log" },
        (payload) => {
          const s = payload.new as Signal;
          if (s.signal_type !== "보유") {
            setSignals(prev => [s, ...prev].slice(0, 100));
          }
        }
      )
      .subscribe();

    return () => { sb.removeChannel(ch); };
  }, []);

  const filtered = signals.filter(s => {
    if (filter === "매수") return s.signal_type === "매수" || s.signal_type === "강한 매수";
    if (filter === "매도") return s.signal_type === "매도" || s.signal_type === "강한 매도";
    return true;
  });

  const buyCount  = signals.filter(s => s.signal_type === "매수" || s.signal_type === "강한 매수").length;
  const sellCount = signals.filter(s => s.signal_type === "매도" || s.signal_type === "강한 매도").length;

  return (
    <>
      {/* 요약 배너 */}
      <div className="grid grid-cols-3 gap-4 mb-6">
        <StatCard label="전체 신호" value={signals.length} color="text-white" />
        <StatCard label="매수 신호" value={buyCount}  color="text-green-400" />
        <StatCard label="매도 신호" value={sellCount} color="text-red-400" />
      </div>

      {/* 필터 탭 */}
      <div className="flex gap-2 mb-6">
        {FILTERS.map(f => (
          <button key={f} onClick={() => setFilter(f)}
            className={`px-4 py-1.5 rounded-full text-sm font-medium transition-colors ${
              filter === f
                ? "bg-blue-600 text-white"
                : "bg-gray-700 text-gray-300 hover:bg-gray-600"
            }`}>
            {f}
          </button>
        ))}
        <span className="ml-auto text-xs text-gray-500 self-center">
          ● Realtime 구독 중
        </span>
      </div>

      {/* 신호 카드 그리드 */}
      {loading ? (
        <div className="text-center text-gray-500 py-20">로딩 중…</div>
      ) : filtered.length === 0 ? (
        <div className="text-center text-gray-500 py-20">
          <p className="text-lg">신호 없음</p>
          <p className="text-sm mt-2">다음 스캔(30분 간격)을 기다려주세요.</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {filtered.map(s => (
            <SignalCard key={s.id} signal={s}
              onTrade={(sig, side) => setModal({ signal: sig, side })} />
          ))}
        </div>
      )}

      {/* 매매 모달 */}
      {modal && (
        <TradeModal signal={modal.signal} side={modal.side}
          onClose={() => setModal(null)} />
      )}
    </>
  );
}

function StatCard({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className="bg-gray-800 rounded-xl p-4 border border-gray-700">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className={`text-3xl font-bold ${color}`}>{value}</p>
    </div>
  );
}
