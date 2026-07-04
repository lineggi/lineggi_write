import logging
import time

import requests

logger = logging.getLogger(__name__)

TELEGRAM_MSG_LIMIT = 4096
LONG_MSG_CHUNK = 4000


class TelegramService:
    def __init__(self, token):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()  # 세션 유지로 속도 향상

    def get_updates(self, offset=None, timeout=30):
        """메시지 수신 (에러 발생 시 죽지 않고 빈 리스트 반환)"""
        params = {"timeout": timeout}
        # offset=0 도 유효한 값이므로 None 비교로 판정
        if offset is not None:
            params["offset"] = offset

        try:
            # 읽기 타임아웃은 롱폴링 타임아웃보다 길게
            response = self.session.get(
                f"{self.base_url}/getUpdates", params=params, timeout=timeout + 10
            )
            return response.json().get("result", [])

        except (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout) as e:
            logger.warning("텔레그램 연결 불안정 (재시도 중...): %s", e)
            time.sleep(2)
            return []

        except Exception:
            logger.exception("get_updates 알 수 없는 오류")
            time.sleep(5)
            return []

    def send_message(self, chat_id, text, parse_mode="Markdown", reply_markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            resp = self.session.post(f"{self.base_url}/sendMessage", json=payload, timeout=10)
            result = resp.json()
            if not result.get("ok"):
                logger.warning("메시지 전송 거절됨: %s", result.get("description"))
            return result
        except Exception:
            logger.exception("메시지 전송 실패")
            return {}

    def send_long_message(self, chat_id, text, parse_mode=None):
        """텔레그램 한도(4096자)를 넘는 메시지 분할 전송"""
        if len(text) <= TELEGRAM_MSG_LIMIT:
            return self.send_message(chat_id, text, parse_mode=parse_mode)

        for i in range(0, len(text), LONG_MSG_CHUNK):
            self.send_message(chat_id, text[i:i + LONG_MSG_CHUNK], parse_mode=parse_mode)
            time.sleep(0.5)  # 순서 꼬임 방지 딜레이

    def send_photo(self, chat_id, photo_path, caption=None):
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption

        try:
            with open(photo_path, "rb") as f:
                resp = self.session.post(
                    f"{self.base_url}/sendPhoto",
                    data=data,
                    files={"photo": f},
                    timeout=60,  # 사진 업로드는 오래 걸릴 수 있음
                )
            return resp.json()
        except Exception:
            logger.exception("사진 전송 실패")
            return {}
