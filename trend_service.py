"""코인데스크·코인니스에서 최신 크립토 뉴스 헤드라인을 수집한다.

수집한 헤드라인을 AIGenerator.extract_trending_keywords 에 넘겨
'요즘 트렌드 키워드'를 추출하는 데 쓴다. 네트워크/마크업 변경에 대비해
best-effort 로 동작하고, 실패해도 빈 리스트를 반환한다(흐름은 계속됨).
"""
import logging
import re
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
COINDESK_RSS = "https://www.coindesk.com/arc/outboundfeeds/rss/"
COINNESS_URL = "https://kr.coinness.com"
REQUEST_TIMEOUT = 10


def _fetch(url: str) -> str:
    try:
        r = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"},
            timeout=REQUEST_TIMEOUT,
        )
        r.raise_for_status()
        return r.text
    except requests.RequestException:
        logger.warning("트렌드 소스 요청 실패: %s", url, exc_info=True)
        return ""


def _coindesk_titles(limit: int) -> list:
    """코인데스크 RSS(XML)에서 기사 제목 추출."""
    xml = _fetch(COINDESK_RSS)
    if not xml:
        return []
    titles = []
    try:
        root = ElementTree.fromstring(xml)
        for item in root.iter("item"):
            t = (item.findtext("title") or "").strip()
            if t:
                titles.append(t)
    except ElementTree.ParseError:
        # XML 파싱 실패 시 정규식 폴백
        titles = [re.sub(r"<!\[CDATA\[|\]\]>", "", t).strip()
                  for t in re.findall(r"<title>(.*?)</title>", xml, re.S)]
    return titles[:limit]


def _coinness_titles(limit: int) -> list:
    """코인니스 홈페이지에서 헤드라인 추출 (best-effort)."""
    html = _fetch(COINNESS_URL)
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    seen, titles = set(), []
    for el in soup.select("h1, h2, h3, a[class*='title'], [class*='title'], [class*='headline']"):
        t = el.get_text(" ", strip=True)
        if 6 <= len(t) <= 80 and t not in seen:
            seen.add(t)
            titles.append(t)
    return titles[:limit]


def fetch_trend_headlines(limit_per_source: int = 20) -> list:
    """코인데스크 + 코인니스 최신 헤드라인을 합쳐서 반환한다."""
    coindesk = _coindesk_titles(limit_per_source)
    coinness = _coinness_titles(limit_per_source)
    logger.info("트렌드 헤드라인 수집: 코인데스크 %d개, 코인니스 %d개", len(coindesk), len(coinness))

    seen, merged = set(), []
    for h in coindesk + coinness:
        h = h.strip()
        if h and h not in seen:
            seen.add(h)
            merged.append(h)
    return merged
