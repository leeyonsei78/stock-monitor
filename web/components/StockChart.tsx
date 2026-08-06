"use client";
import { useEffect, useRef } from "react";
import type { OHLCVBar } from "@/lib/types";

interface Props {
  data: OHLCVBar[];
  ticker: string;
}

export default function StockChart({ data, ticker }: Props) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current || data.length === 0) return;

    let chart: any;
    (async () => {
      const { createChart, ColorType, CandlestickSeries } = await import("lightweight-charts");

      chart = createChart(ref.current!, {
        layout: {
          background: { type: ColorType.Solid, color: "#1f2937" },
          textColor: "#9ca3af",
        },
        grid: {
          vertLines: { color: "#374151" },
          horzLines: { color: "#374151" },
        },
        crosshair: { mode: 1 },
        rightPriceScale: { borderColor: "#374151" },
        timeScale: { borderColor: "#374151", timeVisible: true },
        width:  ref.current!.clientWidth,
        height: 400,
      });

      const candle = chart.addSeries(CandlestickSeries, {
        upColor:      "#16a34a",
        downColor:    "#dc2626",
        borderUpColor:   "#16a34a",
        borderDownColor: "#dc2626",
        wickUpColor:     "#16a34a",
        wickDownColor:   "#dc2626",
      });

      candle.setData(
        data.map(b => ({
          time:  b.date,
          open:  b.open,
          high:  b.high,
          low:   b.low,
          close: b.close,
        }))
      );

      chart.timeScale().fitContent();

      const ro = new ResizeObserver(() => {
        chart.applyOptions({ width: ref.current!.clientWidth });
      });
      ro.observe(ref.current!);
      (ref.current as any)._ro = ro;
    })();

    return () => {
      (ref.current as any)?._ro?.disconnect();
      chart?.remove();
    };
  }, [data]);

  if (data.length === 0) {
    return (
      <div className="h-[400px] bg-gray-800 rounded-xl flex items-center justify-center text-gray-500">
        차트 데이터 로딩 중…
      </div>
    );
  }

  return <div ref={ref} className="w-full rounded-xl overflow-hidden" />;
}
