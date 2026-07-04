"""생성된 글 후처리 유틸."""
import re

END_MARKER = "<END>"
CHECKLIST_HEADER = "액션 아이템(독자별 체크리스트)"
# 체크리스트 헤더부터 "소비자 : ..." 줄까지를 글의 끝으로 간주
_CHECKLIST_END_RE = re.compile(
    r"(액션 아이템\(독자별 체크리스트\)\s*[\s\S]*?^\s*소비자\s*:\s*.*?$)",
    re.MULTILINE,
)


def safe_cut_after_end_marker(article: str) -> str:
    """모델이 END 마커 뒤에 덧붙인 군더더기를 잘라낸다.

    1순위: <END> 마커 앞까지
    2순위: '액션 아이템(독자별 체크리스트)' 섹션의 마지막 줄까지
    둘 다 없으면 원문을 그대로(공백만 정리해서) 돌려준다.
    """
    if not article:
        return ""
    if END_MARKER in article:
        return article.split(END_MARKER, 1)[0].strip()
    if CHECKLIST_HEADER not in article:
        return article.strip()
    m = _CHECKLIST_END_RE.search(article)
    if m:
        return article[: m.end()].strip()
    return article.strip()
