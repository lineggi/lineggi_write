"""Perplexity 팩트 전문 기록 헬퍼.

"팩트 확보 완료(802자)" 같은 로그는 글자 수(len)만 알려주고
실제 내용은 어디에도 남지 않는다. 이 헬퍼는 팩트 전문을
1) logs/ 아래 텍스트 파일로 저장하고
2) 텔레그램으로 나눠서 전송해
나중에 "그 802자가 뭐였는지" 항상 확인할 수 있게 한다.
"""
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

TELEGRAM_CHUNK = 3500  # 텔레그램 메시지 한도(4096자) 아래로 안전하게 분할


def report_facts(tg, chat_id: str, facts: str, save_dir: str = "logs") -> str:
    """팩트 전문을 파일로 저장하고 텔레그램으로 전송한 뒤 파일 경로를 돌려준다."""
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"facts_{datetime.now():%Y%m%d_%H%M%S}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(facts)
    logger.info("팩트 전문 저장: %s (%d자)", path, len(facts))

    total = (len(facts) + TELEGRAM_CHUNK - 1) // TELEGRAM_CHUNK or 1
    for i in range(0, max(len(facts), 1), TELEGRAM_CHUNK):
        part = i // TELEGRAM_CHUNK + 1
        tg.send_message(
            chat_id,
            f"📚 팩트 전문 ({part}/{total})\n{facts[i:i + TELEGRAM_CHUNK]}",
            parse_mode=None,
        )
    return path
