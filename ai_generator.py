import logging
import re
import time
from typing import Dict, List

from google import genai
from openai import OpenAI

logger = logging.getLogger(__name__)

END_MARKER = "<END>"
# 프롬프트가 '소비자' 사용을 금지하므로, 체크리스트 마지막 항목인
# '산업 관계자'를 폴백 종료 지점으로 삼는다. (기존 '소비자 :' 정규식은
# 프롬프트 규칙과 모순되어 절대 매치되지 않던 죽은 코드였음)
_CHECKLIST_END_RE = re.compile(
    r"(액션 아이템\s*\(독자별 체크리스트\)[\s\S]*?^\s*산업 관계자\s*:\s*.*?$)",
    re.MULTILINE,
)


class AIGenerator:
    def __init__(self, google_api_key: str, openai_api_key: str):
        self.client = genai.Client(api_key=google_api_key)

        if openai_api_key:
            self.openai_client = OpenAI(api_key=openai_api_key)
        else:
            self.openai_client = None
            logger.warning("OpenAI 키가 없어 이미지 생성이 불가능합니다.")

        self.preferred = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
        self.model_id = self._pick_model()
        logger.info("모델 설정 완료: Text=%s / Image=DALL·E", self.model_id)

    def _pick_model(self) -> str:
        available = []
        try:
            for m in self.client.models.list():
                name = getattr(m, "name", "")
                actions = getattr(m, "supported_actions", []) or []
                if name.startswith("models/") and "generateContent" in actions:
                    available.append(name.replace("models/", ""))
        except Exception:
            logger.warning("모델 목록 조회 실패 — 기본 모델 사용", exc_info=True)
            return self.preferred[-1]
        if not available:
            return self.preferred[-1]
        for p in self.preferred:
            if p in available:
                return p
        return available[0]

    # -----------------------------
    # (REQ-1) 주제 제안
    # -----------------------------
    def get_5_topics(self, suggestion_data: List[str]) -> str:
        if not suggestion_data:
            return "❌ 뉴스 데이터가 없습니다."
        context_data = "\n\n".join(suggestion_data)

        prompt = f"""
너는 브런치 제목만 뽑는 편집자다. 아래 뉴스 제목 묶음을 보고 제목 5개를 만든다.

[분석 데이터]
{context_data}

[핵심 규칙]
- 제목은 무조건 숫자 포함
- 패턴: A) "<핵심 주제>, ~하는 N가지 방법", B) "<핵심 주제>, ~이 아닌 이유 N가지", C) "<핵심 주제>, ~인 N가지 이유", D) "~라면 누구나 알아야 할 <핵심 주제>"
- 과장/단정 금지. 30자 이내.

[출력 형식 고정]
1. 제목: ...
2. 제목: ...
3. 제목: ...
4. 제목: ...
5. 제목: ...
""".strip()
        return self._safe_generate(prompt)

    # -----------------------------
    # 유틸리티
    # -----------------------------
    def extract_selected_title(self, selected_num: str, topics_text: str) -> str:
        if not topics_text:
            return ""
        for ln in (ln.strip() for ln in topics_text.splitlines() if ln.strip()):
            if ln.startswith(f"{selected_num}."):
                return ln
        return ""

    def extract_n_count(self, selected_title: str, default: int = 3) -> int:
        raw = self._strip_topic_prefix(selected_title)
        m = re.search(r"(\d+)\s*가지", raw)
        return int(m.group(1)) if m else default

    def _tokens(self, text: str) -> set:
        if not text:
            return set()
        return set(re.findall(r"[0-9A-Za-z가-힣]{2,}", text.lower()))

    def pick_representative_news(self, selected_title: str, all_news: List[Dict], k: int = 6) -> List[Dict]:
        if not all_news:
            return []
        title_text = self._strip_topic_prefix(selected_title)
        title_tokens = self._tokens(title_text)

        scored = []
        for n in all_news:
            score = 0.0
            kw = (n.get("keyword") or "").strip()
            if kw and kw in title_text:
                score += 3.0
            overlap = len(title_tokens & self._tokens((n.get("title") or "").strip()))
            score += min(overlap * 1.2, 6.0)
            scored.append((score, n))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [x[1] for x in scored[:k]]

    def fallback_facts(self, selected_title: str, rep_news: List[Dict]) -> str:
        if not rep_news:
            return "- 뉴스 데이터 없음"
        return "\n".join(f"- {n.get('title', '')} (출처: 기사)" for n in rep_news[:6])

    # -----------------------------
    # (REQ-3) 본문 생성
    # -----------------------------
    def generate_article_from_facts(self, selected_title: str, facts_bullets: str, rep_news: List[Dict]) -> str:
        short_title = self._strip_topic_prefix(selected_title)
        n_count = self.extract_n_count(selected_title, default=3)
        citations_text = "\n".join(f"N{i}: {n.get('title', '')}" for i, n in enumerate(rep_news[:6], 1))

        prompt = f"""
너는 브런치 작가이자 가상자산 전문 리서처다.
존댓말을 쓰지말고 독자가 끝까지 읽게 만드는 몰입감 있는 어조로 작성하되, 전문성을 위해 평어체('~다', '~한다')를 사용해라.
아래 '팩트 불릿'에 있는 사실만 근거로 작성하고, 절대 없는 사실을 지어내지 마라.

[입력 데이터]
- 제목 키워드: {short_title}
- 핵심 항목 수(N): {n_count}
- 참고 기사: {citations_text}
- 팩트 불릿: {facts_bullets}

[출력 구조 고정]
1) 제목 한 줄:
   - {short_title}을 활용하되, "충격", "반전", "대립" 키워드를 넣어 클릭을 유도하는 매력적인 문장으로 다듬어 출력 (장식 금지)
2) 3줄 요약 (각 줄 30자 이내):
   - Why: (원인/배경),이 글을 읽어야 하는 이유 한 문장.
   - What: (현상), 무슨 내용의 글인지 한 문장
   - How: (대응), 어떻게 하라는 건지 한 문장
3) 오프닝 훅: 3~4문장
4) What 섹션 (섹션명: 현상에 대한 직관적 제목):
   - 본문 3~4문단.
   - 문단 끝에 [지금 체크할 점] 포함.
5) Why 섹션 (섹션명: 핵심 원인 {n_count}가지):
   - {n_count}개의 문단 작성.
6) How 섹션 (섹션명: 생존 및 대응 전략):
   - 실행 가능한 로드맵 3가지.
7) 액션 아이템 (독자별 체크리스트):
   - 투자자(홀더):
   - 시장 관망자:
   - 산업 관계자:
8) 마지막 줄에 "{END_MARKER}" 단독 출력

[금지 사항]
- ###, ** 등 마크다운 장식 절대 금지.
- '소비자' 대신 '홀더', '시장 참여자' 사용.
- 라벨(현상/원인 등) 직접 출력 금지.
""".strip()
        return self.parse_end(self._safe_generate(prompt, is_long=True))

    def parse_end(self, text: str) -> str:
        """END 마커(1순위) 또는 체크리스트 마지막 항목(폴백) 뒤를 잘라낸다."""
        if not text:
            return "❌ 빈 응답"
        if END_MARKER in text:
            return text.split(END_MARKER, 1)[0].rstrip()
        m = _CHECKLIST_END_RE.search(text)
        if m:
            return text[: m.end()].rstrip()
        return text.rstrip()

    def _safe_generate(self, prompt: str, is_long: bool = False, retries: int = 3) -> str:
        for attempt in range(1, retries + 1):
            try:
                time.sleep(1)
                resp = self.client.models.generate_content(model=self.model_id, contents=prompt)
                if resp.text:
                    return resp.text.strip()
            except Exception:
                logger.warning("생성 실패 (시도 %d/%d)", attempt, retries, exc_info=True)
                time.sleep(3)
        return "❌ 생성 실패"

    def _strip_topic_prefix(self, topic_line: str) -> str:
        if not topic_line:
            return ""
        s = re.sub(r"^\d+\.\s*", "", topic_line.strip())
        s = re.sub(r"^제목\s*:\s*", "", s)
        return s.strip()
