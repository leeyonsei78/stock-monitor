"""수동 매매 라우터"""
import os
import sys
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# 프로젝트 루트를 path에 추가 (KISApi 임포트용)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.api.kis_api import KISApi
from src.notification.slack_bot import SlackNotifier

router = APIRouter()

_notifier = SlackNotifier()


class TradeRequest(BaseModel):
    ticker: str = Field(..., min_length=6, max_length=6)
    quantity: int = Field(..., ge=1)
    price: int = Field(0, ge=0)  # 0이면 시장가


def _get_api():
    return KISApi()


@router.post("/trade/buy")
async def buy(req: TradeRequest):
    """수동 매수 주문"""
    try:
        api = _get_api()
        if req.price > 0:
            result = api.buy_limit(req.ticker, req.quantity, req.price)
        else:
            result = api.buy_market(req.ticker, req.quantity)

        _notifier.send_sync(
            f"🛒 *수동 매수 주문*\n"
            f"종목: {req.ticker} | 수량: {req.quantity}주 | "
            f"가격: {'시장가' if req.price == 0 else f'{req.price:,}원'}\n"
            f"주문번호: {result.get('order_no', '-')}"
        )
        return {"ok": True, "order": result}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/trade/sell")
async def sell(req: TradeRequest):
    """수동 매도 주문"""
    try:
        api = _get_api()
        if req.price > 0:
            result = api.sell_limit(req.ticker, req.quantity, req.price)
        else:
            result = api.sell_market(req.ticker, req.quantity)

        _notifier.send_sync(
            f"💸 *수동 매도 주문*\n"
            f"종목: {req.ticker} | 수량: {req.quantity}주 | "
            f"가격: {'시장가' if req.price == 0 else f'{req.price:,}원'}\n"
            f"주문번호: {result.get('order_no', '-')}"
        )
        return {"ok": True, "order": result}
    except Exception as e:
        raise HTTPException(500, str(e))
