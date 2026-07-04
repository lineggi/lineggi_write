"""환경변수 기반 설정 로더.

코드에 키/토큰/챗ID를 하드코딩하지 않고 전부 환경변수에서 읽는다.
필수 값이 없으면 시작 시점에 바로 에러를 내서, 실행 도중에
"토큰"/"키" 같은 더미 값으로 API를 호출하다 실패하는 일을 막는다.
"""
import os
from dataclasses import dataclass


DEFAULT_KEYWORDS = ("솔라나", "토스뱅크", "스테이블코인", "블록체인 해외송금")


@dataclass(frozen=True)
class Config:
    telegram_token: str
    gemini_api_key: str
    openai_api_key: str
    perplexity_api_key: str
    my_chat_id: str
    sheet_name: str
    keywords: tuple
    use_perplexity: bool
    debug_facts_preview: bool
    debug_telegram_ping: bool
    news_per_keyword: int


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"환경변수 {name} 이(가) 설정되지 않았습니다. .env 를 확인하세요.")
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


def load_config() -> Config:
    use_perplexity = _bool_env("USE_PERPLEXITY", True)

    raw_keywords = os.getenv("KEYWORDS", "")
    keywords = tuple(k.strip() for k in raw_keywords.split(",") if k.strip()) or DEFAULT_KEYWORDS

    return Config(
        telegram_token=_require("TELEGRAM_TOKEN"),
        gemini_api_key=_require("GEMINI_API_KEY"),
        openai_api_key=_require("OPENAI_API_KEY"),
        perplexity_api_key=_require("PERPLEXITY_API_KEY") if use_perplexity else "",
        my_chat_id=_require("MY_CHAT_ID"),
        sheet_name=os.getenv("SHEET_NAME", "AI_Writing_Brunch"),
        keywords=keywords,
        use_perplexity=use_perplexity,
        debug_facts_preview=_bool_env("DEBUG_FACTS_PREVIEW", True),
        debug_telegram_ping=_bool_env("DEBUG_TELEGRAM_PING", True),
        news_per_keyword=int(os.getenv("NEWS_PER_KEYWORD", "10")),
    )
