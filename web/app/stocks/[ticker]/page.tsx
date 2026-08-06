"use client";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { getSupabase } from "@/lib/supabase";
import type { OHLCVBar, Signal } from "@/lib/types";
import StockChart from "@/components/StockChart";
import SignalBadge from "@/components/SignalBadge";
import TradeModal from "@/components/TradeModal";

export default function StockPage() {
  const { ticker } = useParams<{ ticker: string }>();
  const [ohlcv,   setOhlcv]   = useState<OHLCVBar[]>([]);
  const [signals, setSignals] = useState<Signal[]>([]);
  const [modal,   setModal]   = useState<{ signal: Signal; side: "buy" | "sell" } | null>(null);
  const [loading, setLoading] = useState(true);

  const latest = signals[0];

  useEffect(() => {
    if (!ticker) return;
    const sb = getSupabase();

    // OHLCV 캐시에서 조회
    sb.from("stock_ohlcv_cache")
      .select("date,open,high,low,close,volume")
      .eq("ticker", ticker)
      .order("date", { ascending: true })
      .limit(120)
      .then(({ data }) => {
        setOhlcv((data ?? []) as OHLCVBar[]);
        setLoading(false);
      });

    // 해당 종목 신호 히스토리
    sb.from("stock_signal_log")
      .select("*")
      .eq("ticker", ticker)
      .neq("signal_type", "보유")
      .order("alerted_at", { ascending: false })
      .limit(20)
      .then(({ data }) => setSignals((data ?? []) as Signal[]));
  }, [ticker]);

  function Kv({ label, value, color = "text-white" }: { label: string; value: string; color?: string }) {
    return (
      <div className="bg-gray-700 rounded-lg px-3 py-2">
        <div className="text-[10px] text-gray-400 mb-0.5">{label}</div>
        <div className={`font-semibold text-sm ${color}`}>{value}</div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* 헤더 */}
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-2xl font-bold text-white">
            {latest?.name || ticker}
            <span className="ml-2 text-gray-400 text-base font-normal">{ticker}</span>
          </h1>
          {latest && (
            <p className="text-3xl font-bold text-white mt-1">
              {latest.current_price.toLocaleString()}원
            </p>
          )}
        </div>
        {latest && (
          <div className="flex gap-2">
            <SignalBadge type={latest.signal_type} />
            <button onClick={() => setModal({ signal: latest, side: "buy" })}
              className="px-4 py-1.5 rounded-lg bg-green-700 hover:bg-green-600 text-white text-sm font-bold transition-colors">
              매수
            </button>
            <button onClick={() => setModal({ signal: latest, side: "sell" })}
              className="px-4 py-1.5 rounded-lg bg-red-700 hover:bg-red-600 text-white text-sm font-bold transition-colors">
              매도
            </button>
          </div>
        )}
      </div>

      {/* 캔들차트 */}
      <div className="bg-gray-800 rounded-xl p-4 border border-gray-700">
        <h2 className="text-sm font-semibold text-gray-300 mb-3">일봉 차트 (최근 120일)</h2>
        {loading ? (
          <div className="h-[400px] flex items-center justify-center text-gray-500">로딩 중…</div>
        ) : (
          <StockChart data={ohlcv} ticker={ticker} />
        )}
      </div>

      {/* 최신 지표 */}
      {latest && (
        <div className="bg-gray-800 rounded-xl p-4 border border-gray-700">
          <h2 className="text-sm font-semibold text-gray-300 mb-3">최신 지표</h2>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <Kv label="RSI" value={latest.rsi?.toFixed(1) ?? "-"}
              color={(latest.rsi ?? 50) > 70 ? "text-red-400" : (latest.rsi ?? 50) < 30 ? "text-green-400" : "text-white"} />
            <Kv label="MACD 히스토" value={latest.macd_histogram?.toFixed(2) ?? "-"}
              color={(latest.macd_histogram ?? 0) > 0 ? "text-green-400" : "text-red-400"} />
            <Kv label="거래량 배율" value={latest.volume_ratio ? `${latest.volume_ratio.toFixed(1)}x` : "-"} />
            <Kv label="볼린저 %b" value={latest.bb_pct?.toFixed(3) ?? "-"} />
            <Kv label="MA5" value={latest.ma5?.toLocaleString() ?? "-"} />
            <Kv label="MA20" value={latest.ma20?.toLocaleString() ?? "-"} />
            <Kv label="MA60" value={latest.ma60?.toLocaleString() ?? "-"} />
            <Kv label="신호 점수" value={(latest.score >= 0 ? "+" : "") + latest.score?.toFixed(3)}
              color={latest.score >= 0 ? "text-green-400" : "text-red-400"} />
          </div>
        </div>
      )}

      {/* 투자자 동향 */}
      {latest && (
        <div className="bg-gray-800 rounded-xl p-4 border border-gray-700">
          <h2 className="text-sm font-semibold text-gray-300 mb-3">투자자 동향</h2>
          <div className="grid grid-cols-2 gap-2">
            <Kv label="외국인 순매수"
              value={latest.foreign_net != null ? `${latest.foreign_net > 0 ? "+" : ""}${latest.foreign_net.toLocaleString()}주` : "-"}
              color={(latest.foreign_net ?? 0) > 0 ? "text-green-400" : "text-red-400"} />
            <Kv label="기관 순매수"
              value={latest.institution_net != null ? `${latest.institution_net > 0 ? "+" : ""}${latest.institution_net.toLocaleString()}주` : "-"}
              color={(latest.institution_net ?? 0) > 0 ? "text-green-400" : "text-red-400"} />
          </div>
          {latest.reason && (
            <p className="mt-3 text-sm text-gray-300 leading-relaxed">{latest.reason}</p>
          )}
        </div>
      )}

      {/* 신호 히스토리 */}
      <div className="bg-gray-800 rounded-xl p-4 border border-gray-700">
        <h2 className="text-sm font-semibold text-gray-300 mb-3">최근 신호</h2>
        {signals.length === 0 ? (
          <p className="text-gray-500 text-sm">신호 없음</p>
        ) : (
          <div className="space-y-2">
            {signals.map(s => (
              <div key={s.id} className="flex items-center justify-between text-sm border-b border-gray-700/50 pb-2">
                <span className="text-gray-400 text-xs">
                  {new Date(s.alerted_at).toLocaleString("ko-KR", {
                    month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"
                  })}
                </span>
                <SignalBadge type={s.signal_type} />
                <span className="text-white font-mono">{s.current_price.toLocaleString()}원</span>
                <span className={`font-mono font-bold ${s.score >= 0 ? "text-green-400" : "text-red-400"}`}>
                  {s.score >= 0 ? "+" : ""}{s.score?.toFixed(3)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {modal && (
        <TradeModal signal={modal.signal} side={modal.side}
          onClose={() => setModal(null)} />
      )}
    </div>
  );
}
