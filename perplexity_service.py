import hashlib
import logging
import os
import random
import re
import time
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

MIN_BULLETS = 8          # 이보다 적으면 실패 처리
RETRY_THRESHOLD = 10     # 이보다 적으면 보강 프롬프트로 추가 시도
MAX_BULLETS = 16
MIN_BULLET_LEN = 18
DEDUPE_JACCARD = 0.62


class PerplexityService:
    """
    Perplexity를 '글쓰기'가 아니라 '팩트 불릿 압축기'로만 사용.

    - 입력: 대표 기사 6개(제목 + 상/하단 스니펫)
    - 출력: 중복 제거된 fact bullets(숫자/사례/발언 중심, 의견/전망 금지)
    """

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        debug: bool = False,
        timeout: int = 45,
        max_retries: int = 4,
    ):
        self.api_key = api_key
        self.base_url = "https://api.perplexity.ai/chat/completions"
        # Perplexity 모델은 계정/시점에 따라 다를 수 있어 ENV로도 덮어쓰기 가능
        self.model = model or os.getenv("PERPLEXITY_MODEL", "sonar")
        self.debug = debug
        self.timeout = timeout
        self.max_retries = max_retries

    # -----------------------------
    # Public API
    # -----------------------------
    def discover_trending_keywords(self, domain: str, n: int = 5) -> List[Dict]:
        """실시간 웹 검색으로 특정 분야에서 요즘 화제인 키워드 n개를 발굴한다.

        반환: [{"keyword": "...", "reason": "..."}, ...]  (실패 시 빈 리스트)
        """
        if not self.api_key:
            return []

        prompt = f"""
너는 트렌드 분석가다. 지금 시점 기준으로 '{domain}' 분야에서
한국 대중이 가장 관심을 많이 가질 만한 이슈/키워드 {n}개를 골라라.

[규칙]
- 각 줄을 정확히 "키워드 | 이유(한 문장)" 형식으로만 출력
- 키워드는 뉴스 검색에 바로 쓸 수 있는 짧은 명사구(2~10자 권장)
- 최신 이슈 우선, 너무 일반적인 단어(예: '{domain}') 자체는 제외
- 설명/서론/번호/불릿 없이 {n}줄만 출력
""".strip()

        raw = self._call_perplexity(prompt)
        if raw.startswith("❌"):
            logger.warning("트렌드 키워드 발굴 실패: %s", raw)
            return []

        results = []
        for ln in (ln.strip() for ln in raw.splitlines() if ln.strip()):
            # "키워드 | 이유" 파싱 (번호/불릿 접두어 제거)
            ln = re.sub(r"^(?:[-•*]|\d+[\)\.])\s*", "", ln)
            if "|" not in ln:
                continue
            kw, _, reason = ln.partition("|")
            kw = kw.strip().strip("\"'")
            if kw:
                results.append({"keyword": kw, "reason": reason.strip()})
        return results[:n]

    def summarize_to_facts(self, selected_title: str, rep_news: List[Dict]) -> str:
        if not self.api_key:
            return ""

        blocks = self._build_blocks(rep_news[:6])
        prompt = self._build_prompt(selected_title, blocks)

        raw = self._call_perplexity(prompt)
        if raw.startswith("❌"):
            return raw

        bullets = self._dedupe_bullets(self._normalize_bullets(raw, rep_news[:6]))

        # 불릿이 부족하면 보강 프롬프트로 한 번 더 시도
        if len(bullets) < RETRY_THRESHOLD:
            raw2 = self._call_perplexity(
                self._build_supplement_prompt(selected_title, blocks, bullets)
            )
            if not raw2.startswith("❌"):
                bullets = self._dedupe_bullets(
                    bullets + self._normalize_bullets(raw2, rep_news[:6])
                )

        bullets = bullets[:MAX_BULLETS]
        if len(bullets) < MIN_BULLETS:
            return "❌ Perplexity 팩트 불릿이 충분히 생성되지 않았습니다."

        out = "\n".join(f"- {b}" for b in bullets)
        if self.debug:
            self._debug_log(out)
        return out

    # -----------------------------
    # Prompt builders
    # -----------------------------
    def _build_blocks(self, rep_news: List[Dict]) -> str:
        blocks = []
        for i, n in enumerate(rep_news, 1):
            title = (n.get("title") or "").strip()
            snippet = ((n.get("top_content") or "") + " " + (n.get("bottom_content") or "")).strip()
            blocks.append(f"[기사{i}]\n제목: {title}\n스니펫: {snippet}")
        return "\n\n".join(blocks).strip()

    def _build_prompt(self, selected_title: str, blocks: str) -> str:
        return f"""
너는 기자가 아니라 '팩트 추출기'다.
아래 기사 스니펫에서 선택 제목과 직접 관련된 '사실'만 불릿으로 추출하라.

[선택 제목]
{selected_title}

[기사 스니펫]
{blocks}

[규칙]
- 출력은 오직 불릿( - 로 시작 )만
- 중복 사실은 하나로 통합(MECE)
- 숫자/기간/장소/주체/행동이 들어간 문장 우선
- 해석/의견/전망/조언/비유/수사 금지
- 가능한 경우 출처 기사 번호를 괄호로 표시: (출처: 기사1)
- 10~16개 불릿
- 불릿 외 문장(서론/결론/제목/요약) 출력 금지
""".strip()

    def _build_supplement_prompt(self, selected_title: str, blocks: str, existing_bullets: List[str]) -> str:
        existing = "\n".join(f"- {b}" for b in existing_bullets[:MAX_BULLETS])
        return f"""
너는 '팩트 추출기'다. 아래 기존 불릿은 이미 뽑았다.
기존 불릿과 겹치지 않는 새로운 사실만 추가로 불릿 6~10개를 만들어라.

[선택 제목]
{selected_title}

[기사 스니펫]
{blocks}

[기존 불릿(중복 금지)]
{existing}

[규칙]
- 출력은 오직 불릿만
- 중복 금지
- 해석/의견/전망 금지
- 각 불릿 끝에 (출처: 기사N) 필수
""".strip()

    # -----------------------------
    # HTTP call
    # -----------------------------
    def _call_perplexity(self, prompt: str) -> str:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "Extract only factual bullet points. Output bullets only."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }

        last_err = None
        for attempt in range(self.max_retries):
            try:
                time.sleep(random.uniform(0.4, 1.1))
                r = requests.post(self.base_url, headers=headers, json=payload, timeout=self.timeout)

                if r.status_code == 200:
                    return r.json()["choices"][0]["message"]["content"].strip()

                # 재시도 대상
                if r.status_code in (429, 503):
                    delay = min((2 ** attempt) + random.uniform(0, 0.5), 10)
                    logger.warning(
                        "Perplexity %s. %.1fs 후 재시도 (%d/%d)",
                        r.status_code, delay, attempt + 1, self.max_retries,
                    )
                    time.sleep(delay)
                    continue

                return f"❌ Perplexity 오류: {r.status_code} {r.text}"

            except Exception as e:
                last_err = e
                if attempt < self.max_retries - 1:
                    delay = min((2 ** attempt) + random.uniform(0, 0.5), 10)
                    logger.warning(
                        "Perplexity 예외: %s. %.1fs 후 재시도 (%d/%d)",
                        e, delay, attempt + 1, self.max_retries,
                    )
                    time.sleep(delay)
                    continue
                return f"❌ Perplexity 예외: {e}"

        return f"❌ Perplexity 실패: {last_err}"

    # -----------------------------
    # Normalization / Dedupe
    # -----------------------------
    def _normalize_bullets(self, raw: str, rep_news: List[Dict]) -> List[str]:
        """raw에서 불릿만 추출·정규화하고, 출처 표기가 없으면 추정해 보강한다."""
        bullets = []
        for ln in (ln.strip() for ln in raw.splitlines() if ln.strip()):
            # 명백한 서문/헤더 제거
            if re.match(r"^(what|why|how|결론|요약|제목)[:：]", ln, re.IGNORECASE):
                continue
            # "- ...", "• ...", "* ..." / "1) ...", "1. ..." 을 불릿으로 인정
            m = re.match(r"^(?:[-•*]|\d+[\)\.])\s+(.+)", ln)
            if m:
                bullets.append(m.group(1).strip())

        bullets = [self._ensure_source_tag(b, rep_news) for b in bullets]

        # 길이/품질 필터 (너무 짧거나 의견성 문장 제거)
        cleaned = []
        for b in bullets:
            if len(b) < MIN_BULLET_LEN:
                continue
            if any(x in b for x in ("전망", "예상", "될 것", "해야", "필요", "중요", "권장")):
                # 인용 사실("~라고 밝혔다" 등)이면 살려둠
                if not any(x in b for x in ("발언", "말했", "밝혔", "발표", "공개")):
                    continue
            cleaned.append(b)
        return cleaned

    def _ensure_source_tag(self, bullet: str, rep_news: List[Dict]) -> str:
        if re.search(r"\(출처:\s*기사\d+\)", bullet):
            return bullet

        # 토큰 겹침이 가장 큰 기사 번호를 출처로 추정
        bullet_tokens = self._tokens(bullet)
        best_i, best_score = 1, 0
        for i, n in enumerate(rep_news[:6], 1):
            text = " ".join([
                n.get("title", ""),
                n.get("top_content", ""),
                n.get("bottom_content", ""),
            ])
            score = len(bullet_tokens & self._tokens(text))
            if score > best_score:
                best_score, best_i = score, i

        return f"{bullet} (출처: 기사{best_i})"

    def _dedupe_bullets(self, bullets: List[str]) -> List[str]:
        """토큰 Jaccard 유사도가 임계치 이상이면 중복으로 제거."""
        kept, kept_tokens = [], []
        for b in bullets:
            t = self._tokens(re.sub(r"\(출처:\s*기사\d+\)", "", b))
            if not t:
                continue
            is_dup = any(
                (len(t & kt) / len(t | kt)) >= DEDUPE_JACCARD
                for kt in kept_tokens if t | kt
            )
            if not is_dup:
                kept.append(b)
                kept_tokens.append(t)
        return kept

    @staticmethod
    def _tokens(s: str) -> set:
        return set(re.findall(r"[0-9A-Za-z가-힣]{2,}", s.lower()))

    # -----------------------------
    # Debug
    # -----------------------------
    def _debug_log(self, out_text: str):
        bullet_count = sum(1 for ln in out_text.splitlines() if ln.strip().startswith("-"))
        fp = hashlib.md5(out_text.encode("utf-8")).hexdigest()[:8]
        logger.info(
            "Perplexity 팩트 불릿 생성 완료 (model=%s, bullets=%d, md5=%s)\n%s",
            self.model, bullet_count, fp, out_text[:900].rstrip(),
        )
