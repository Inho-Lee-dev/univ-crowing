"""공통 데이터 형식과 날짜 처리 함수."""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# 한국은 서머타임이 없으므로 고정 오프셋으로 충분하다.
KST = timezone(timedelta(hours=9))
TITLE_MAX = 2000  # 노션 제목 길이 제한 (PLAN.md 8-3)

Range = tuple[str | None, str | None]


@dataclass
class Item:
    source: str            # '선택' 값
    kind: str              # "program" | "notice"
    title: str
    url: str
    date: str | None       # YYYY-MM-DD
    apply: Range = (None, None)
    run: Range = (None, None)


def clean_title(title: str) -> str:
    """앞뒤 공백을 지우고 노션 제한 길이로 자른다."""
    return title.strip()[:TITLE_MAX]


def normalize_range(start: str | None, end: str | None) -> Range:
    """종료일이 없거나 시작일과 같으면 None으로 맞춘다."""
    if not start:
        return (None, None)
    if not end or end == start:
        return (start, None)
    return (start, end)


_DOT_DATE = re.compile(r"(\d{4})\.(\d{1,2})\.(\d{1,2})")


def parse_period(text: str | None) -> Range:
    """U-STEP 기간 문자열을 (시작, 종료)로 바꾼다.

    '2026.11.03(화) ~ 2026.11.18(수)' -> ('2026-11-03', '2026-11-18')
    비어 있으면 (None, None), 형식이 다르면 ValueError.
    """
    if not text or not text.strip():
        return (None, None)
    found = [date(int(y), int(m), int(d)).isoformat() for y, m, d in _DOT_DATE.findall(text)]
    if not found or len(found) > 2:
        raise ValueError(f"기간 형식을 알 수 없음: {text!r}")
    return normalize_range(found[0], found[1] if len(found) == 2 else None)


def utc_to_kst_date(text: str) -> str:
    """'2026-10-07T17:32:56Z' 같은 UTC 시각을 한국 날짜(YYYY-MM-DD)로 바꾼다."""
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"시간대 정보가 없음: {text!r}")
    return dt.astimezone(KST).date().isoformat()


def today_kst() -> date:
    return datetime.now(KST).date()


def months_ago(day: date, months: int) -> date:
    """월 단위로 뺀다. 없는 날짜는 그 달 마지막 날로 맞춘다."""
    total = day.year * 12 + (day.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))
