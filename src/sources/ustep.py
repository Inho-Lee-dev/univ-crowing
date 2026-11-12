"""U-STEP 학생 비교과 (PLAN.md 4-1)."""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlencode

from ..models import Item, clean_title, parse_period, today_kst

log = logging.getLogger(__name__)

LIST_URL = "https://ustep.ulsan.ac.kr/home/sub/prog-list"
API_URL = "https://ustep.ulsan.ac.kr/controller/homeDefault"
DETAIL_URL = "https://ustep.ulsan.ac.kr/home/sub/prog-detail"
PAGE_SIZE = 100
MAX_PAGES = 30   # 응답이 이상할 때 무한 반복을 막는다

_CSRF = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


def _sp_param(page: int) -> str:
    return json.dumps({
        "CMD": "LIST", "schType": "type1", "schText": "", "schOption": "option1",
        "schSkill": "", "schMile": "", "schInterest": "N",
        "PAGE": page, "PAGESIZE": PAGE_SIZE, "USER_NO": "", "SVR_TYPE": "", "schDept": "",
    })


def fetch_raw(cfg: dict, session) -> list[dict]:
    """진행 중(option1) 목록을 끝까지 받아 페이지별 응답 목록으로 돌려준다."""
    from . import PAGE_DELAY, TIMEOUT

    r = session.get(LIST_URL, timeout=TIMEOUT)
    r.raise_for_status()
    m = _CSRF.search(r.text)
    if not m:
        raise RuntimeError("CSRF 토큰을 찾지 못함")
    headers = {"X-CSRF-TOKEN": m.group(1), "X-Requested-With": "XMLHttpRequest", "Referer": LIST_URL}

    pages, got = [], 0
    for page in range(1, MAX_PAGES + 1):
        time.sleep(PAGE_DELAY)
        r = session.post(API_URL, headers=headers, timeout=TIMEOUT,
                         data={"_spnm": "PROG_LIST", "sp_param": _sp_param(page)})
        r.raise_for_status()
        data = r.json()
        rows = data.get("list") or []
        pages.append(data)
        got += len(rows)
        if not rows or got >= int(rows[0].get("TOT_CNT", 0)):
            return pages
    raise RuntimeError(f"{MAX_PAGES}페이지를 넘어 중단함")


def parse(pages: list[dict], cfg: dict) -> list[Item]:
    name = cfg["name"]
    today = today_kst().isoformat()
    items, seen = [], set()
    for data in pages:
        for row in data.get("list") or []:
            try:
                ma, sub = int(row["MA_IDX"]), int(row["SUB_IDX"])
                title = clean_title(str(row["PG_NM"]))
                apply = parse_period(row.get("APPLY_DATE"))
                run = parse_period(row.get("SUBJECT_DATE"))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("[%s] 형식이 다른 행을 건너뜀: %s", name, e)
                continue
            if not title or (ma, sub) in seen:
                continue
            seen.add((ma, sub))
            url = f"{DETAIL_URL}?{urlencode({'MA_IDX': ma, 'SUB_IDX': sub})}"
            items.append(Item(source=name, kind="program", title=title, url=url,
                              date=today, apply=apply, run=run))
    return items
