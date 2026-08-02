"""
KIS WebSocket 실시간 데이터 수신 클라이언트
실시간 체결가, 호가, 체결 통보 등 구독
"""
import asyncio
import json
import os
import websockets
from typing import Callable, Optional
from dotenv import load_dotenv
from src.utils.logger import setup_logger
import yaml

load_dotenv()
logger = setup_logger("websocket")


class KISWebSocket:
    TR_REAL_PRICE = "H0STCNT0"   # 실시간 체결가
    TR_REAL_ORDERBOOK = "H0STASP0"  # 실시간 호가
    TR_REAL_NOTICE = "H0STCNI9"   # 실시간 체결 통보

    def __init__(self, approval_key: str):
        with open("config/config.yaml", "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)["kis"]

        is_mock = os.getenv("KIS_IS_MOCK", "true").lower() == "true"
        self._ws_url = cfg["mock_ws_url"] if is_mock else cfg["real_ws_url"]
        self._approval_key = approval_key
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscriptions: dict[str, list[str]] = {}  # tr_id -> [tickers]
        self._handlers: dict[str, Callable] = {}
        self._running = False

    def register_handler(self, tr_id: str, handler: Callable):
        """특정 TR의 실시간 데이터 핸들러 등록"""
        self._handlers[tr_id] = handler

    async def subscribe(self, tr_id: str, ticker: str):
        """실시간 종목 구독"""
        msg = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1",
                "content-type": "utf-8",
            },
            "body": {
                "input": {
                    "tr_id": tr_id,
                    "tr_key": ticker,
                }
            },
        }
        await self._ws.send(json.dumps(msg))
        self._subscriptions.setdefault(tr_id, [])
        if ticker not in self._subscriptions[tr_id]:
            self._subscriptions[tr_id].append(ticker)
        logger.info(f"WebSocket 구독: {tr_id} - {ticker}")

    async def unsubscribe(self, tr_id: str, ticker: str):
        """구독 해제"""
        msg = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": ticker}},
        }
        await self._ws.send(json.dumps(msg))
        if tr_id in self._subscriptions and ticker in self._subscriptions[tr_id]:
            self._subscriptions[tr_id].remove(ticker)
        logger.info(f"WebSocket 구독 해제: {tr_id} - {ticker}")

    def _parse_price_data(self, raw: str) -> dict:
        """실시간 체결가 데이터 파싱 (|로 구분된 필드)"""
        fields = raw.split("|")
        if len(fields) < 4:
            return {}
        data_part = fields[3].split("^")
        if len(data_part) < 13:
            return {}
        return {
            "ticker": data_part[0],
            "time": data_part[1],
            "price": int(data_part[2]) if data_part[2] else 0,
            "change": int(data_part[4]) if data_part[4] else 0,
            "change_pct": float(data_part[5]) if data_part[5] else 0.0,
            "volume": int(data_part[8]) if data_part[8] else 0,
            "cumulative_volume": int(data_part[9]) if data_part[9] else 0,
            "buy_sell": data_part[11],   # 1=매도, 2=매수
        }

    def _parse_orderbook_data(self, raw: str) -> dict:
        """실시간 호가 데이터 파싱"""
        fields = raw.split("|")
        if len(fields) < 4:
            return {}
        data_part = fields[3].split("^")
        asks, bids = [], []
        for i in range(10):
            offset = i * 2
            if len(data_part) > 3 + offset + 1:
                asks.append({
                    "price": int(data_part[3 + offset]) if data_part[3 + offset] else 0,
                    "qty": int(data_part[4 + offset]) if data_part[4 + offset] else 0,
                })
        for i in range(10):
            offset = i * 2 + 20
            if len(data_part) > 3 + offset + 1:
                bids.append({
                    "price": int(data_part[3 + offset]) if data_part[3 + offset] else 0,
                    "qty": int(data_part[4 + offset]) if data_part[4 + offset] else 0,
                })
        return {"ticker": data_part[0] if data_part else "", "asks": asks, "bids": bids}

    async def _process_message(self, message: str):
        try:
            # JSON 응답 (구독 확인, 에러 등)
            if message.startswith("{"):
                data = json.loads(message)
                header = data.get("header", {})
                tr_id = header.get("tr_id", "")
                rt_cd = header.get("tr_key", "")
                body = data.get("body", {})
                if body.get("rt_cd") == "0":
                    logger.debug(f"구독 확인: {tr_id}")
                return

            # 실시간 데이터 (파이프 구분)
            parts = message.split("|")
            if len(parts) < 2:
                return

            tr_id = parts[1]
            if tr_id == self.TR_REAL_PRICE:
                parsed = self._parse_price_data(message)
                if parsed and tr_id in self._handlers:
                    await self._handlers[tr_id](parsed)
            elif tr_id == self.TR_REAL_ORDERBOOK:
                parsed = self._parse_orderbook_data(message)
                if parsed and tr_id in self._handlers:
                    await self._handlers[tr_id](parsed)

        except Exception as e:
            logger.error(f"WebSocket 메시지 처리 오류: {e}")

    async def connect(self):
        """WebSocket 연결 및 수신 루프"""
        self._running = True
        while self._running:
            try:
                logger.info(f"WebSocket 연결 중: {self._ws_url}")
                async with websockets.connect(self._ws_url) as ws:
                    self._ws = ws
                    logger.info("WebSocket 연결 성공")

                    # 재연결 후 구독 복원
                    for tr_id, tickers in self._subscriptions.items():
                        for ticker in tickers:
                            await self.subscribe(tr_id, ticker)

                    async for message in ws:
                        await self._process_message(message)

            except websockets.ConnectionClosed:
                logger.warning("WebSocket 연결 끊김 - 5초 후 재연결...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"WebSocket 오류: {e} - 10초 후 재연결...")
                await asyncio.sleep(10)

    async def disconnect(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        logger.info("WebSocket 연결 종료")

    async def subscribe_ticker(self, ticker: str):
        """종목의 실시간 체결가 + 호가 동시 구독"""
        await self.subscribe(self.TR_REAL_PRICE, ticker)
        await self.subscribe(self.TR_REAL_ORDERBOOK, ticker)
