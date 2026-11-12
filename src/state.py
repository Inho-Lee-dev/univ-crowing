"""이미 본 글 기록 (state/seen.json).

수집한 글의 링크를 마지막으로 본 날짜와 함께 남긴다. 기록에 있는 링크는 노션에서 지워져 있어도 다시 넣지 않는다.
사용자가 관심 없는 행을 직접 지우면 그대로 지워진 채로 둔다는 뜻이다.

GitHub Actions에서는 별도 작업이 이 파일만 저장소에 커밋한다 (.github/workflows/crawl.yml).
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from .sources import is_safe_link

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parent.parent / "state" / "seen.json"
VERSION = 1
# 추가 기간(공지 1개월, 프로그램 2개월)보다 충분히 길게 둔다. 이보다 오래 안 보인 글은 어차피 기간 밖이다.
KEEP_DAYS = 180


class StateError(RuntimeError):
    pass


def load(path: Path | None = None) -> dict[str, str] | None:
    """기록을 읽는다. 파일이 없으면 None. 형식이 이상하면 StateError (빈 기록으로 착각해 지운 글을 되살리지 않도록)."""
    path = path or STATE_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise StateError(f"기록 파일을 읽을 수 없음: {type(e).__name__}") from None
    seen = data.get("seen") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("version") != VERSION or not isinstance(seen, dict):
        raise StateError("기록 파일 형식이 다름")
    for url, day in seen.items():
        if not (isinstance(url, str) and is_safe_link(url) and isinstance(day, str)):
            raise StateError("기록 파일에 잘못된 항목이 있음")
        try:
            date.fromisoformat(day)
        except ValueError:
            raise StateError("기록 파일에 잘못된 날짜가 있음") from None
    return dict(seen)


def merge(seen: dict[str, str], urls: list[str], today: date) -> dict[str, str]:
    """이번에 본 링크의 날짜를 오늘로 바꾸고, KEEP_DAYS 넘게 안 보인 링크는 지운다."""
    out = dict(seen)
    for u in urls:
        out[u] = today.isoformat()
    limit = (today - timedelta(days=KEEP_DAYS)).isoformat()
    return {u: d for u, d in out.items() if d >= limit}


def save(seen: dict[str, str], path: Path | None = None) -> None:
    path = path or STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    body = {"version": VERSION, "seen": dict(sorted(seen.items()))}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, ensure_ascii=False, indent=1) + "\n")
    tmp.replace(path)
