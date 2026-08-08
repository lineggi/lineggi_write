import logging
import re
import time
from typing import Dict, List

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

END_MARKER = "<END>"
LONG_MAX_TOKENS = 8192  # 본문(장문) 생성 시 출력 한도 — 기본값 잘림 방지


def _strip_em_dash(text: str) -> str:
    """본문에서 em/en dash(—, –, ―)를 제거한다(모델이 규칙을 어겨도 보장).

    앞뒤 공백을 정리하며 쉼표로 대체하고, 중복 쉼표·공백을 정돈한다.
    (가운뎃점 ·는 '7·27' 등 날짜·사건명에도 쓰여 여기서 건드리지 않는다.)
    """
    if not text:
        return text
    text = re.sub(r"\s*[—–―]\s*", ", ", text)   # em/en dash → 쉼표
    text = re.sub(r"\s*,\s*,", ",", text)         # 중복 쉼표 정리
    text = re.sub(r"[ \t]{2,}", " ", text)        # 중복 공백 정리
    return text

# 프롬프트가 '소비자' 사용을 금지하므로, 체크리스트 마지막 항목인
# '산업 관계자'를 폴백 종료 지점으로 삼는다. ('- ' 접두어/전각 콜론 허용)
_CHECKLIST_END_RE = re.compile(
    r"(액션 아이템\s*\(독자별 체크리스트\)[\s\S]*?^\s*-?\s*산업 관계자\s*[:：]\s*.*?$)",
    re.MULTILINE,
)
# 산업 관계자 줄이 누락된 출력을 위한 2차 폴백 (시장 관망자 줄까지)
_CHECKLIST_END_RE2 = re.compile(
    r"(액션 아이템\s*\(독자별 체크리스트\)[\s\S]*?^\s*-?\s*시장 관망자\s*[:：]\s*.*?$)",
    re.MULTILINE,
)


class AIGenerator:
    # 제목/요약 등 짧은 생성: 빠르고 저렴한 flash 우선
    PREFERRED_TEXT = ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]
    # 본문(장문) 생성: 논리·문장 완성도가 좋은 pro 우선, 없으면 flash 폴백
    PREFERRED_LONG = ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"]

    def __init__(self, google_api_key: str):
        self.client = genai.Client(api_key=google_api_key)
        available = self._list_models()
        self.model_id = self._choose_model(available, self.PREFERRED_TEXT)
        self.long_model_id = self._choose_model(available, self.PREFERRED_LONG)
        logger.info("모델 설정 완료: Text=%s / Long=%s", self.model_id, self.long_model_id)

    def _list_models(self) -> List[str]:
        available = []
        try:
            for m in self.client.models.list():
                name = getattr(m, "name", "")
                actions = getattr(m, "supported_actions", []) or []
                if name.startswith("models/") and "generateContent" in actions:
                    available.append(name.replace("models/", ""))
        except Exception:
            logger.warning("모델 목록 조회 실패 — 기본 모델 사용", exc_info=True)
        return available

    @staticmethod
    def _choose_model(available: List[str], preferred: List[str]) -> str:
        if not available:
            return preferred[-1]
        for p in preferred:
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
- 30자 이내, 모바일 피드에서 잘리지 않게.
- 구체성 최우선: 숫자·고유명사(기업/인물/코인명)·기간을 넣어 신뢰를 준다.
- 후킹은 '진짜 역설·의외성'으로 만든다(예상과 반대되는 사실, 숨겨진 원인, 임박한 마감).
- 결론을 다 말하지 말고 "왜?"를 남겨 클릭하게 만든다.

[제목 유형 — 5개를 서로 다른 유형으로 다양하게 섞어라(최소 3개는 숫자 포함)]
- 인용·위협형: "9분 만에 다 털린다" 구글이 폭로한 비트코인 약점
- 역설형: 전쟁 중 비트코인이 8% 오른 3가지 이유
- 미스터리형: 머스크家의 비트코인, 숨겨진 2가지 진실
- 마감·손실회피형: 2027년 과세 전, 지금 안 하면 후회할 준비

[피해야 할 것 — 어기면 감점]
- '충격·반전·대립·폭발' 같은 과장 클릭베이트 단어 남발 금지(신뢰를 깎는다).
- '~알아보자', '~무엇일까?' 같은 약한 맺음말 금지 → 단정형으로 끝낸다.
- 5개 제목이 전부 "~이유 N가지" 한 틀로 획일화되지 않게 한다.

[출력 형식 고정 — '제목:' 같은 라벨 없이 번호와 제목만]
1. (제목)
2. (제목)
3. (제목)
4. (제목)
5. (제목)
""".strip()
        raw = self._safe_generate(prompt)
        # 모델이 '제목:' 라벨을 붙여도 번호는 유지하고 라벨만 제거
        return re.sub(r"(?m)^(\s*\d+\.\s*)제목\s*[:：]\s*", r"\1", raw)

    # -----------------------------
    # 트렌드 키워드 추출 (헤드라인 기반)
    # -----------------------------
    def extract_trending_keywords(self, headlines: List[str], n: int = 5) -> List[Dict]:
        """코인데스크·코인니스 헤드라인에서 트렌드 키워드 n개를 뽑는다.

        반환: [{"keyword": "...", "reason": "..."}, ...] (실패 시 빈 리스트)
        """
        if not headlines:
            return []
        joined = "\n".join(f"- {h}" for h in headlines[:40])
        prompt = f"""
다음은 코인데스크·코인니스의 최신 크립토 뉴스 헤드라인이다.
한국 독자가 지금 가장 관심 가질 트렌드 키워드 {n}개를 골라라.

[헤드라인]
{joined}

[규칙]
- 각 줄을 정확히 "키워드 | 이유(한 문장)" 형식으로만 출력
- 키워드는 네이버 뉴스 검색에 바로 쓸 수 있는 한국어 명사구(2~12자)
- 영어 헤드라인은 한국어 검색어로 바꿔라 (예: Bitcoin ETF -> 비트코인 ETF)
- 최신·구체적 이슈 우선, 너무 일반적인 단어(예: 비트코인, 코인) 단독 사용 지양
- 설명/서론/번호/불릿 없이 {n}줄만 출력
""".strip()

        raw = self._safe_generate(prompt)
        if not raw or raw.startswith("❌"):
            return []

        results = []
        for ln in (ln.strip() for ln in raw.splitlines() if ln.strip()):
            ln = re.sub(r"^(?:[-•*]|\d+[\)\.])\s*", "", ln)  # 번호/불릿 접두어 제거
            if "|" not in ln:
                continue
            kw, _, reason = ln.partition("|")
            kw = kw.strip().strip("\"'")
            if kw:
                results.append({"keyword": kw, "reason": reason.strip()})
        return results[:n]

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

        # 제목에 숫자가 있으면 그 개수로 고정, 없으면(역설형/인용형 제목 등)
        # 내용 흐름에 맞게 2~4개 중 선택하도록 유연하게 지시
        explicit_n = self.extract_n_count(selected_title, default=0)
        if explicit_n:
            n_text = f"{explicit_n}가지"
            n_para_text = f"{explicit_n}개의 문단 작성."
        else:
            n_text = "N가지 (N은 2~4 중 내용에 맞게 선택)"
            n_para_text = "선택한 N개의 문단 작성."

        # 팩트 불릿의 (출처: 기사N) 표기와 같은 라벨을 사용해 매핑 혼동 방지
        citations_text = "\n".join(f"기사{i}: {n.get('title', '')}" for i, n in enumerate(rep_news[:6], 1))

        prompt = f"""
너는 브런치 작가이자 가상자산 전문 리서처다.
존댓말을 쓰지말고 독자가 끝까지 읽게 만드는 몰입감 있는 어조로 작성하되, 전문성을 위해 평어체('~다', '~한다')를 사용해라.
아래 '팩트 불릿'에 있는 사실만 근거로 작성하고, 절대 없는 사실을 지어내지 마라.

[표절 방지 — 반드시 지켜라]
- 팩트 불릿은 '내용(사실)'만 참고하고, 문장 표현·어순·구조는 절대 그대로 베끼지 마라.
- 모든 문장을 네 문체로 새로 써라. 팩트의 문장을 복사하거나 몇 단어만 바꿔 쓰는 것 금지.
- 숫자·고유명사·회사명 등 사실 자체는 그대로 써도 되지만, 그 사실을 담는 문장은 반드시 재구성하라.
- 원문의 직접 인용("...")을 그대로 옮기지 말고, 발언의 취지를 네 말로 풀어써라.

[AI 티 나는 문체 금지 — 어기면 감점]
한국인이 실제로 쓰는 자연스러운 글처럼 써라. 아래 4가지는 절대 피한다.
1) 번역투·의인화·수동태 금지.
   - 사물 의인화 금지 (예: "AI가 올라탈 바닥", "이 모든 게 굴러가는 데는 두 가지 장치가 깔려 있다").
   - 한국에서 잘 안 쓰는 번역 관용구 금지 (예: "아무리 ~해도 지나치지 않다", "사다리의 두 칸",
     "적어 둔 노트", "평행 사례", "솔직한 토로였다").
   - 수동태 남발 금지. 능동태로 주어가 행동하게 써라.
2) 상투적 요약·단언의 반복 금지.
   - "한 마디로 말하면 이것이다", "핵심은 N가지다", "이것이 정답이다" 같은 문구를
     문단마다 되풀이하지 마라. 요약 표현은 글 전체에서 최소한으로만.
3) 평균으로 수렴하는 뻔한 구조 금지.
   - 주제와 동떨어진 일상 비유 금지 (예: "건너야 할 다리의 길이를 재는 일",
     "여섯 가지 질문을 책상에 올려놓아 보시기를 권한다").
   - 모든 걸 습관적으로 3개로 나누지 마라. 장문·단문을 규칙적으로 번갈아 리듬을 만들지 마라.
4) 군더더기 선언문 금지.
   - 중요하지 않은 인용 앞에 "처음 든 생각은 이거였다", "발표를 시작하며 한 말이 있다" 같은
     선언을 붙이지 마라. 바로 본론으로 들어가라.

[입력 데이터]
- 제목 키워드: {short_title}
- 핵심 항목 수: {n_text}
- 참고 기사: {citations_text}
- 팩트 불릿: {facts_bullets}

[출력 구조 고정]
1) 제목 한 줄:
   - {short_title}을 활용해 클릭을 유도하는 제목 한 줄로 다듬어 출력 (장식 금지).
   - 구체적 숫자·고유명사·기간을 넣고, 예상 밖의 역설이나 숨겨진 원인으로 궁금증을 남긴다.
   - '충격/반전/대립/폭발' 같은 과장 단어와 '~알아보자/무엇일까' 같은 약한 맺음말은 쓰지 않는다.
2) 3줄 요약 (각 줄 30자 이내):
   - Why: (원인/배경),이 글을 읽어야 하는 이유 한 문장.
   - What: (현상), 무슨 내용의 글인지 한 문장
   - How: (대응), 어떻게 하라는 건지 한 문장
3) 오프닝 훅: 3~4문장
4) What 섹션 (섹션명: 현상에 대한 직관적 제목):
   - 본문 3~4문단.
   - 문단 끝에 [지금 체크할 점] 포함.
5) Why 섹션 (섹션명: 핵심 원인 {n_text}):
   - {n_para_text}
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
- 3줄 요약의 Why/What/How 라벨은 그대로 출력하되, 그 외 본문에서는 라벨(현상/원인 등) 직접 출력 금지.

[문장 규칙]
- 문장 안에서 쉼표(,) 사용을 최소화한다. 삽입구·연결용 쉼표 대신 문장을 짧게 끊어라
  (예: "정부는 규제를 준비 중이고, 시장은 혼란스럽다" → "정부는 규제를 준비 중이다. 시장은 혼란스럽다").
- 단, 여러 항목을 나열할 때 쓰는 쉼표(예: "송금, 결제, 자산관리")는 그대로 허용한다.
- Em dash(—, –, ―) 절대 사용 금지. 문장을 나눌 땐 마침표로 끊고, 삽입은 쉼표로 처리하라.
- 항목을 나열할 때 가운뎃점(·)을 쓰지 말고 쉼표(,)나 '와/과'로 이어라
  (예: "정보보안·네트워크" → "정보보안과 네트워크", "구글·팔란티어" → "구글, 팔란티어").
""".strip()
        return _strip_em_dash(self.parse_end(self._safe_generate(prompt, is_long=True)))

    def parse_end(self, text: str) -> str:
        """END 마커(1순위) 또는 체크리스트 마지막 항목(폴백) 뒤를 잘라낸다."""
        if not text:
            return "❌ 빈 응답"
        if END_MARKER in text:
            return text.split(END_MARKER, 1)[0].rstrip()
        for pattern in (_CHECKLIST_END_RE, _CHECKLIST_END_RE2):
            m = pattern.search(text)
            if m:
                return text[: m.end()].rstrip()
        return text.rstrip()

    def _safe_generate(self, prompt: str, is_long: bool = False, retries: int = 3) -> str:
        # 장문은 pro 모델 + 출력 토큰 한도 상향 (본문이 중간에 잘리는 것 방지)
        model = self.long_model_id if is_long else self.model_id
        config = types.GenerateContentConfig(max_output_tokens=LONG_MAX_TOKENS) if is_long else None

        for attempt in range(1, retries + 1):
            try:
                time.sleep(1)
                resp = self.client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
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
