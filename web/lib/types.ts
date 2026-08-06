export type SignalType = "강한 매수" | "매수" | "보유" | "매도" | "강한 매도";

export interface Signal {
  id: number;
  ticker: string;
  name: string;
  signal_type: SignalType;
  score: number;
  current_price: number;
  tech_score: number;
  investor_score: number;
  reason: string;
  rsi: number | null;
  macd_histogram: number | null;
  volume_ratio: number | null;
  foreign_net: number | null;
  institution_net: number | null;
  bb_pct: number | null;
  ma5: number | null;
  ma20: number | null;
  ma60: number | null;
  alerted_at: string;
}

export interface OHLCVBar {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}
