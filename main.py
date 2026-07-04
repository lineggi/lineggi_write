"""뉴스 브리핑 봇 진입점.

설정(config), 뉴스 수집(news_collector), 텔레그램 UI(telegram_ui),
글 후처리(text_utils), 팩트 기록(facts_logger)을 조립만 한다.
비즈니스 로직은 각 모듈에 있다.
"""
import logging

from ai_generator import AIGenerator
from telegram_service import TelegramService
from perplexity_service import PerplexityService

from config import load_config
from facts_logger import report_facts
from news_collector import collect_news
from telegram_ui import TelegramUI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    cfg = load_config()

    tg = TelegramService(cfg.telegram_token)
    ui = TelegramUI(tg, cfg.my_chat_id, debug_ping=cfg.debug_telegram_ping)
    ai = AIGenerator(google_api_key=cfg.gemini_api_key, openai_api_key=cfg.openai_api_key)
    px = PerplexityService(cfg.perplexity_api_key) if cfg.use_perplexity else None

    tg.send_message(
        cfg.my_chat_id,
        "🚀 뉴스 브리핑 봇 시작 (Text:Gemini / Img:Flat Design)",
        parse_mode=None,
    )

    # Step 1) 뉴스 수집
    ui.debug("Step1 시작(뉴스 수집)")
    all_news = collect_news(cfg.keywords, per_keyword=cfg.news_per_keyword)
    logger.info("뉴스 %d건 수집 완료", len(all_news))

    # ------------------------------------------------------------------
    # Step 2 이후 (시트 저장 → 주제 제안 → 콜백 폴링 → 팩트 수집 → 글 생성)
    # 원본 main.py 가 여기서 잘려 있어 이 아래 로직은 기존 코드를 옮겨 붙여야 한다.
    # 옮길 때 대응 규칙:
    #   - get_full_keyboard()            -> ui.topic_keyboard()
    #   - remove_inline_keyboard(mid)    -> ui.remove_inline_keyboard(mid)
    #   - answer_callback_query(...)     -> ui.answer_callback_query(...)
    #   - tg_debug(msg)                  -> ui.debug(msg)
    #   - safe_cut_after_end_marker(...) -> from text_utils import safe_cut_after_end_marker
    #   - MY_CHAT_ID / SHEET_NAME 등     -> cfg.my_chat_id / cfg.sheet_name
    #
    # 팩트 수집 직후에는 반드시 전문을 남긴다:
    #   facts = px.search_facts(topic)   # 기존 호출 그대로
    #   ui.debug(f"팩트 확보 완료({len(facts)}자)")
    #   if cfg.debug_facts_preview:
    #       report_facts(tg, cfg.my_chat_id, facts)   # logs/facts_*.txt + 텔레그램 전문
    # ------------------------------------------------------------------


if __name__ == "__main__":
    main()
