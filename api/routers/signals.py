"""신호 조회 라우터"""
import os
from fastapi import APIRouter, Query
from supabase import create_client

router = APIRouter()


def _supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


@router.get("/signals")
async def get_signals(
    limit: int = Query(50, ge=1, le=200),
    signal_type: str = Query(None, description="buy | sell | all"),
):
    """최근 매수/매도 신호 목록"""
    client = _supabase()
    q = (
        client.table("stock_signal_log")
        .select("*")
        .neq("signal_type", "보유")
        .order("alerted_at", desc=True)
        .limit(limit)
    )
    if signal_type == "buy":
        q = q.in_("signal_type", ["매수", "강한 매수"])
    elif signal_type == "sell":
        q = q.in_("signal_type", ["매도", "강한 매도"])

    result = q.execute()
    return {"signals": result.data or []}
