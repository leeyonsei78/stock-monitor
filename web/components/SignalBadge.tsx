import type { SignalType } from "@/lib/types";

const MAP: Record<SignalType, { label: string; cls: string }> = {
  "강한 매수": { label: "🚀 강한 매수", cls: "bg-green-700 text-green-100" },
  "매수":      { label: "📈 매수",      cls: "bg-green-600 text-white" },
  "보유":      { label: "⏸ 보유",       cls: "bg-gray-600 text-gray-200" },
  "매도":      { label: "📉 매도",      cls: "bg-red-600 text-white" },
  "강한 매도": { label: "🔴 강한 매도", cls: "bg-red-800 text-red-100" },
};

export default function SignalBadge({ type }: { type: SignalType }) {
  const { label, cls } = MAP[type] ?? MAP["보유"];
  return (
    <span className={`inline-block rounded-full px-3 py-0.5 text-xs font-bold ${cls}`}>
      {label}
    </span>
  );
}
