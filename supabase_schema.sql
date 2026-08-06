-- ============================================================
-- 주식 자동매매 Supabase 스키마 확장
-- Supabase SQL Editor에서 실행하세요
-- ============================================================

-- 1. stock_signal_log 컬럼 확장
ALTER TABLE stock_signal_log
  ADD COLUMN IF NOT EXISTS name              TEXT    DEFAULT '',
  ADD COLUMN IF NOT EXISTS tech_score        REAL    DEFAULT 0,
  ADD COLUMN IF NOT EXISTS investor_score    REAL    DEFAULT 0,
  ADD COLUMN IF NOT EXISTS reason            TEXT    DEFAULT '',
  ADD COLUMN IF NOT EXISTS rsi               REAL,
  ADD COLUMN IF NOT EXISTS macd_histogram    REAL,
  ADD COLUMN IF NOT EXISTS volume_ratio      REAL,
  ADD COLUMN IF NOT EXISTS foreign_net       INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS institution_net   INTEGER DEFAULT 0,
  ADD COLUMN IF NOT EXISTS bb_pct            REAL,
  ADD COLUMN IF NOT EXISTS ma5               REAL,
  ADD COLUMN IF NOT EXISTS ma20              REAL,
  ADD COLUMN IF NOT EXISTS ma60              REAL;

-- 2. Realtime 활성화 (대시보드 실시간 구독용)
ALTER PUBLICATION supabase_realtime ADD TABLE stock_signal_log;

-- 3. 일봉 OHLCV 캐시 테이블 (차트용)
CREATE TABLE IF NOT EXISTS stock_ohlcv_cache (
  id         BIGSERIAL    PRIMARY KEY,
  ticker     TEXT         NOT NULL,
  date       TEXT         NOT NULL,
  open       INTEGER      NOT NULL DEFAULT 0,
  high       INTEGER      NOT NULL DEFAULT 0,
  low        INTEGER      NOT NULL DEFAULT 0,
  close      INTEGER      NOT NULL DEFAULT 0,
  volume     BIGINT       NOT NULL DEFAULT 0,
  cached_at  TIMESTAMPTZ  DEFAULT NOW(),
  UNIQUE(ticker, date)
);

CREATE INDEX IF NOT EXISTS idx_ohlcv_ticker_date
  ON stock_ohlcv_cache(ticker, date DESC);

-- 4. RLS 비활성화 (서비스 키 사용, 기존 테이블과 동일 정책)
ALTER TABLE stock_ohlcv_cache DISABLE ROW LEVEL SECURITY;
