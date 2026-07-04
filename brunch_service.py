"""브런치 작가 페이지에서 최근 발행 글 제목을 수집한다.

제목 생성(get_5_topics) 시 "내 기존 인기 제목 스타일"을 참고자료로
넣어주기 위한 용도. 네트워크/마크업 변경에 대비해 여러 전략으로
best-effort 추출하고, 실패하면 빈 리스트를 반환한다(글 생성은 계속됨).
"""
import logging
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
# 제목이 아닌 UI 텍스트를 걸러내기 위한 최소 길이/블랙리스트
_MIN_TITLE_LEN = 4
_BLACKLIST = {"브런치", "브런치스토리", "구독", "제안하기", "공유하기", "더보기"}


def fetch_recent_titles(author_url: str, limit: int = 15) -> list:
    """브런치 작가 페이지에서 최근 글 제목을 최대 limit개 수집한다."""
    if not author_url:
        return []

    try:
        res = requests.get(
            author_url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=10,
        )
        res.raise_for_status()
    except requests.RequestException:
        logger.warning("브런치 페이지 요청 실패: %s", author_url, exc_info=True)
        return []

    soup = BeautifulSoup(res.text, "html.parser")
    titles: list = []
    seen = set()

    def _add(text: str):
        t = (text or "").strip()
        if (
            len(t) >= _MIN_TITLE_LEN
            and t not in _BLACKLIST
            and t not in seen
        ):
            seen.add(t)
            titles.append(t)

    # 전략 1) 브런치 글 목록의 제목 요소
    for el in soup.select("strong.tit_subject, .tit_subject, strong.title"):
        _add(el.get_text(" ", strip=True))

    # 전략 2) 작가 글 링크(/@author/숫자)의 텍스트
    if len(titles) < limit:
        author_handle = re.search(r"/@([^/?#]+)", author_url)
        if author_handle:
            pat = re.compile(rf"/@{re.escape(author_handle.group(1))}/\d+")
            for a in soup.find_all("a", href=pat):
                _add(a.get_text(" ", strip=True))

    if not titles:
        logger.warning("브런치 제목을 찾지 못했습니다(마크업 변경 가능): %s", author_url)

    return titles[:limit]
