"""ICT융합학부 / IT융합전공 공지 게시판 (PLAN.md 4-2).

작성자 열(td.bdlUser)은 읽지 않는다 (PLAN.md 8-3).
"""
from __future__ import annotations

import logging
import re
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..models import Item, clean_title

log = logging.getLogger(__name__)

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def fetch_raw(cfg: dict, session) -> str:
    from . import TIMEOUT

    r = session.get(cfg["url"], timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def _post_no(url: str) -> int:
    try:
        return int(parse_qs(urlsplit(url).query).get("no", ["0"])[0])
    except ValueError:
        return 0


def parse(html: str, cfg: dict) -> list[Item]:
    name, base = cfg["name"], cfg["url"]
    soup = BeautifulSoup(html, "html.parser")
    by_title: dict[str, Item] = {}
    for tr in soup.select("tbody tr"):
        a = tr.select_one("td.bdlTitle a")
        d = tr.select_one("td.bdlDate")
        if not a or not a.get("href") or not d:
            continue
        title = clean_title(a.get_text(" ", strip=True))
        m = _DATE.search(d.get_text(strip=True))
        if not title or not m:
            continue
        try:
            day = date.fromisoformat(m.group()).isoformat()
        except ValueError:
            log.warning("[%s] 날짜 형식이 달라 건너뜀: %s", name, m.group())
            continue
        item = Item(source=name, kind="notice", title=title, url=urljoin(base, a["href"]), date=day)
        # 같은 공지가 글 번호만 다르게 여러 번 나온다. 제목 하나에 한 건만 남긴다.
        # 페이지 순서와 상관없이 같은 결과가 나오도록 최신 날짜, 그다음 큰 글 번호를 고른다.
        prev = by_title.get(title)
        if prev is None or (item.date, _post_no(item.url)) > (prev.date, _post_no(prev.url)):
            by_title[title] = item
    return list(by_title.values())
