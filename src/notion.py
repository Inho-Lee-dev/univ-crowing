"""노션 데이터베이스 조회·생성·갱신·보관 처리, 그리고 무엇을 할지 정하는 규칙 (PLAN.md 5-3, 5-4).

노션에 하는 쓰기는 세 가지뿐이다 (PLAN.md 8-6).
- 행 생성
- 5-3 표의 열 갱신 (링크로 매칭된 행만)
- 보관 처리 archived=true (5-4 삭제 기준을 모두 만족하는 행만)
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date

import requests

from .models import Item, Range, clean_title, months_ago, normalize_range

log = logging.getLogger(__name__)

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
TIMEOUT = 30
REQUEST_DELAY = 0.35   # 초당 약 3회 제한 (PLAN.md 5-8)
MAX_RETRIES = 3
CONN_RETRY_DELAY = 2  # 연결 오류 시 한 번 더 시도하기 전 대기 (초)

# 종류별로 갱신해도 되는 열 (PLAN.md 5-3)
UPDATABLE = {"program": ("title", "apply", "run"), "notice": ("title",)}
COLUMN_TYPES = {"title": "title", "select": "select", "date": "date",
                "apply": "date", "run": "date", "link": "url"}


class NotionError(RuntimeError):
    """상태 코드와 짧은 메시지만 담는다. 토큰·DB ID·응답 전체는 넣지 않는다."""


class TooManyDeletes(RuntimeError):
    pass


# ---------------------------------------------------------------- API 클라이언트

class NotionClient:
    def __init__(self, token: str, database_id: str, session: requests.Session | None = None,
                 sleep=time.sleep):
        if not token or not database_id:
            raise NotionError("NOTION_TOKEN, NOTION_DATABASE_ID 환경변수가 필요합니다")
        self._db = database_id
        self._sleep = sleep
        self._last = 0.0
        self._s = session or requests.Session()
        self._s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def __repr__(self) -> str:
        return "NotionClient(<숨김>)"

    @classmethod
    def from_env(cls, **kw) -> "NotionClient":
        return cls(os.environ.get("NOTION_TOKEN", ""), os.environ.get("NOTION_DATABASE_ID", ""), **kw)

    def _send(self, method: str, path: str, body: dict | None, conn_retry: bool) -> requests.Response:
        """요청 한 번. conn_retry면 연결 오류(끊김·시간 초과) 때 2초 뒤 한 번 더 보낸다.
        예외 메시지에 URL(DB ID 포함)이 들어가므로 오류에는 종류만 남긴다."""
        for last in (not conn_retry, True):
            try:
                return self._s.request(method, API + path, json=body, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout) as e:
                if last:
                    raise NotionError(f"노션 연결 실패: {type(e).__name__}") from None
                log.warning("노션 연결 오류(%s), %d초 후 한 번 더 시도", type(e).__name__, CONN_RETRY_DELAY)
                self._sleep(CONN_RETRY_DELAY)
            except requests.RequestException as e:
                raise NotionError(f"노션 연결 실패: {type(e).__name__}") from None
        raise AssertionError("unreachable")

    def _request(self, method: str, path: str, body: dict | None = None, conn_retry: bool = True) -> dict:
        """conn_retry=False: 행 생성용. 요청은 처리됐는데 응답만 끊긴 경우 다시 보내면 행이 두 번
        생길 수 있어 재시도하지 않는다. 빠진 행은 다음 실행이 채운다."""
        for attempt in range(MAX_RETRIES + 1):
            wait = REQUEST_DELAY - (time.monotonic() - self._last)
            if wait > 0:
                self._sleep(wait)
            self._last = time.monotonic()
            r = self._send(method, path, body, conn_retry)
            if r.status_code == 429:
                if attempt == MAX_RETRIES:
                    break
                try:
                    retry_after = float(r.headers.get("Retry-After", "1"))
                except ValueError:
                    retry_after = 1.0
                log.warning("노션 429, %.1f초 후 재시도 (%d/%d)", retry_after, attempt + 1, MAX_RETRIES)
                self._sleep(min(max(retry_after, 0.0), 60.0))
                continue
            if r.status_code >= 400:
                try:
                    j = r.json()
                    msg = f"{j.get('code', '')}: {str(j.get('message', ''))[:200]}"
                except ValueError:
                    msg = "응답 해석 불가"
                raise NotionError(f"노션 {r.status_code} {msg}")
            return r.json()
        raise NotionError("노션 429 재시도 횟수 초과")

    # 읽기
    def get_schema(self) -> dict[str, str]:
        """열 이름 → 타입."""
        props = self._request("GET", f"/databases/{self._db}").get("properties", {})
        return {name: p.get("type") for name, p in props.items()}

    def query_all(self) -> list[dict]:
        pages, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self._request("POST", f"/databases/{self._db}/query", body)
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")

    # 쓰기 (세 가지뿐)
    def create_page(self, properties: dict) -> None:
        self._request("POST", "/pages", {"parent": {"database_id": self._db}, "properties": properties},
                      conn_retry=False)

    def update_page(self, page_id: str, properties: dict) -> None:
        self._request("PATCH", f"/pages/{page_id}", {"properties": properties})

    def archive_page(self, page_id: str) -> None:
        self._request("PATCH", f"/pages/{page_id}", {"archived": True})


def check_schema(schema: dict[str, str], columns: dict[str, str]) -> None:
    """설정의 열이 데이터베이스에 있고 타입이 맞는지 확인한다. 열을 만들거나 고치지 않는다."""
    problems = []
    for key, name in columns.items():
        want = COLUMN_TYPES[key]
        got = schema.get(name)
        if got != want:
            problems.append(f"'{name}' 열: {want} 타입이어야 함 (현재: {got or '없음'})")
    if problems:
        raise NotionError("데이터베이스 열 확인 실패 - " + "; ".join(problems))


# ---------------------------------------------------------------- 노션 행 ↔ 값

@dataclass
class Row:
    page_id: str
    title: str
    select: str | None
    date: str | None
    apply: Range
    run: Range
    link: str | None
    created: str = ""   # 노션 created_time (중복 중 어느 행을 남길지 정할 때 씀)

    def key(self) -> tuple:
        """중복 판별용 튜플. 링크를 뺀 모든 열 값이 같으면 같은 행으로 본다."""
        return (self.title, self.select, self.date, self.apply, self.run)


def _date_range(prop: dict | None) -> Range:
    d = (prop or {}).get("date") or {}
    start = (d.get("start") or "")[:10] or None
    end = (d.get("end") or "")[:10] or None
    return normalize_range(start, end)


def parse_row(page: dict, columns: dict[str, str]) -> Row:
    props = page.get("properties", {})
    title_prop = props.get(columns["title"]) or {}
    select = (props.get(columns["select"]) or {}).get("select") or {}
    link = (props.get(columns["link"]) or {}).get("url") or None
    return Row(
        page_id=page["id"],
        title=clean_title("".join(t.get("plain_text", "") for t in title_prop.get("title", []))),
        select=select.get("name"),
        date=_date_range(props.get(columns["date"]))[0],
        apply=_date_range(props.get(columns["apply"])),
        run=_date_range(props.get(columns["run"])),
        link=link.strip() if link else None,
        created=page.get("created_time", ""),
    )


def _date_value(r: Range) -> dict:
    return {"date": {"start": r[0], "end": r[1]} if r[0] else None}


def _value(key: str, v):
    if key == "title":
        return {"title": [{"type": "text", "text": {"content": v}}]}
    return _date_value(v)


def item_properties(item: Item, columns: dict[str, str]) -> dict:
    props = {
        columns["title"]: _value("title", item.title),
        columns["select"]: {"select": {"name": item.source}},
        columns["date"]: _date_value((item.date, None)),
        columns["link"]: {"url": item.url},
    }
    if item.kind == "program":
        props[columns["apply"]] = _date_value(item.apply)
        props[columns["run"]] = _date_value(item.run)
    return props


# ---------------------------------------------------------------- 규칙

@dataclass
class Update:
    row: Row
    item: Item
    changes: dict[str, tuple]   # 'title'|'apply'|'run' → (이전, 이후)


@dataclass
class Plan:
    creates: list[Item] = field(default_factory=list)
    updates: list[Update] = field(default_factory=list)
    deletes: list[Row] = field(default_factory=list)
    dup_deletes: list[Row] = field(default_factory=list)   # 노션에 이미 있는 똑같은 행 중 지울 것
    too_old: int = 0             # 추가 기준에 걸러진 수
    existing: int = 0            # 이미 있어서 건너뛴 수
    duplicates: int = 0          # 같은 실행 안에서 겹친 수


def should_add(item: Item, add_cutoff: str, program_cutoff: str | None = None) -> bool:
    """추가 기준 (PLAN.md 5-4).
    공지: 작성일이 add_cutoff 이후. 프로그램: 신청 시작일이 program_cutoff 이후 (None이면 제한 없음)."""
    if item.kind == "program":
        start = normalize_range(*item.apply)[0]
        return program_cutoff is None or (bool(start) and start >= program_cutoff)
    return bool(item.date) and item.date >= add_cutoff


def _changes(item: Item, row: Row) -> dict[str, tuple]:
    new = {"title": clean_title(item.title),
           "apply": normalize_range(*item.apply), "run": normalize_range(*item.run)}
    old = {"title": row.title, "apply": row.apply, "run": row.run}
    return {k: (old[k], new[k]) for k in UPDATABLE[item.kind] if old[k] != new[k]}


def is_deletable(row: Row, kind: str | None, cutoff: str, today: str, apply: Range | None = None) -> bool:
    """삭제 기준 (PLAN.md 5-4). 모두 만족해야 참.

    kind는 row.select가 이번 실행 대상 출처일 때만 주어진다 (아니면 None).
    """
    if not row.link or kind is None:
        return False
    if not row.date or row.date >= cutoff:
        return False
    if kind == "program":
        start, end = apply if apply is not None else row.apply
        deadline = end or start
        if not deadline or deadline >= today:
            return False
    return True


def find_duplicate_rows(rows: list[Row], source_kinds: dict[str, str]) -> tuple[list[Row], list[Row]]:
    """노션 행 중 링크를 뺀 튜플(모든 열 값)이 같은 행을 찾는다. (남길 행, 지울 행)을 돌려준다.

    - 프로그램이 만든 행(링크 있음)이고 이번 실행 대상 출처인 행만 본다. 수동 입력 행은 건드리지 않는다.
    - 같은 튜플 묶음에서 가장 먼저 만들어진 행 하나만 남긴다 (만든 시각이 같으면 page_id 순).
    """
    groups: dict[tuple, list[Row]] = {}
    keep: list[Row] = []
    for r in rows:
        if r.link and r.select in source_kinds:
            groups.setdefault(r.key(), []).append(r)
        else:
            keep.append(r)
    drop: list[Row] = []
    for same in groups.values():
        same.sort(key=lambda r: (r.created, r.page_id))
        keep.append(same[0])
        drop.extend(same[1:])
    return keep, drop


def _notice_key(title: str, day: str | None) -> tuple[str, str | None]:
    """학과 간 중복 비교용 (공지사항, 게시된 날짜). 게시판마다 띄어쓰기가 달라 공백을 하나로 맞춘다."""
    return (" ".join(title.split()), day)


def make_plan(items: list[Item], rows: list[Row], source_kinds: dict[str, str],
              today: date, add_months: int, months: int, program_add_months: int | None = None,
              cross_dedup: list[tuple[str, str]] = ()) -> Plan:
    """수집한 항목과 노션 기존 행을 비교해 생성·갱신·삭제 목록을 만든다. 노션에 쓰지 않는다.

    add_months: 공지는 작성일이 최근 add_months개월 안일 때만 추가한다.
    months: 날짜가 months개월 지난 행은 삭제 대상이다.
    program_add_months: 프로그램은 신청 시작일이 최근 program_add_months개월 안일 때만 추가한다.
    cross_dedup: (남길 출처, 지울 출처) 목록. 두 출처에 공지사항·게시된 날짜가 같은 공지가 있으면
        남길 출처 것만 둔다. 지울 출처 공지는 새로 넣지 않고, 노션에 이미 있으면 보관 처리한다.
    """
    cutoff = months_ago(today, months).isoformat()
    # 추가 기간이 보관 기간보다 길면 넣자마자 지워지므로 둘 중 늦은 날을 쓴다.
    add_cutoff = max(months_ago(today, add_months).isoformat(), cutoff)
    program_cutoff = None if program_add_months is None else months_ago(today, program_add_months).isoformat()
    today_s = today.isoformat()
    plan = Plan()
    rows, plan.dup_deletes = find_duplicate_rows(rows, source_kinds)

    keep_of = {drop: keep for keep, drop in cross_dedup}
    # 남길 출처의 (제목, 날짜): 노션에 있는 행 + 이번 실행에 새로 넣는 행
    kept = {keep: {_notice_key(r.title, r.date) for r in rows if r.select == keep} for keep, _ in cross_dedup}

    def shadowed(source: str | None, title: str, day: str | None) -> bool:
        keep = keep_of.get(source)
        return keep is not None and _notice_key(title, day) in kept[keep]

    # 남길 출처 항목을 먼저 처리해야 이번에 새로 들어가는 행도 비교 대상이 된다.
    items = sorted(items, key=lambda it: it.source in keep_of)

    by_link = {r.link: r for r in rows if r.link}
    by_name = {(r.select, r.title) for r in rows}                       # 공지용
    manual = {(r.title, r.apply[0]) for r in rows if not r.link}        # 프로그램용
    # 링크만 다르고 나머지가 같은 프로그램(U-STEP 분반 등)은 한 행만 둔다. 지운 분반이 다시 생기지 않게 막는다.
    same_program = {(r.select, r.title, r.apply, r.run) for r in rows if r.link}
    new_apply: dict[str, Range] = {}

    seen_urls: set[str] = set()
    for item in items:
        if item.url in seen_urls:
            plan.duplicates += 1
            continue
        seen_urls.add(item.url)
        title = clean_title(item.title)

        row = by_link.get(item.url)
        if row is not None:
            plan.existing += 1
            changes = _changes(item, row)
            if changes:
                plan.updates.append(Update(row, item, changes))
                if "apply" in changes:
                    new_apply[row.page_id] = changes["apply"][1]
            continue

        if item.kind == "notice" and (item.source, title) in by_name:
            plan.existing += 1
            continue
        if item.kind == "notice" and shadowed(item.source, title, item.date):
            plan.duplicates += 1
            continue
        if item.kind == "program" and (title, normalize_range(*item.apply)[0]) in manual:
            plan.existing += 1
            continue
        program_key = (item.source, title, normalize_range(*item.apply), normalize_range(*item.run))
        if item.kind == "program" and program_key in same_program:
            plan.duplicates += 1
            continue

        if not should_add(item, add_cutoff, program_cutoff):
            plan.too_old += 1
            continue

        plan.creates.append(item)
        by_link[item.url] = Row("", title, item.source, item.date, item.apply, item.run, item.url)
        by_name.add((item.source, title))
        if item.kind == "program":
            same_program.add(program_key)
        if item.source in kept:
            kept[item.source].add(_notice_key(title, item.date))

    # 지울 출처의 기존 행 중 남길 출처에 같은 공지가 있는 행 (수동 입력 행·이번 실행 대상이 아닌 출처는 제외)
    cross = [r for r in rows if r.link and r.select in source_kinds and shadowed(r.select, r.title, r.date)]
    plan.dup_deletes += cross
    gone = {r.page_id for r in cross}
    plan.updates = [u for u in plan.updates if u.row.page_id not in gone]

    for row in rows:
        if row.page_id in gone:
            continue
        if is_deletable(row, source_kinds.get(row.select), cutoff, today_s, new_apply.get(row.page_id)):
            plan.deletes.append(row)
    return plan


# ---------------------------------------------------------------- 실행

def _fmt(key: str, v) -> str:
    if key == "title":
        return v
    start, end = v
    return f"{start or '-'}~{end}" if end else (start or "-")


LABELS = {"title": "이름", "apply": "신청기간", "run": "운영기간"}


def describe(plan: Plan) -> list[str]:
    lines = [f"[추가] {it.source} / {it.title} / {it.date}" for it in plan.creates]
    for u in plan.updates:
        diff = ", ".join(f"{LABELS[k]}: {_fmt(k, a)} → {_fmt(k, b)}" for k, (a, b) in u.changes.items())
        lines.append(f"[갱신] {u.row.select} / {u.row.title} / {diff}")
    lines += [f"[삭제] {r.select} / {r.title} / {r.date}" for r in plan.deletes]
    lines += [f"[중복 삭제] {r.select} / {r.title} / {r.date}" for r in plan.dup_deletes]
    return lines


def execute(client: NotionClient, plan: Plan, columns: dict[str, str], source_kinds: dict[str, str],
            max_delete: int, do_delete: bool = True) -> None:
    """계획을 노션에 반영한다. 삭제 대상(기간 지남 + 중복)이 max_delete를 넘으면 아무것도 쓰지 않고 오류."""
    n_delete = len(plan.deletes) + len(plan.dup_deletes)
    if do_delete and n_delete > max_delete:
        raise TooManyDeletes(f"삭제 대상 {n_delete}건이 한도 {max_delete}건을 넘어 중단함")

    for it in plan.creates:
        client.create_page(item_properties(it, columns))
        log.info("[추가] %s / %s / %s", it.source, it.title, it.date)

    for u in plan.updates:
        allowed = UPDATABLE[u.item.kind]
        if not u.row.link or not set(u.changes) <= set(allowed):
            raise RuntimeError("허용되지 않은 갱신 (버그)")
        client.update_page(u.row.page_id, {columns[k]: _value(k, new) for k, (_, new) in u.changes.items()})
        log.info(describe(Plan(updates=[u]))[0])

    if not do_delete:
        return
    for row in plan.deletes:
        # 계획 단계에서 걸렀지만 한 번 더 확인한다.
        if not row.link or row.select not in source_kinds:
            raise RuntimeError("삭제 기준에 맞지 않는 행 (버그)")
        client.archive_page(row.page_id)
        log.info("[삭제] %s / %s / %s", row.select, row.title, row.date)
    for row in plan.dup_deletes:
        if not row.link or row.select not in source_kinds:
            raise RuntimeError("중복 삭제 기준에 맞지 않는 행 (버그)")
        client.archive_page(row.page_id)
        log.info("[중복 삭제] %s / %s / %s", row.select, row.title, row.date)
