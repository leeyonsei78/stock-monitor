"""
Slack 알림 발송 + 수동 매매 인터랙티브 봇
슬랙 메시지로 매수/매도/수량/가격 입력 → 주문 실행
"""
import os
import asyncio
import threading
from typing import Optional, Callable
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk import WebClient
from dotenv import load_dotenv
from src.utils.logger import setup_logger

load_dotenv()
logger = setup_logger("slack")


class SlackNotifier:
    """단방향 알림 전송 (자동 모드에서도 사용)"""

    def __init__(self):
        self._client = WebClient(token=os.getenv("SLACK_BOT_TOKEN"))
        self._channel = os.getenv("SLACK_CHANNEL_ID")

    async def send(self, message: str, blocks: Optional[list] = None):
        try:
            kwargs = {"channel": self._channel, "text": message}
            if blocks:
                kwargs["blocks"] = blocks
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self._client.chat_postMessage(**kwargs)
            )
        except Exception as e:
            logger.error(f"Slack 메시지 전송 실패: {e}")

    def send_sync(self, message: str, blocks: Optional[list] = None):
        try:
            kwargs = {"channel": self._channel, "text": message}
            if blocks:
                kwargs["blocks"] = blocks
            self._client.chat_postMessage(**kwargs)
        except Exception as e:
            logger.error(f"Slack 메시지 전송 실패: {e}")


class ManualSlackBot:
    """
    수동 매매 슬랙 봇
    커맨드: /trade buy 005930 100 80000   → 삼성전자 100주 지정가 80,000원 매수
            /trade sell 005930 50          → 삼성전자 50주 시장가 매도
            /trade sell 005930 50 79000   → 삼성전자 50주 지정가 79,000원 매도
            /trade status                  → 포트폴리오 현황
            /trade cancel ORD12345        → 주문 취소
            /trade recommend               → AI 추천 종목 조회
    """

    def __init__(self, order_callback: Callable, status_callback: Callable,
                 cancel_callback: Callable, recommend_callback: Callable):
        self._app = App(
            token=os.getenv("SLACK_BOT_TOKEN"),
            signing_secret=os.getenv("SLACK_SIGNING_SECRET"),
        )
        self._handler = SocketModeHandler(self._app, os.getenv("SLACK_APP_TOKEN"))
        self._order_cb = order_callback
        self._status_cb = status_callback
        self._cancel_cb = cancel_callback
        self._recommend_cb = recommend_callback

        self._register_handlers()

    def _register_handlers(self):
        @self._app.command("/trade")
        def handle_trade(ack, say, command):
            ack()
            text = command.get("text", "").strip()
            user = command.get("user_name", "unknown")
            logger.info(f"슬랙 명령 수신 [{user}]: /trade {text}")

            try:
                result = self._parse_and_execute(text, user)
                say(result)
            except Exception as e:
                say(f"❌ 오류 발생: {e}")
                logger.error(f"슬랙 명령 처리 오류: {e}")

        @self._app.message("도움말")
        def help_message(message, say):
            say(self._help_text())

    def _parse_and_execute(self, text: str, user: str) -> str:
        parts = text.split()
        if not parts:
            return self._help_text()

        action = parts[0].lower()

        # 포트폴리오 현황
        if action == "status" or action == "현황":
            return self._status_cb()

        # 추천 종목
        if action in ("recommend", "추천"):
            return self._recommend_cb()

        # 도움말
        if action in ("help", "도움말"):
            return self._help_text()

        # 주문 취소
        if action in ("cancel", "취소"):
            if len(parts) < 2:
                return "❌ 사용법: /trade cancel 주문번호"
            return self._cancel_cb(parts[1])

        # 매수 / 매도
        if action not in ("buy", "sell", "매수", "매도"):
            return f"❌ 알 수 없는 명령: `{action}`\n" + self._help_text()

        if len(parts) < 3:
            return "❌ 사용법: /trade buy 종목코드 수량 [가격]"

        ticker = parts[1].zfill(6)  # 6자리로 패딩
        try:
            quantity = int(parts[2])
        except ValueError:
            return f"❌ 수량이 올바르지 않습니다: {parts[2]}"

        price = None
        if len(parts) >= 4:
            try:
                price = int(parts[3].replace(",", ""))
            except ValueError:
                return f"❌ 가격이 올바르지 않습니다: {parts[3]}"

        side = "BUY" if action in ("buy", "매수") else "SELL"

        # 확인 메시지 블록으로 전송 후 콜백
        confirm = self._build_confirm_message(ticker, side, quantity, price, user)
        return self._order_cb(ticker, side, quantity, price, user, confirm)

    def _build_confirm_message(self, ticker: str, side: str, quantity: int,
                               price: Optional[int], user: str) -> str:
        side_str = "매수" if side == "BUY" else "매도"
        price_str = f"{price:,}원 (지정가)" if price else "시장가"
        return (
            f"📋 *주문 확인* (@{user})\n"
            f"종목: {ticker} | 방향: {side_str} | 수량: {quantity:,}주 | 가격: {price_str}"
        )

    def _help_text(self) -> str:
        return (
            "📌 *주식 자동매매 슬랙 봇 명령어*\n\n"
            "*매수 (지정가)*: `/trade buy 종목코드 수량 가격`\n"
            "예) `/trade buy 005930 100 75000` → 삼성전자 100주 지정가 75,000원 매수\n\n"
            "*매수 (시장가)*: `/trade buy 종목코드 수량`\n"
            "예) `/trade buy 005930 100` → 삼성전자 100주 시장가 매수\n\n"
            "*매도 (지정가)*: `/trade sell 종목코드 수량 가격`\n"
            "예) `/trade sell 005930 50 76000`\n\n"
            "*매도 (시장가)*: `/trade sell 종목코드 수량`\n"
            "예) `/trade sell 005930 50` → 삼성전자 50주 시장가 매도\n\n"
            "*전량 매도 (시장가)*: `/trade sell 종목코드 0`\n\n"
            "*주문 취소*: `/trade cancel 주문번호`\n\n"
            "*포트폴리오 현황*: `/trade status`\n\n"
            "*AI 추천 종목*: `/trade recommend`\n"
        )

    def start(self):
        """별도 스레드에서 슬랙 봇 실행"""
        logger.info("슬랙 봇 시작 (Socket Mode)")
        thread = threading.Thread(target=self._handler.start, daemon=True)
        thread.start()

    def stop(self):
        self._handler.close()
        logger.info("슬랙 봇 종료")
