import html
import logging
import os
import re
from datetime import datetime, timedelta

import requests

logger = logging.getLogger(__name__)

NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
REQUEST_TIMEOUT = 10
FETCH_DISPLAY = 100     # 네이버 API 최대 (필터 후에도 충분한 기사 확보용)
RECENT_DAYS = 7         # 오늘 기준 이 일수 이내의 기사만 사용
PUBDATE_FMT = "%a, %d %b %Y %H:%M:%S %z"  # RFC 822 (예: Wed, 25 Jun 2026 21:00:00 +0900)


class NaverCrawler:
    def __init__(self):
        # ⚠️ 인증 정보는 절대 소스에 하드코딩하지 않는다 (.env 로 관리)
        self.client_id = os.getenv("NAVER_CLIENT_ID", "")
        self.client_secret = os.getenv("NAVER_CLIENT_SECRET", "")

    def get_news(self, keyword, recent_days=RECENT_DAYS):
        if not self.client_id or not self.client_secret:
            raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 필요합니다.")

        headers = {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
        }
        # sort="sim": 관련도(인기)순. 최신 필터는 아래에서 pubDate 로 직접 적용.
        params = {"query": keyword, "display": FETCH_DISPLAY, "sort": "sim"}

        try:
            response = requests.get(
                NAVER_NEWS_URL, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            )
        except requests.RequestException:
            logger.exception("'%s' 뉴스 API 요청 실패", keyword)
            return []

        if response.status_code != 200:
            logger.error("'%s' 뉴스 수집 실패 (에러코드: %s)", keyword, response.status_code)
            return []

        data = response.json()
        total_results = data.get("total", 0)  # 시트 D열 '주제검색량'

        # 오늘 기준 recent_days 이내 컷오프 (pubDate 가 +0900 이므로 KST 로 비교)
        cutoff = datetime.now().astimezone() - timedelta(days=recent_days)

        news_list = []
        skipped_old = 0
        rank = 0
        for item in data.get("items", []):
            raw_date = item.get("pubDate", "")
            try:
                pub_dt = datetime.strptime(raw_date, PUBDATE_FMT)
            except ValueError:
                pub_dt = None

            # 날짜 파싱 성공 + 컷오프보다 오래된 기사는 제외 (관련도 순서는 유지)
            if pub_dt is not None and pub_dt < cutoff:
                skipped_old += 1
                continue

            clean_date = pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else raw_date
            title = html.unescape(re.sub(r"<[^>]+>", "", item.get("title", "")))

            rank += 1
            news_list.append({
                "pub_time": clean_date,        # A: 발행시간
                "keyword": keyword,            # B: 키워드
                "rank": rank,                  # C: 인기순위 (7일 필터 후 관련도 순위)
                "total_count": total_results,  # D: 주제검색량 (시트가 읽는 키 이름과 통일)
                "title": title,                # E: 뉴스제목
                "link": item.get("link", ""),  # F: 링크
                # G, H(본문 상/하단)는 main.collect_news 에서 채워짐
            })

        logger.info(
            "'%s' 최근 %d일 이내 %d건 수집 (오래된 기사 %d건 제외)",
            keyword, recent_days, len(news_list), skipped_old,
        )
        return news_list


# 싱글톤 객체
naver_crawler = NaverCrawler()
