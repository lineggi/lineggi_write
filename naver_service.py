import logging
import os
import time
from datetime import datetime

import gspread
import requests
from bs4 import BeautifulSoup
from gspread.exceptions import APIError, SpreadsheetNotFound

logger = logging.getLogger(__name__)

# 구글 API 일시 장애(재시도로 넘길 수 있는) 상태 코드
TRANSIENT_STATUS = {429, 500, 502, 503}
SHEET_MAX_RETRIES = 3

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)
SHEET_HEADER = [
    "발행시간", "키워드", "인기순위", "주제검색량",
    "뉴스제목", "링크", "본문 상단(250자)", "본문 하단(250자)",
]


def get_news_content(url):
    """뉴스 본문을 추출해 상단(100~350자 구간), 하단 250자로 분리한다."""
    try:
        res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=5)
        soup = BeautifulSoup(res.text, "html.parser")

        # 불필요한 태그 제거 (광고, 버튼, 기자소개 등)
        for s in soup(["script", "style", "header", "footer", "nav", "button", "aside", "form"]):
            s.decompose()

        # 주요 언론사 본문 영역 타겟팅
        content = soup.select_one(
            "article, #dic_area, #articleBodyContents, #article_body, .article_body, .news_con"
        )
        text = content.get_text(" ", strip=True) if content else ""

        if not text:
            text = " ".join(
                p.get_text(strip=True)
                for p in soup.find_all("p")
                if len(p.get_text(strip=True)) > 30
            )

        # 상단 100자(리드/기자명 등) 건너뛰고 250자 추출
        top_content = text[100:350] if len(text) > 350 else text[:250]
        bottom_content = text[-250:] if len(text) > 250 else ""
        return top_content, bottom_content

    except Exception:
        logger.warning("본문 추출 실패: %s", url, exc_info=True)
        return "", ""


def save_news_to_sheet(sheet_name, all_news):
    """수집된 뉴스(all_news)를 구글 시트에 누적 저장한다.

    본문은 main.collect_news 에서 이미 수집한 top_content/bottom_content 를
    그대로 사용한다 — 여기서 get_news_content 를 다시 호출하지 않는다.

    반환: 텔레그램으로 그대로 보낼 수 있는 결과 메시지(성공/실패 이유).
    """
    if not all_news:
        return "📊 시트에 저장할 기사가 없습니다."

    current_path = os.path.dirname(os.path.abspath(__file__))
    key_file = os.path.join(current_path, "credentials.json")

    # 1) 인증 파일 존재 확인 — 가장 흔한 실패 원인
    if not os.path.exists(key_file):
        logger.error("credentials.json 없음: %s", key_file)
        return (
            "❌ 시트 저장 실패: credentials.json 파일이 프로젝트 폴더에 없습니다.\n"
            "구글 서비스 계정 키 파일을 이 폴더에 넣어주세요."
        )

    new_rows = [
        [
            item.get("pub_date", datetime.now().strftime("%Y-%m-%d %H:%M")),
            item.get("keyword"),
            item.get("relevance_rank"),
            item.get("total_count"),
            item.get("title"),
            item.get("link"),
            item.get("top_content", ""),
            item.get("bottom_content", ""),
        ]
        for item in all_news
    ]

    for attempt in range(1, SHEET_MAX_RETRIES + 1):
        try:
            client = gspread.service_account(filename=key_file)  # 최신 방식
            spreadsheet = client.open(sheet_name)
            sheet = spreadsheet.sheet1

            # 헤더 보장: 비어 있으면 추가, 첫 줄이 헤더가 아니면 맨 위에 삽입
            existing = sheet.get_all_values()
            if not existing:
                sheet.append_row(SHEET_HEADER)
            elif existing[0][: len(SHEET_HEADER)] != SHEET_HEADER:
                sheet.insert_row(SHEET_HEADER, index=1)

            sheet.append_rows(new_rows)

            # 헤더 기준 필터 자동 적용 (실패해도 저장은 유지)
            try:
                sheet.set_basic_filter()
            except Exception:
                logger.warning("기본 필터 적용 실패", exc_info=True)

            logger.info("📊 %d개 기사가 '%s'에 저장되었습니다.", len(new_rows), sheet_name)
            return f"📊 구글 시트 '{sheet_name}'에 {len(new_rows)}개 기사 저장 완료."

        except SpreadsheetNotFound:
            logger.error("시트를 찾을 수 없음: %s", sheet_name)
            return (
                f"❌ 시트 저장 실패: '{sheet_name}' 시트를 찾을 수 없습니다.\n"
                "① SHEET_NAME 이 시트 문서 이름과 정확히 같은지, "
                "② 그 시트를 서비스 계정 이메일(client_email)에 편집자로 공유했는지 확인하세요."
            )
        except APIError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            # 503 등 일시적 오류는 백오프 후 재시도
            if status in TRANSIENT_STATUS and attempt < SHEET_MAX_RETRIES:
                wait = 2 ** attempt
                logger.warning(
                    "구글 API 일시 오류(%s) — %d초 후 재시도 (%d/%d)",
                    status, wait, attempt, SHEET_MAX_RETRIES,
                )
                time.sleep(wait)
                continue
            logger.exception("구글 API 오류")
            if status in TRANSIENT_STATUS:
                return (
                    "❌ 시트 저장 실패: 구글 서비스 일시 오류(503 등)가 계속됩니다.\n"
                    "구글 서버 문제이니 잠시 후 다시 시도하면 대개 해결됩니다."
                )
            return (
                "❌ 시트 저장 실패: 구글 API 오류.\n"
                "서비스 계정에 시트 편집 권한이 있는지, Google Sheets/Drive API가 "
                f"활성화됐는지 확인하세요. ({str(e)[:120]})"
            )
        except Exception as e:
            logger.exception("시트 저장 오류")
            return f"❌ 시트 저장 실패: {type(e).__name__} — {str(e)[:150]}"

    return "❌ 시트 저장 실패: 재시도 후에도 구글 API가 응답하지 않습니다."
