"""뉴스 브리핑 봇 진입점.

키워드는 매 실행마다 달라지므로 .env 에 두지 않는다:
  1) 명령줄:  python main.py "솔라나,토스뱅크"
  2) 생략 시: 봇이 텔레그램으로 키워드를 물어보고 답장을 기다림
"""
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

import requests

from ai_generator import AIGenerator
from crawler import naver_crawler
from naver_service import get_news_content, save_news_to_sheet
from perplexity_service import PerplexityService
from telegram_service import TelegramService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

NEWS_PER_KEYWORD = 10           # 키워드당 수집 기사 수
TOPIC_RANK_LIMIT = 5            # 주제 제안에 사용할 키워드별 상위 기사 수
TOPIC_CHOICES = {"1", "2", "3", "4", "5"}
REFRESH_CALLBACK = "R"
FACTS_LOG_DIR = "logs"          # 팩트 전문 보관 위치


# =========================
# 0) 설정
# =========================
@dataclass(frozen=True)
class Config:
    telegram_token: str
    gemini_api_key: str
    perplexity_api_key: str
    my_chat_id: str
    sheet_name: str
    use_perplexity: bool
    debug_telegram_ping: bool


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"환경변수 {name} 이(가) 설정되지 않았습니다. .env 를 확인하세요.")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def load_config() -> Config:
    return Config(
        telegram_token=_require_env("TELEGRAM_TOKEN"),
        gemini_api_key=_require_env("GEMINI_API_KEY"),
        perplexity_api_key=os.getenv("PERPLEXITY_API_KEY", ""),
        my_chat_id=_require_env("MY_CHAT_ID"),
        sheet_name=os.getenv("SHEET_NAME", "AI_Writing_Brunch"),
        use_perplexity=_bool_env("USE_PERPLEXITY", True),
        debug_telegram_ping=_bool_env("DEBUG_TELEGRAM_PING", True),
    )


# =========================
# 순수 유틸리티 (상태 의존 X)
# =========================
def get_full_keyboard() -> str:
    keyboard = {
        "inline_keyboard": [
            [{"text": f"{i}번 선택", "callback_data": str(i)} for i in sorted(TOPIC_CHOICES)],
            [{"text": "🔄 주제 다시 제안받기", "callback_data": REFRESH_CALLBACK}],
        ]
    }
    return json.dumps(keyboard)


def parse_keywords(text: str) -> List[str]:
    """'솔라나, 토스뱅크' 같은 쉼표 구분 문자열을 키워드 리스트로 변환."""
    return [k.strip() for k in (text or "").split(",") if k.strip()]


def archive_facts(facts: str) -> str:
    """팩트 전문을 logs/ 아래 파일로 남긴다.

    '팩트 확보 완료(N자)' 로그는 글자 수만 남기 때문에, 나중에
    "그 N자가 뭐였는지" 확인할 수 있도록 전문을 보관한다.
    """
    os.makedirs(FACTS_LOG_DIR, exist_ok=True)
    path = os.path.join(FACTS_LOG_DIR, f"facts_{datetime.now():%Y%m%d_%H%M%S}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(facts)
    return path


# =========================
# 봇 본체
# =========================
class NewsBriefingBot:
    def __init__(self, config: Config, keywords: Optional[List[str]] = None):
        self.cfg = config
        self.chat_id = config.my_chat_id
        self.keywords: List[str] = keywords or []

        self.tg = TelegramService(config.telegram_token)
        self.ai = AIGenerator(google_api_key=config.gemini_api_key)
        self.px = PerplexityService(config.perplexity_api_key)

        self.all_news: List[Dict] = []
        self.suggestion_data: List[str] = []
        self.last_topics: str = ""     # 서비스 객체가 아닌 봇이 상태를 소유
        self.processing_lock = False   # 중복 실행 방지
        self._last_update_id = -1      # 텔레그램 업데이트 오프셋 (키워드 입력/콜백 공용)

    # ---------- 텔레그램 헬퍼 ----------
    def send(self, text: str, **kwargs):
        kwargs.setdefault("parse_mode", None)
        return self.tg.send_message(self.chat_id, text, **kwargs)

    def debug(self, msg: str):
        logger.info(msg)
        if self.cfg.debug_telegram_ping:
            self.send(f"🧪 DEBUG: {msg}")

    def remove_inline_keyboard(self, message_id: int):
        try:
            requests.post(
                f"{self.tg.base_url}/editMessageReplyMarkup",
                json={"chat_id": self.chat_id, "message_id": message_id, "reply_markup": None},
                timeout=10,
            )
        except requests.RequestException:
            logger.warning("remove_inline_keyboard 실패 (message_id=%s)", message_id, exc_info=True)

    def answer_callback_query(self, callback_query_id: str, text: str = ""):
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
            payload["show_alert"] = False
        try:
            requests.post(f"{self.tg.base_url}/answerCallbackQuery", json=payload, timeout=10)
        except requests.RequestException:
            logger.warning("answer_callback_query 실패", exc_info=True)

    # ---------- 텔레그램 업데이트 폴링 ----------
    def _poll_updates(self, timeout: int = 30) -> List[Dict]:
        updates = self.tg.get_updates(offset=self._last_update_id + 1, timeout=timeout)
        for u in updates:
            self._last_update_id = u.get("update_id", self._last_update_id)
        return updates

    def _drain_pending_updates(self):
        """봇 시작 전에 쌓여 있던 오래된 메시지를 건너뛴다."""
        while self._poll_updates(timeout=0):
            pass

    # ---------- Step 0) 키워드 입력 ----------
    def ask_keywords(self) -> List[str]:
        """텔레그램으로 키워드를 물어보고 답장을 기다린다."""
        self.send(
            "🔍 이번에 수집할 키워드를 쉼표(,)로 구분해서 보내주세요.\n"
            "예) 솔라나,토스뱅크,스테이블코인"
        )
        while True:
            for update in self._poll_updates():
                msg = update.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
                    continue
                keywords = parse_keywords(msg.get("text", ""))
                if keywords:
                    self.send(f"✅ 키워드 설정: {', '.join(keywords)}")
                    return keywords
                if msg.get("text"):
                    self.send("⚠️ 키워드를 인식하지 못했어요. 쉼표로 구분해 다시 보내주세요.")
            time.sleep(1)

    # ---------- Step 1) 뉴스 수집 ----------
    def collect_news(self) -> bool:
        self.debug("Step1 시작(뉴스 수집)")
        self.all_news = []

        for kw in self.keywords:
            try:
                items = naver_crawler.get_news(kw)
            except Exception:
                logger.exception("naver_crawler 예외 (keyword=%s)", kw)
                continue

            for rank, item in enumerate(items[:NEWS_PER_KEYWORD], 1):
                try:
                    top_txt, bottom_txt = get_news_content(item["link"])
                except Exception:
                    top_txt, bottom_txt = "", ""

                self.all_news.append({
                    "pub_date": item.get("pub_time", datetime.now().strftime("%Y-%m-%d %H:%M")),
                    "keyword": kw,
                    "relevance_rank": rank,
                    "total_count": item.get("total_count", ""),  # 시트 D열(주제검색량)
                    "title": item.get("title", "제목 없음"),
                    "link": item.get("link", ""),
                    "top_content": top_txt,
                    "bottom_content": bottom_txt,
                })

        return bool(self.all_news)

    # ---------- Step 2) 시트 저장 ----------
    def save_to_sheet(self):
        self.debug("Step2 시작(시트 저장)")
        # 본문은 collect_news 에서 이미 수집했으므로 여기서 재크롤링하지 않는다
        save_news_to_sheet(self.cfg.sheet_name, self.all_news)

    # ---------- Step 3) 주제 생성 ----------
    def suggest_topics(self) -> bool:
        self.debug("Step3 시작(주제 생성)")
        self.suggestion_data = []
        for kw in self.keywords:
            kw_top = [
                n for n in self.all_news
                if n["keyword"] == kw and n["relevance_rank"] <= TOPIC_RANK_LIMIT
            ]
            group_text = f"[{kw} 관련]\n" + "\n".join(f"- {n['title']}" for n in kw_top)
            self.suggestion_data.append(group_text)

        topics = self.ai.get_5_topics(self.suggestion_data)

        if topics and "❌" not in topics:
            self.last_topics = topics
            # parse_mode=None 이므로 ** 같은 마크다운 장식은 쓰지 않는다
            self.send(f"📝 오늘의 주제 제안\n\n{topics}", reply_markup=get_full_keyboard())
            return True

        self.send(f"⚠️ 주제 생성 실패\n{topics}")
        return False

    # ---------- Step 4) 콜백 루프 ----------
    def run_callback_loop(self):
        self.debug("Step4 시작(대기 모드)")

        while True:
            for update in self._poll_updates():
                if "callback_query" in update:
                    self._handle_callback(update["callback_query"])
            time.sleep(1)

    def _handle_callback(self, query: Dict):
        callback_id = query.get("id")
        user_input = query.get("data", "")
        msg_id = query.get("message", {}).get("message_id")

        if callback_id:
            self.answer_callback_query(callback_id)
        if msg_id:
            self.remove_inline_keyboard(msg_id)

        if self.processing_lock:
            self.send("⚠️ 처리 중입니다.")
            return

        if user_input == REFRESH_CALLBACK:
            self._regenerate_topics()
        elif user_input in TOPIC_CHOICES:
            self._write_article(user_input)

    def _regenerate_topics(self):
        self.processing_lock = True
        try:
            self.send("🔄 주제 재생성 중...")
            new_topics = self.ai.get_5_topics(self.suggestion_data)
            self.last_topics = new_topics
            self.send(f"📝 재제안 주제\n\n{new_topics}", reply_markup=get_full_keyboard())
        finally:
            self.processing_lock = False

    def _write_article(self, user_input: str):
        self.processing_lock = True
        try:
            selected_title = (
                self.ai.extract_selected_title(user_input, self.last_topics)
                or f"{user_input}번 주제"
            )
            self.send(f"⏳ [{selected_title}] 분석 시작...")

            # 1) 대표 기사 선별
            rep_news = self.ai.pick_representative_news(selected_title, self.all_news, k=6)

            # 2) 팩트 불릿 (Perplexity / Local fallback)
            facts_bullets = ""
            if self.cfg.use_perplexity:
                facts_bullets = self.px.summarize_to_facts(selected_title, rep_news)

            if (not facts_bullets) or facts_bullets.startswith("❌"):
                self.send("⚠️ 로컬 팩트 불릿 사용.")
                facts_bullets = self.ai.fallback_facts(selected_title, rep_news)

            # 팩트 전문 파일 보관 + 텔레그램 전송
            facts_path = archive_facts(facts_bullets)
            self.send(f"🧾 팩트 확보 완료 ({len(facts_bullets)}자, 저장: {facts_path})")
            self.tg.send_long_message(self.chat_id, facts_bullets)

            # 3) 본문 생성 (Gemini) — END 마커 컷은 generate_article_from_facts
            #    내부의 parse_end 가 처리하므로 여기서 다시 자르지 않는다
            article = self.ai.generate_article_from_facts(selected_title, facts_bullets, rep_news)

            # 4) 본문 즉시 전송
            if article and "❌" not in article:
                self.tg.send_long_message(self.chat_id, article)
                self.send("✅ 본문 전송 완료.\n✨ 모든 작업 완료!")
            else:
                self.send("❌ 본문 생성 실패.")

        except Exception as e:
            logger.exception("글 생성 전체 예외")
            self.send(f"❌ 오류 발생: {e}")
        finally:
            self.processing_lock = False

    # ---------- 엔트리포인트 ----------
    def run(self):
        logger.info("main 시작")
        self._drain_pending_updates()
        self.send("🚀 뉴스 브리핑 봇 시작 (Text:Gemini)")

        if not self.keywords:
            self.keywords = self.ask_keywords()

        if not self.collect_news():
            self.send("❌ 수집된 뉴스가 없습니다.")
            return

        self.save_to_sheet()

        if not self.suggest_topics():
            return

        self.run_callback_loop()


def main():
    # 키워드는 매 실행마다 바뀌므로 명령줄 인자로 받는다 (생략 시 텔레그램에서 질문)
    keywords = parse_keywords(" ".join(sys.argv[1:])) if len(sys.argv) > 1 else None
    NewsBriefingBot(load_config(), keywords=keywords).run()


if __name__ == "__main__":
    main()
