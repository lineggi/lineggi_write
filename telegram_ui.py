"""텔레그램 인라인 키보드/콜백 등 UI 성격의 헬퍼 모음.

main() 안에 클로저로 숨어 있던 함수들을 클래스로 분리해서
재사용·테스트가 가능하게 했다. TelegramService(tg)의 base_url 을
그대로 사용하므로 기존 서비스 코드는 수정할 필요가 없다.
"""
import json
import logging

import requests

logger = logging.getLogger(__name__)

TOPIC_COUNT = 5          # 주제 제안 개수 (1~5번 버튼)
REFRESH_CALLBACK = "R"   # "주제 다시 제안받기" 콜백 데이터
REQUEST_TIMEOUT = 10


class TelegramUI:
    def __init__(self, tg, chat_id: str, debug_ping: bool = False):
        self.tg = tg
        self.chat_id = chat_id
        self.debug_ping = debug_ping

    def topic_keyboard(self) -> str:
        keyboard = {
            "inline_keyboard": [
                [{"text": f"{i}번 선택", "callback_data": str(i)} for i in range(1, TOPIC_COUNT + 1)],
                [{"text": "🔄 주제 다시 제안받기", "callback_data": REFRESH_CALLBACK}],
            ]
        }
        return json.dumps(keyboard)

    def remove_inline_keyboard(self, message_id: int) -> None:
        try:
            requests.post(
                f"{self.tg.base_url}/editMessageReplyMarkup",
                json={"chat_id": self.chat_id, "message_id": message_id, "reply_markup": None},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            logger.exception("remove_inline_keyboard 실패 (message_id=%s)", message_id)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = False
        try:
            requests.post(
                f"{self.tg.base_url}/answerCallbackQuery",
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            logger.exception("answer_callback_query 실패 (id=%s)", callback_query_id)

    def debug(self, msg: str) -> None:
        logger.info(msg)
        if self.debug_ping:
            self.tg.send_message(self.chat_id, f"🧪 DEBUG: {msg}", parse_mode=None)
