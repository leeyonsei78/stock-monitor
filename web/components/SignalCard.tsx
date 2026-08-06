"use client";
import Link from "next/link";
import type { Signal } from "@/lib/types";
import SignalBadge from "./SignalBadge";

function fmt(n: number | null | undefined, dec = 0) {
  if (n == null) return "-";
  return n.toLocaleString("ko-KR", { minimumFractionDigits: dec, maximumFractionDigits: dec });
}

interface Props {
  signal: Signal;
  onTrade: (signal: Signal, side: "buy" | "sell") => void;
}

export default function SignalCard({ signal, onTrade }: Props) {
  const isBuy  = signal.signal_type === "매수" || signal.signal_type === "강한 매수";
  const isSell = signal.signal_type === "매도" || signal.signal_type === "강한 매도";
  const border = isBuy ? "border-green-600" : isSell ? "border-red-600" : "border-gray-600";
  const time   = new Date(signal.alerted_at).toLocaleString("ko-KR", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });

  return (
    <div className={`bg-gray-800 border ${border} rounded-xl p-4 flex flex-col gap-3`}>
      {/* 헤더 */}
      <div className="flex items-start justify-between gap-2">
        <div>
          <Link href={`/stocks/${signal.ticker}`}
            className="text-base font-bold text-white hover:text-blue-400 transition-colors">
            {signal.name || signal.ticker}
          </Link>
          <span className="ml-2 text-xs text-gray-400">{signal.ticker}</span>
        </div>
        <SignalBadge type={signal.signal_type} />
      </div>

      {/* 가격 + 점수 */}
      <div className="flex items-baseline gap-3">
        <span className="text-2xl font-bold text-white">{fmt(signal.current_price)}원</span>
        <span className={`text-sm font-semibold ${signal.score >= 0 ? "text-green-400" : "text-red-400"}`}>
          {signal.score >= 0 ? "+" : ""}{signal.score?.toFixed(3)}
        </span>
      </div>

      {/* 지표 그리드 */}
      <div className="grid grid-cols-3 gap-2 text-xs text-gray-300">
        <Kv label="RSI" value={fmt(signal.rsi, 1)} />
        <Kv label="거래량" value={signal.volume_ratio ? `${signal.volume_ratio.toFixed(1)}x` : "-"} />
        <Kv label="외국인" value={signal.foreign_net != null ? `${signal.foreign_net > 0 ? "+" : ""}${fmt(signal.foreign_net)}주` : "-"} />
        <Kv label="기관" value={signal.institution_net != null ? `${signal.institution_net > 0 ? "+" : ""}${fmt(signal.institution_net)}주` : "-"} />
        <Kv label="기술점수" value={fmt(signal.tech_score, 3)} />
        <Kv label="수급점수" value={fmt(signal.investor_score, 3)} />
      </div>

      {/* 이유 */}
      {signal.reason && (
        <p className="text-xs text-gray-400 truncate" title={signal.reason}>
          {signal.reason}
        </p>
      )}

      {/* 푸터 */}
      <div className="flex items-center justify-between mt-1">
        <span className="text-xs text-gray-500">{time}</span>
        <div className="flex gap-2">
          <button onClick={() => onTrade(signal, "buy")}
            className="px-3 py-1 rounded text-xs font-bold bg-green-700 hover:bg-green-600 text-white transition-colors">
            매수
          </button>
          <button onClick={() => onTrade(signal, "sell")}
            className="px-3 py-1 rounded text-xs font-bold bg-red-700 hover:bg-red-600 text-white transition-colors">
            매도
          </button>
        </div>
      </div>
    </div>
  );
}

function Kv({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-gray-700 rounded px-2 py-1">
      <div className="text-gray-400 text-[10px]">{label}</div>
      <div className="font-semibold text-white">{value}</div>
    </div>
  );
}
