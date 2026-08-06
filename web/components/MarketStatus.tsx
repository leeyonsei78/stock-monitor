"use client";
import { useEffect, useState } from "react";

function getStatus() {
  const now = new Date(new Date().toLocaleString("en-US", { timeZone: "Asia/Seoul" }));
  const day = now.getDay();
  const h = now.getHours(), m = now.getMinutes();
  const t = h * 60 + m;
  if (day === 0 || day === 6) return { label: "주말 휴장", color: "text-gray-400" };
  if (t < 8 * 60) return { label: "장 시작 전", color: "text-gray-400" };
  if (t < 9 * 60) return { label: "장전 시간외", color: "text-yellow-400" };
  if (t < 15 * 60 + 30) return { label: "정규장 운영 중", color: "text-green-400" };
  if (t < 18 * 60) return { label: "장후 시간외", color: "text-yellow-400" };
  return { label: "장 마감", color: "text-gray-400" };
}

export default function MarketStatus() {
  const [status, setStatus] = useState(getStatus());
  const [time, setTime] = useState("");

  useEffect(() => {
    const tick = () => {
      setStatus(getStatus());
      setTime(new Date().toLocaleString("ko-KR", {
        timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", second: "2-digit",
      }));
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="flex items-center gap-3 text-sm">
      <span className={`font-semibold ${status.color}`}>● {status.label}</span>
      <span className="text-gray-400 font-mono">{time} KST</span>
    </div>
  );
}
