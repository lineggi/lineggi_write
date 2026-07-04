"""Step 1: 키워드별 네이버 뉴스 수집."""
import logging

from crawler import naver_crawler
from naver_service import get_news_content

logger = logging.getLogger(__name__)


def collect_news(keywords, per_keyword: int = 10) -> list:
    """키워드별 상위 뉴스와 본문(상단/하단)을 수집해 리스트로 돌려준다.

    개별 키워드/기사 실패는 로그만 남기고 건너뛴다 — 한 건의 실패가
    전체 수집을 중단시키지 않는다.
    """
    all_news = []
    for kw in keywords:
        try:
            items = naver_crawler.get_news(kw)
        except Exception:
            logger.exception("naver_crawler 실패 (keyword=%s)", kw)
            continue

        if not items:
            logger.warning("검색 결과 없음 (keyword=%s)", kw)
            continue

        for rank, item in enumerate(items[:per_keyword], 1):
            try:
                top_txt, bottom_txt = get_news_content(item["link"])
            except Exception:
                logger.warning("본문 수집 실패 (link=%s)", item.get("link"))
                top_txt, bottom_txt = "", ""

            all_news.append(
                {
                    "keyword": kw,
                    "rank": rank,
                    **item,
                    "content_top": top_txt,
                    "content_bottom": bottom_txt,
                }
            )

    return all_news
