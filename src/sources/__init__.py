"""수집기 등록과 공통 도구.

수집기 모듈은 두 함수를 가진다.
- fetch_raw(cfg, session): 네트워크에서 원본 응답을 받는다.
- parse(raw, cfg): 원본을 Item 목록으로 바꾼다. 네트워크 없이 테스트한다.
"""
from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import requests

from ..models import Item
from . import sw_didisam, ulsan_board, ustep

log = logging.getLogger(__name__)

TIMEOUT = 20          # 초 (PLAN.md 5-8)
PAGE_DELAY = 0.5      # 같은 사이트 연속 요청 간격
USER_AGENT = "Mozilla/5.0 (compatible; ulsan-notice-collector; +personal use)"
ALLOWED_DOMAIN = "ulsan.ac.kr"
RETRY_DELAY = 2       # 연결 오류 시 한 번 더 시도하기 전 대기 (초)

REGISTRY = {
    "ustep": ustep,
    "ulsan_board": ulsan_board,
    "sw_didisam": sw_didisam,
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def is_safe_link(url: str) -> bool:
    """https:// 이고 ulsan.ac.kr 또는 그 하위 도메인인지 확인한다 (PLAN.md 8-3)."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    if port not in (None, 443):
        return False
    return host == ALLOWED_DOMAIN or host.endswith("." + ALLOWED_DOMAIN)


def keep_safe(items: list[Item]) -> list[Item]:
    """링크 검증을 통과한 항목만 남기고, 나머지는 경고를 남긴다."""
    safe = []
    for it in items:
        if is_safe_link(it.url):
            safe.append(it)
        else:
            log.warning("[%s] 허용되지 않은 링크라 건너뜀: %s", it.source, it.url[:200])
    return safe


def collect(cfg: dict, session: requests.Session | None = None, sleep=time.sleep) -> list[Item]:
    """연결 오류(끊김·시간 초과)가 나면 2초 뒤 한 번 더 시도한다. 읽기 요청뿐이라 다시 보내도 안전하다."""
    module = REGISTRY[cfg["type"]]
    session = session or make_session()
    try:
        raw = module.fetch_raw(cfg, session)
    except (requests.ConnectionError, requests.Timeout) as e:
        log.warning("[%s] 연결 오류(%s), %d초 후 한 번 더 시도", cfg["name"], type(e).__name__, RETRY_DELAY)
        sleep(RETRY_DELAY)
        raw = module.fetch_raw(cfg, session)
    return keep_safe(module.parse(raw, cfg))
