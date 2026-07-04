import html
import logging
import os
import re
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
REQUEST_TIMEOUT = 10


class NaverCrawler:
    def __init__(self):
        # ⚠️ 인증 정보는 절대 소스에 하드코딩하지 않는다 (.env 로 관리)
        self.client_id = os.getenv("NAVER_CLIENT_ID", "")
        self.client_secret = os.getenv("NAVER_CLIENT_SECRET", "")

    def get_news(self, keyword, display=10):
        if not self.client_id or not self.client_secret:
            raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수가 필요합니다.")

        headers = {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
        }
        # sort="sim": 관련도(인기순) 상위 기사 수집
        params = {"query": keyword, "display": display, "sort": "sim"}

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

        news_list = []
        for idx, item in enumerate(data.get("items", []), 1):
            # HTML 태그 제거 + 엔티티(&quot; 등) 일괄 복원
            title = html.unescape(re.sub(r"<[^>]+>", "", item.get("title", "")))

            # RFC 822 형식 pubDate -> '2026-01-19 21:00'
            raw_date = item.get("pubDate", "")
            try:
                clean_date = datetime.strptime(
                    raw_date, "%a, %d %b %Y %H:%M:%S +0900"
                ).strftime("%Y-%m-%d %H:%M")
            except ValueError:
                clean_date = raw_date

            news_list.append({
                "pub_time": clean_date,        # A: 발행시간
                "keyword": keyword,            # B: 키워드
                "rank": idx,                   # C: 인기순위
                "total_count": total_results,  # D: 주제검색량 (시트가 읽는 키 이름과 통일)
                "title": title,                # E: 뉴스제목
                "link": item.get("link", ""),  # F: 링크
                # G, H(본문 상/하단)는 main.collect_news 에서 채워짐
            })
        return news_list


# 싱글톤 객체
naver_crawler = NaverCrawler()
