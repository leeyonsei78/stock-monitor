"""
Slack 알림 발송 + 수동 매매 인터랙티브 봇
슬랙 명령 → 확인 버튼 클릭 → 주문 실행
"""
import os
import time
import uuid
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

    매수/매도/취소는 SLACK_TRADE_ALLOWED_USERS(콤마 구분 Slack user ID)에 등록된 사용자만
    실행할 수 있고, 실제 주문은 확인/취소 버튼을 눌러야 체결된다 (2026-08-24 추가).
    이전에는 사용자 인증이 전혀 없었고, "주문 확인" 메시지를 만들면서도 실제로는 확인 절차
    없이 명령 즉시 체결됐음 (confirm_msg가 콜백에 전달만 되고 쓰이지 않던 죽은 코드).
    status/recommend는 조회 전용이라 인증 없이 유지.
    """

    PENDING_TTL_SEC = 300  # 확인 대기 주문 유효 시간 (5분) — 방치된 확인창 방지

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

        allowed = os.getenv("SLACK_TRADE_ALLOWED_USERS", "")
        self._allowed_users = {u.strip() for u in allowed.split(",") if u.strip()}
        if not self._allowed_users:
            logger.warning(
                "SLACK_TRADE_ALLOWED_USERS 미설정 — 매수/매도/취소 명령을 아무도 실행할 수 없습니다 "
                "(status/recommend는 계속 동작). 승인할 Slack user ID를 콤마로 구분해 설정하세요."
            )

        # token → {ticker, side, quantity, price, user, user_id, created_at}
        self._pending: dict[str, dict] = {}

        self._register_handlers()

    # ── 권한 체크 ────────────────────────────────────────────────
    def _is_authorized(self, user_id: str) -> bool:
        return bool(user_id) and user_id in self._allowed_users

    def _purge_expired(self):
        now = time.time()
        for token in [t for t, p in self._pending.items() if now - p["created_at"] > self.PENDING_TTL_SEC]:
            self._pending.pop(token, None)

    def _register_handlers(self):
        @self._app.command("/trade")
        def handle_trade(ack, say, command):
            ack()
            text = command.get("text", "").strip()
            user = command.get("user_name", "unknown")
            user_id = command.get("user_id", "")
            logger.info(f"슬랙 명령 수신 [{user}/{user_id}]: /trade {text}")

            try:
                result_text, blocks = self._parse_and_execute(text, user, user_id)
                if blocks:
                    say(text=result_text, blocks=blocks)
                else:
                    say(result_text)
            except Exception as e:
                say(f"❌ 오류 발생: {e}")
                logger.error(f"슬랙 명령 처리 오류: {e}")

        @self._app.message("도움말")
        def help_message(message, say):
            say(self._help_text())

        @self._app.action("trade_confirm")
        def handle_confirm(ack, body, client):
            ack()
            self._resolve_pending(body, client, approve=True)

        @self._app.action("trade_cancel")
        def handle_cancel(ack, body, client):
            ack()
            self._resolve_pending(body, client, approve=False)

    # ── 확인/취소 버튼 처리 ──────────────────────────────────────
    def _resolve_pending(self, body: dict, client, approve: bool):
        token = body["actions"][0]["value"]
        clicker = body.get("user", {})
        clicker_id = clicker.get("id", "")
        channel_id = body["channel"]["id"]
        message_ts = body["message"]["ts"]

        pending = self._pending.get(token)
        if pending is None or time.time() - pending["created_at"] > self.PENDING_TTL_SEC:
            self._pending.pop(token, None)
            client.chat_update(channel=channel_id, ts=message_ts,
                                text="⌛ 만료되었거나 이미 처리된 주문입니다.", blocks=[])
            return

        # 요청 본인만 확인/취소 가능 — 다른 사람이 잘못 눌러도 원 요청은 그대로 유지
        if clicker_id != pending["user_id"]:
            client.chat_postEphemeral(
                channel=channel_id, user=clicker_id,
                text="🚫 본인이 요청한 주문만 확인/취소할 수 있습니다.",
            )
            return

        self._pending.pop(token, None)

        if not approve:
            client.chat_update(channel=channel_id, ts=message_ts,
                                text=f"❌ 주문 취소됨 (by @{pending['user']})", blocks=[])
            return

        result = self._order_cb(
            pending["ticker"], pending["side"], pending["quantity"],
            pending["price"], pending["user"],
        )
        client.chat_update(channel=channel_id, ts=message_ts, text=result, blocks=[])

    def _parse_and_execute(self, text: str, user: str, user_id: str) -> tuple[str, Optional[list]]:
        parts = text.split()
        if not parts:
            return self._help_text(), None

        action = parts[0].lower()

        # 포트폴리오 현황
        if action == "status" or action == "현황":
            return self._status_cb(), None

        # 추천 종목
        if action in ("recommend", "추천"):
            return self._recommend_cb(), None

        # 도움말
        if action in ("help", "도움말"):
            return self._help_text(), None

        # 주문 취소
        if action in ("cancel", "취소"):
            if not self._is_authorized(user_id):
                logger.warning(f"미승인 사용자 취소 시도 차단: {user}({user_id})")
                return "🚫 매매 명령 권한이 없습니다. 관리자에게 문의하세요.", None
            if len(parts) < 2:
                return "❌ 사용법: /trade cancel 주문번호", None
            return self._cancel_cb(parts[1]), None

        # 매수 / 매도
        if action not in ("buy", "sell", "매수", "매도"):
            return f"❌ 알 수 없는 명령: `{action}`\n" + self._help_text(), None

        if not self._is_authorized(user_id):
            logger.warning(f"미승인 사용자 매매 시도 차단: {user}({user_id})")
            return "🚫 매매 명령 권한이 없습니다. 관리자에게 문의하세요.", None

        if len(parts) < 3:
            return "❌ 사용법: /trade buy 종목코드 수량 [가격]", None

        ticker = parts[1].zfill(6)  # 6자리로 패딩
        try:
            quantity = int(parts[2])
        except ValueError:
            return f"❌ 수량이 올바르지 않습니다: {parts[2]}", None

        price = None
        if len(parts) >= 4:
            try:
                price = int(parts[3].replace(",", ""))
            except ValueError:
                return f"❌ 가격이 올바르지 않습니다: {parts[3]}", None

        side = "BUY" if action in ("buy", "매수") else "SELL"

        self._purge_expired()
        token = uuid.uuid4().hex
        self._pending[token] = {
            "ticker": ticker, "side": side, "quantity": quantity, "price": price,
            "user": user, "user_id": user_id, "created_at": time.time(),
        }
        summary = self._order_summary_text(ticker, side, quantity, price, user)
        return summary, self._confirm_blocks(token, summary)

    def _order_summary_text(self, ticker: str, side: str, quantity: int,
                             price: Optional[int], user: str) -> str:
        side_str = "매수" if side == "BUY" else "매도"
        price_str = f"{price:,}원 (지정가)" if price else "시장가"
        qty_str = f"{quantity:,}주" if quantity > 0 else "전량"
        return (
            f"📋 *주문 확인 요청* (@{user})\n"
            f"종목: `{ticker}` | 방향: *{side_str}* | 수량: {qty_str} | 가격: {price_str}\n"
            f"5분 내에 아래 버튼으로 확인해주세요."
        )

    @staticmethod
    def _confirm_blocks(token: str, summary: str) -> list:
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
            {
                "type": "actions",
                "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "✅ 확인"},
                     "style": "primary", "action_id": "trade_confirm", "value": token},
                    {"type": "button", "text": {"type": "plain_text", "text": "❌ 취소"},
                     "style": "danger", "action_id": "trade_cancel", "value": token},
                ],
            },
        ]

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
            "*AI 추천 종목*: `/trade recommend`\n\n"
            "_매수/매도/취소는 승인된 사용자만 가능하며, 버튼으로 확인해야 체결됩니다._"
        )

    def start(self):
        """별도 스레드에서 슬랙 봇 실행"""
        logger.info("슬랙 봇 시작 (Socket Mode)")
        thread = threading.Thread(target=self._handler.start, daemon=True)
        thread.start()

    def stop(self):
        self._handler.close()
        logger.info("슬랙 봇 종료")
