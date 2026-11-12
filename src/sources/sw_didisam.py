"""SW중심대학사업단 공지, didisam 플랫폼 API (PLAN.md 4-3).

응답의 작성자 필드(nick_name, user_id)에는 교직원 실명·회원번호가 들어 있다.
필요한 필드만 꺼내고 작성자 필드는 읽지 않는다 (PLAN.md 8-3).
"""
from __future__ import annotations

import logging

from ..models import Item, clean_title, utc_to_kst_date

log = logging.getLogger(__name__)

API_URL = "https://prd-community.didisam.com/api/v3/sampage/notice/community"
DETAIL_URL = "https://sw.ulsan.ac.kr/site/{slug}/notices/{id}"
PAGE_SIZE = 20

# fixture 저장 시 제거하는 필드
AUTHOR_FIELDS = ("nick_name", "user_id")


def fetch_raw(cfg: dict, session) -> dict:
    from . import TIMEOUT

    params = {"page_num": 0, "page_size": PAGE_SIZE, "is_private": "false",
              "target_object": "", "target_type": ""}
    r = session.get(API_URL, params=params, headers={"slug": cfg["slug"]}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def parse(data: dict, cfg: dict) -> list[Item]:
    name, slug = cfg["name"], cfg["slug"]
    items = []
    for row in (data.get("result") or {}).get("data") or []:
        if row.get("is_private"):
            continue
        try:
            notice_id = int(row["notice_id"])
            title = clean_title(str(row["title"]))
            day = utc_to_kst_date(str(row["insert_date"]))
        except (KeyError, TypeError, ValueError) as e:
            log.warning("[%s] 형식이 다른 행을 건너뜀: %s", name, e)
            continue
        if not title:
            continue
        items.append(Item(source=name, kind="notice", title=title,
                          url=DETAIL_URL.format(slug=slug, id=notice_id), date=day))
    return items
