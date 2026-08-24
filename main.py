"""뉴스 브리핑 봇 진입점.

키워드는 매 실행마다 달라지므로 .env 에 두지 않는다:
  1) 명령줄:  python main.py "솔라나,토스뱅크"
  2) 생략 시: 봇이 텔레그램으로 키워드를 물어보고 답장을 기다림
"""
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

import requests
from dotenv import load_dotenv

# .env 파일의 키들을 환경변수로 로드 (다른 모듈 import 전에 실행 —
# crawler 등이 import 시점에 환경변수를 읽기 때문)
load_dotenv()

from ai_generator import AIGenerator
from crawler import naver_crawler
from naver_service import get_news_content, save_news_to_sheet
from perplexity_service import PerplexityService
from telegram_service import TelegramService
from trend_service import fetch_trend_headlines

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

NEWS_PER_KEYWORD = 10           # 키워드당 수집 기사 수
TOPIC_RANK_LIMIT = 5            # 주제 제안에 사용할 키워드별 상위 기사 수
TOPIC_CHOICES = {"1", "2", "3", "4", "5"}
REFRESH_CALLBACK = "R"
NEWKW_CALLBACK = "NEWKW"        # 키워드부터 다시 입력
FACTS_LOG_DIR = "logs"          # 팩트 전문 보관 위치

# 모든 글 끝에 붙는 고정 강의 홍보 CTA (링크는 cfg.lecture_url 로 별도 부착)
CTA_BODY = (
    "블록체인, 아직 '비트코인'이 전부라고 생각하시나요? 사실 블록체인은 금융과 의료, 공공, "
    "물류까지 이미 세상을 바꾸고 있습니다. 뉴스로 흐름만 좇으면 정작 그 안의 진짜 변화는 놓치기 쉽습니다.\n\n"
    "크립토유치원 강의는 코인 투자 경험이 없어도, 개발자가 아니어도 블록체인을 제대로 이해하도록 만들었습니다. "
    "코딩 없이 원리를 배우고 실제 서비스 사례로 감을 잡을 수 있습니다. "
    "블록체인을 처음 접하는 초보라면 지금 시작해보세요."
)

# 리드마그넷: 구독 + 이메일 댓글 시 뉴스 시트 링크를 별도 공유
SHEET_OFFER = (
    "브런치 구독 후 댓글에 이메일을 남겨주시면, "
    "제가 매일 정리하는 크립토 뉴스 시트를 무료로 보내드립니다."
)


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
    trend_domain: str        # 키워드 추천 기본 분야 (예: 크립토)
    lecture_url: str         # 글 끝에 붙일 강의 판매 링크


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
        sheet_name=os.getenv("SHEET_NAME", "Brunch_news"),
        use_perplexity=_bool_env("USE_PERPLEXITY", True),
        debug_telegram_ping=_bool_env("DEBUG_TELEGRAM_PING", True),
        trend_domain=os.getenv("TREND_DOMAIN", "크립토"),
        lecture_url=os.getenv("LECTURE_URL", "https://megastudyacademy.co.kr/camp/lecture/805"),
    )


# =========================
# 순수 유틸리티 (상태 의존 X)
# =========================
def get_full_keyboard() -> str:
    keyboard = {
        "inline_keyboard": [
            [{"text": f"{i}번 선택", "callback_data": str(i)} for i in sorted(TOPIC_CHOICES)],
            [{"text": "🔄 주제 다시 제안받기", "callback_data": REFRESH_CALLBACK}],
            [{"text": "🆕 키워드 다시 입력하기", "callback_data": NEWKW_CALLBACK}],
        ]
    }
    return json.dumps(keyboard)


def parse_keywords(text: str) -> List[str]:
    """'솔라나, 토스뱅크' 같은 쉼표 구분 문자열을 키워드 리스트로 변환."""
    return [k.strip() for k in (text or "").split(",") if k.strip()]


def attach_source_dates(facts: str, rep_news: List[Dict]) -> str:
    """팩트 불릿의 '(출처: 기사N)' 뒤에 해당 기사 발행일(YYYY-MM-DD)을 붙인다.

    예) (출처: 기사1) -> (출처: 기사1, 2026-07-04)
    """
    def _repl(m):
        idx = int(m.group(1))
        if 1 <= idx <= len(rep_news):
            date = (rep_news[idx - 1].get("pub_date") or "")[:10]
            if date:
                return f"(출처: 기사{idx}, {date})"
        return m.group(0)

    return re.sub(r"\(출처:\s*기사(\d+)\)", _repl, facts)


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
        self.recommended_keywords: List[str] = []  # 번호 선택용 추천 키워드
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

    def _wait_for_text(self) -> str:
        """내 채팅에서 다음 텍스트 메시지 한 건을 기다려 반환한다."""
        while True:
            for update in self._poll_updates():
                msg = update.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
                    continue
                text = (msg.get("text") or "").strip()
                if text:
                    return text
            time.sleep(1)

    # ---------- Step 0) 트렌드 소스 선택 & 키워드 추천 ----------
    def _source_keyboard(self) -> str:
        return json.dumps({
            "inline_keyboard": [
                [{"text": "📰 트렌드 추천받기 (코인데스크·코인니스)", "callback_data": "src:trend"}],
                [{"text": "✍️ 직접 입력하기", "callback_data": "src:manual"}],
            ]
        })

    def choose_source_and_recommend(self):
        """시작 시 '트렌드 추천' vs '직접 입력'을 버튼으로 고르게 하고,
        트렌드를 고르면 코인데스크·코인니스 헤드라인에서 키워드를 추천한다."""
        self.send(
            "👋 오늘 글, 어떻게 시작할까요?\n"
            "• 📰 트렌드 추천받기 — 코인데스크·코인니스 최신 뉴스에서 키워드를 뽑아드려요\n"
            "• ✍️ 직접 입력하기 — 원하는 키워드를 직접 입력",
            reply_markup=self._source_keyboard(),
        )

        # 소스 선택 콜백 대기
        source = None
        while source is None:
            for update in self._poll_updates():
                cq = update.get("callback_query")
                if not cq:
                    continue
                data = cq.get("data", "")
                if cq.get("id"):
                    self.answer_callback_query(cq["id"])
                mid = cq.get("message", {}).get("message_id")
                if mid:
                    self.remove_inline_keyboard(mid)
                if data == "src:trend":
                    source = "trend"
                elif data == "src:manual":
                    source = "manual"
            if source is None:
                time.sleep(1)

        if source == "manual":
            return  # ask_keywords 가 직접 입력을 받는다

        # 트렌드: 코인데스크·코인니스 헤드라인 -> AI 키워드 추출
        self.send("📰 코인데스크·코인니스에서 최신 트렌드를 수집하는 중...")
        headlines = fetch_trend_headlines()
        recs = self.ai.extract_trending_keywords(headlines) if headlines else []
        if not recs:
            self.send("⚠️ 트렌드를 못 가져왔어요. 원하는 키워드를 직접 입력해주세요.")
            return

        # 버튼/번호로 선택할 수 있도록 추천 키워드를 순서대로 보관
        self.recommended_keywords = [r["keyword"] for r in recs]
        lines = "\n".join(f"{i}. {r['keyword']} — {r['reason']}" for i, r in enumerate(recs, 1))
        self.send(
            f"💡 지금 크립토 트렌드\n\n{lines}\n\n👇 원하는 키워드를 눌러주세요.",
            reply_markup=self._keyword_keyboard(),
        )

    def _keyword_keyboard(self) -> str:
        """추천 키워드를 한 줄에 하나씩 버튼으로. callback_data는 인덱스(kw:i)."""
        rows = [
            [{"text": kw, "callback_data": f"kw:{i}"}]
            for i, kw in enumerate(self.recommended_keywords)
        ]
        rows.append([{"text": "✍️ 직접 입력하기", "callback_data": "kw:manual"}])
        return json.dumps({"inline_keyboard": rows})

    def _resolve_keywords(self, text: str) -> List[str]:
        """입력 텍스트를 키워드 리스트로 변환.

        추천 목록의 번호(예: '2' 또는 '1,3')를 입력하면 해당 키워드로 치환하고,
        그 외에는 입력한 텍스트를 그대로 키워드로 쓴다.
        """
        tokens = parse_keywords(text)
        resolved = []
        for tok in tokens:
            if tok.isdigit() and 1 <= int(tok) <= len(self.recommended_keywords):
                resolved.append(self.recommended_keywords[int(tok) - 1])
            else:
                resolved.append(tok)
        return resolved

    def ask_keywords(self) -> List[str]:
        """키워드를 버튼(콜백) 또는 텍스트(번호/직접입력)로 받는다."""
        if not self.recommended_keywords:
            self.send(
                "🔍 수집할 키워드를 쉼표(,)로 구분해 입력해주세요.\n"
                "예) 솔라나,토스뱅크,스테이블코인"
            )
        # else: 버튼 안내는 choose_source_and_recommend 에서 이미 보냄

        while True:
            for update in self._poll_updates():
                # 1) 버튼 탭(콜백)
                cq = update.get("callback_query")
                if cq:
                    kws = self._handle_keyword_callback(cq)
                    if kws:
                        return kws
                    continue
                # 2) 텍스트 입력(번호 또는 직접 입력)
                msg = update.get("message") or {}
                if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
                    continue
                text = (msg.get("text") or "").strip()
                if not text:
                    continue
                keywords = self._resolve_keywords(text)
                if keywords:
                    self.send(f"✅ 키워드 설정: {', '.join(keywords)}")
                    return keywords
                self.send("⚠️ 키워드를 인식하지 못했어요. 버튼을 누르거나 키워드를 입력해주세요.")
            time.sleep(1)

    def _handle_keyword_callback(self, cq: Dict) -> List[str]:
        """키워드 선택 버튼 콜백 처리. 선택되면 [키워드] 반환, 아니면 빈 리스트."""
        data = cq.get("data", "")
        if cq.get("id"):
            self.answer_callback_query(cq["id"])
        msg_id = cq.get("message", {}).get("message_id")
        if msg_id:
            self.remove_inline_keyboard(msg_id)

        if data == "kw:manual":
            self.send("🔍 키워드를 쉼표(,)로 구분해 입력해주세요.\n예) 솔라나,토스뱅크")
            return []
        if data.startswith("kw:"):
            idx = data[3:]
            if idx.isdigit() and 0 <= int(idx) < len(self.recommended_keywords):
                kw = self.recommended_keywords[int(idx)]
                self.send(f"✅ 키워드 설정: {kw}")
                return [kw]
        return []

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

                self.all_news.append(self._build_news_item(item, kw, rank, top_txt, bottom_txt))

        return bool(self.all_news)

    @staticmethod
    def _build_news_item(item: Dict, keyword: str, rank: int, top_txt: str, bottom_txt: str) -> Dict:
        return {
            "pub_date": item.get("pub_time", datetime.now().strftime("%Y-%m-%d %H:%M")),
            "keyword": keyword,
            "relevance_rank": rank,
            "total_count": item.get("total_count", ""),  # 시트 D열(주제검색량)
            "title": item.get("title", "제목 없음"),
            "link": item.get("link", ""),
            "top_content": top_txt,
            "bottom_content": bottom_txt,
        }

    @staticmethod
    def _usable_count(news: List[Dict]) -> int:
        """본문(상단/하단)이 실제로 채워진 기사 수 — 팩트 추출에 쓸 수 있는 기사."""
        return sum(1 for n in news if (n.get("top_content") or n.get("bottom_content")))

    def _supplement_news(self, selected_title: str, max_add: int = 6) -> None:
        """선택한 제목으로 네이버 뉴스를 추가 검색해 self.all_news 에 병합한다.

        기존과 중복(link)되는 기사는 제외하고, 본문까지 추출해 담는다.
        """
        query = self.ai._strip_topic_prefix(selected_title) or selected_title
        try:
            items = naver_crawler.get_news(query)
        except Exception:
            logger.exception("추가 뉴스 수집 실패 (query=%s)", query)
            return

        existing_links = {n.get("link") for n in self.all_news}
        base_rank = len(self.all_news)
        added = 0
        for item in items:
            link = item.get("link", "")
            if not link or link in existing_links:
                continue
            try:
                top_txt, bottom_txt = get_news_content(link)
            except Exception:
                top_txt, bottom_txt = "", ""
            self.all_news.append(
                self._build_news_item(item, query, base_rank + added + 1, top_txt, bottom_txt)
            )
            existing_links.add(link)
            added += 1
            if added >= max_add:
                break

        logger.info("제목 기반 추가 수집: %d건 (query=%s)", added, query)

    # ---------- Step 2) 시트 저장 ----------
    def save_to_sheet(self):
        self.debug("Step2 시작(시트 저장)")
        # 본문은 collect_news 에서 이미 수집했으므로 여기서 재크롤링하지 않는다
        result = save_news_to_sheet(self.cfg.sheet_name, self.all_news)
        # 성공이든 실패든 결과를 텔레그램으로 알려 조용한 실패를 막는다
        self.send(result)

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
        elif user_input == NEWKW_CALLBACK:
            self._restart_with_new_keywords()
        elif user_input in TOPIC_CHOICES:
            self._write_article(user_input)

    def _restart_with_new_keywords(self):
        """봇 종료 없이 키워드 입력부터 다시 진행한다(소스 선택 → 수집 → 주제 제안)."""
        self.processing_lock = True
        try:
            # 이전 실행 상태 초기화
            self.keywords = []
            self.recommended_keywords = []
            self.all_news = []
            self.suggestion_data = []
            self.last_topics = ""

            self.choose_source_and_recommend()
            self.keywords = self.ask_keywords()

            if not self.collect_news():
                retry_kb = json.dumps({
                    "inline_keyboard": [
                        [{"text": "🆕 키워드 다시 입력하기", "callback_data": NEWKW_CALLBACK}]
                    ]
                })
                self.send("❌ 수집된 뉴스가 없습니다. 다른 키워드로 다시 시도해주세요.", reply_markup=retry_kb)
                return
            self.save_to_sheet()
            self.suggest_topics()
        finally:
            self.processing_lock = False

    def _regenerate_topics(self):
        self.processing_lock = True
        try:
            self.send("🔄 주제 재생성 중...")
            new_topics = self.ai.get_5_topics(self.suggestion_data)
            self.last_topics = new_topics
            self.send(f"📝 재제안 주제\n\n{new_topics}", reply_markup=get_full_keyboard())
        finally:
            self.processing_lock = False

    def _append_cta(self, article: str) -> str:
        """본문 끝에 고정 강의 홍보 CTA와 판매 링크를 붙인다."""
        return (
            f"{article.rstrip()}\n\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{CTA_BODY}\n\n"
            f"크립토유치원 강의 보러 가기\n{self.cfg.lecture_url}\n\n"
            f"{SHEET_OFFER}"
        )

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

            # 1-1) 본문 있는 기사가 1개뿐이면, 제목으로 관련 기사를 추가 수집
            if self._usable_count(rep_news) < 2:
                self.send("🔎 관련 기사가 1개뿐이라, 제목과 관련된 기사를 추가로 수집합니다...")
                self._supplement_news(selected_title)
                rep_news = self.ai.pick_representative_news(selected_title, self.all_news, k=6)
                usable = self._usable_count(rep_news)
                self.send(f"📎 활용 가능한 관련 기사 {usable}건 확보")
                if usable < 2:
                    self.send("⚠️ 그래도 관련 기사가 부족합니다(속보일 수 있음). 발행 전 사실을 꼭 교차 확인하세요.")

            # 2) 팩트 불릿 (Perplexity / Local fallback)
            facts_bullets = ""
            if self.cfg.use_perplexity:
                facts_bullets = self.px.summarize_to_facts(selected_title, rep_news)

            used_fallback = (not facts_bullets) or facts_bullets.startswith("❌")
            if used_fallback:
                # 근거 기사 자체가 너무 적으면 부실한 글이 나오므로 중단
                if len(rep_news) < 3:
                    self.send("❌ 근거 기사가 부족해 본문 생성을 중단합니다. 다른 주제를 선택해주세요.")
                    return
                self.send(
                    "⚠️ Perplexity 팩트 수집 실패 — 기사 제목만 근거로 작성합니다.\n"
                    "숫자·인용 등 세부 사실은 발행 전 반드시 직접 확인하세요!"
                )
                facts_bullets = self.ai.fallback_facts(selected_title, rep_news)

            # 각 팩트의 출처 기사 발행일을 (출처: 기사N, YYYY-MM-DD) 로 붙인다
            facts_bullets = attach_source_dates(facts_bullets, rep_news)

            # 팩트 전문 파일 보관 + 텔레그램 전송
            facts_path = archive_facts(facts_bullets)
            facts_label = "팩트(기사 제목 기반)" if used_fallback else "팩트 확보 완료"
            self.send(f"🧾 {facts_label} ({len(facts_bullets)}자, 저장: {facts_path})")
            self.tg.send_long_message(self.chat_id, facts_bullets)

            # 3) 본문 생성 (Gemini) — END 마커 컷은 generate_article_from_facts
            #    내부의 parse_end 가 처리하므로 여기서 다시 자르지 않는다
            article = self.ai.generate_article_from_facts(selected_title, facts_bullets, rep_news)

            # 3-1) 강의 홍보 CTA + 판매 링크를 항상 글 끝에 붙인다
            if article and "❌" not in article:
                article = self._append_cta(article)

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
            self.choose_source_and_recommend()
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
