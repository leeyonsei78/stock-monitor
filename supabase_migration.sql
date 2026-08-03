-- 5분 변화율 추적용 테이블
-- Supabase SQL Editor에서 실행하세요

CREATE TABLE IF NOT EXISTS stock_price_snapshot (
    ticker      TEXT PRIMARY KEY,
    price       INTEGER NOT NULL,
    scanned_at  TIMESTAMPTZ DEFAULT now()
);
