"""OHLCV 조회 라우터"""
import os
from datetime import datetime, timedelta
from fastapi import APIRouter, Path, Query, HTTPException
from supabase import create_client

router = APIRouter()


def _supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


@router.get("/ohlcv/{ticker}")
async def get_ohlcv(
    ticker: str = Path(..., min_length=6, max_length=6),
    days: int = Query(60, ge=10, le=250),
):
    """종목 일봉 데이터 (Supabase 캐시 → FinanceDataReader fallback)"""
    client = _supabase()

    # 캐시에서 조회
    result = (
        client.table("stock_ohlcv_cache")
        .select("date,open,high,low,close,volume")
        .eq("ticker", ticker)
        .order("date", desc=False)
        .limit(days)
        .execute()
    )
    if result.data and len(result.data) >= 10:
        return {"ticker": ticker, "ohlcv": result.data}

    # 캐시 미스 → FinanceDataReader 직접 조회
    try:
        import FinanceDataReader as fdr
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=days * 2)).strftime("%Y%m%d")
        df = fdr.DataReader(ticker, start, end)
        if df is None or df.empty:
            raise HTTPException(404, "데이터 없음")

        ohlcv = [
            {
                "date": str(d.date()),
                "open": int(row.get("Open", 0)),
                "high": int(row.get("High", 0)),
                "low": int(row.get("Low", 0)),
                "close": int(row.get("Close", 0)),
                "volume": int(row.get("Volume", 0)),
            }
            for d, row in df.iterrows()
        ][-days:]

        # 캐시에 저장 (다음 요청 빠르게)
        if ohlcv:
            rows = [{"ticker": ticker, **bar} for bar in ohlcv]
            try:
                client.table("stock_ohlcv_cache").upsert(rows).execute()
            except Exception:
                pass

        return {"ticker": ticker, "ohlcv": ohlcv}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"데이터 조회 실패: {e}")
