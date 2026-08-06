"use client";
import { useState } from "react";
import type { Signal } from "@/lib/types";

interface Props {
  signal: Signal;
  side: "buy" | "sell";
  onClose: () => void;
}

export default function TradeModal({ signal, side, onClose }: Props) {
  const isBuy = side === "buy";
  const suggestedPrice = signal.current_price;
  const suggestedQty   = Math.max(1, Math.floor(1_000_000 / suggestedPrice));

  const [price, setPrice]  = useState(suggestedPrice);
  const [qty, setQty]      = useState(suggestedQty);
  const [market, setMarket] = useState(false);
  const [loading, setLoading] = useState(false);
  const [result, setResult]   = useState<string | null>(null);

  async function submit() {
    setLoading(true);
    try {
      const res = await fetch("/api/trade", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          side,
          ticker: signal.ticker,
          quantity: qty,
          price: market ? 0 : price,
        }),
      });
      const data = await res.json();
      if (data.ok) {
        setResult(`주문 완료 — 주문번호: ${data.order?.order_no ?? "-"}`);
      } else {
        setResult(`오류: ${data.error}`);
      }
    } catch (e) {
      setResult("요청 실패");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-gray-800 rounded-2xl p-6 w-full max-w-sm shadow-2xl border border-gray-600">
        <h2 className={`text-xl font-bold mb-4 ${isBuy ? "text-green-400" : "text-red-400"}`}>
          {isBuy ? "📈 매수 주문" : "📉 매도 주문"} — {signal.name || signal.ticker}
        </h2>

        {result ? (
          <div className="text-center py-4">
            <p className="text-white mb-4">{result}</p>
            <button onClick={onClose}
              className="px-6 py-2 bg-gray-600 rounded-lg text-white hover:bg-gray-500">
              닫기
            </button>
          </div>
        ) : (
          <>
            <div className="space-y-4 mb-6">
              <div>
                <label className="text-xs text-gray-400 block mb-1">현재가</label>
                <p className="text-white font-bold">{signal.current_price.toLocaleString()}원</p>
              </div>

              <label className="flex items-center gap-2 text-sm text-gray-300">
                <input type="checkbox" checked={market} onChange={e => setMarket(e.target.checked)}
                  className="rounded" />
                시장가 주문
              </label>

              {!market && (
                <div>
                  <label className="text-xs text-gray-400 block mb-1">주문가 (원)</label>
                  <input type="number" value={price}
                    onChange={e => setPrice(Number(e.target.value))}
                    className="w-full bg-gray-700 text-white rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-blue-500" />
                </div>
              )}

              <div>
                <label className="text-xs text-gray-400 block mb-1">수량 (주)</label>
                <input type="number" value={qty} min={1}
                  onChange={e => setQty(Number(e.target.value))}
                  className="w-full bg-gray-700 text-white rounded-lg px-3 py-2 border border-gray-600 focus:outline-none focus:border-blue-500" />
              </div>

              <div className="bg-gray-700 rounded-lg px-3 py-2 text-sm text-gray-300">
                예상 금액: <span className="text-white font-bold">
                  {(market ? signal.current_price * qty : price * qty).toLocaleString()}원
                </span>
              </div>
            </div>

            <div className="flex gap-3">
              <button onClick={onClose} disabled={loading}
                className="flex-1 py-2.5 rounded-lg bg-gray-600 hover:bg-gray-500 text-white transition-colors">
                취소
              </button>
              <button onClick={submit} disabled={loading}
                className={`flex-1 py-2.5 rounded-lg font-bold text-white transition-colors ${
                  isBuy ? "bg-green-700 hover:bg-green-600" : "bg-red-700 hover:bg-red-600"
                }`}>
                {loading ? "처리 중..." : isBuy ? "매수 확인" : "매도 확인"}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
