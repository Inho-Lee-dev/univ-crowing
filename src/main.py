"""실행 진입점 (PLAN.md 5-7).

    python -m src.main --dry-run        노션에 쓰지 않고 추가·갱신·삭제 목록만 출력
    python -m src.main                  추가 + 갱신 + 삭제
    python -m src.main --no-delete      삭제는 건너뜀
    python -m src.main --only 사이트이름  특정 사이트만
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

from .models import Item, today_kst
from .notion import NotionClient, NotionError, TooManyDeletes, check_schema, describe, execute, make_plan, parse_row
from .sources import PAGE_DELAY, collect, make_session

log = logging.getLogger("collector")
ROOT = Path(__file__).resolve().parent.parent


def load_config(path: Path = ROOT / "config.yaml") -> dict:
    return yaml.safe_load(path.read_text())


def load_env(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE 형식의 .env를 읽어 환경변수로 넣는다. 이미 있는 값은 덮어쓰지 않는다. 값은 출력하지 않는다."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and value:
            os.environ.setdefault(key, value)


def source_kind(src: dict) -> str:
    return "program" if src["type"] == "ustep" else "notice"


def run_sources(sources: list[dict]) -> tuple[dict[str, list[Item]], list[str]]:
    """사이트별로 수집한다. 한 사이트가 실패해도 나머지는 계속 (PLAN.md 5-8)."""
    session = make_session()
    collected, failed = {}, []
    for i, src in enumerate(sources):
        if i:
            time.sleep(PAGE_DELAY)
        try:
            collected[src["name"]] = collect(src, session)
        except Exception as e:
            failed.append(src["name"])
            log.error("[%s] 수집 실패: %s: %s", src["name"], type(e).__name__, str(e)[:200])
    return collected, failed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="울산대 공지·비교과 → 노션 수집기")
    p.add_argument("--dry-run", action="store_true", help="노션에 쓰지 않고 추가·갱신·삭제 목록만 출력")
    p.add_argument("--no-delete", action="store_true", help="추가·갱신만 하고 삭제는 건너뜀")
    p.add_argument("--only", metavar="사이트이름", help="특정 사이트만 실행 (디버깅용)")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    cfg = load_config()
    columns, ret = cfg["notion"]["columns"], cfg["retention"]
    sources = cfg["sources"]
    if args.only:
        sources = [s for s in sources if s["name"] == args.only]
        if not sources:
            log.error("설정에 없는 사이트: %s", args.only)
            return 2
    kinds = {s["name"]: source_kind(s) for s in sources}

    load_env()
    have_notion = bool(os.environ.get("NOTION_TOKEN") and os.environ.get("NOTION_DATABASE_ID"))
    if not have_notion and not args.dry_run:
        log.error("NOTION_TOKEN, NOTION_DATABASE_ID가 없습니다. .env 또는 환경변수를 확인하세요.")
        return 2

    collected, failed = run_sources(sources)
    items = [it for name in collected for it in collected[name]]

    rows = []
    client = None
    if have_notion:
        try:
            client = NotionClient.from_env()
            check_schema(client.get_schema(), columns)
            rows = [parse_row(pg, columns) for pg in client.query_all()]
            log.info("노션 기존 행 %d건", len(rows))
        except NotionError as e:
            log.error("노션 조회 실패: %s", e)
            return 1
    else:
        log.warning("노션 정보가 없어 기존 행 없이 계산합니다 (dry-run 전용). 갱신·삭제는 표시되지 않습니다.")

    plan = make_plan(items, rows, kinds, today_kst(), int(ret["add_months"]), int(ret["months"]),
                     int(ret["program_add_months"]),
                     [(d["keep"], d["drop"]) for d in cfg.get("cross_source_dedup") or []])

    new = Counter(it.source for it in plan.creates)
    upd = Counter(u.item.source for u in plan.updates)
    for name in collected:
        log.info("[%s] 수집 %d건 / 신규 %d건 / 갱신 %d건", name, len(collected[name]), new[name], upd[name])

    do_delete = not args.no_delete
    max_delete = int(ret["max_delete_per_run"])
    if args.dry_run:
        for line in describe(plan):
            if do_delete or not line.startswith(("[삭제]", "[중복 삭제]")):
                log.info(line)
        n_delete = len(plan.deletes) + len(plan.dup_deletes)
        if do_delete and n_delete > max_delete:
            log.error("삭제 대상 %d건이 한도 %d건을 넘습니다. 실제 실행에서는 중단됩니다.", n_delete, max_delete)
            failed.append("삭제 한도")
    else:
        try:
            execute(client, plan, columns, kinds, max_delete, do_delete)
        except (NotionError, TooManyDeletes) as e:
            log.error("노션 반영 실패: %s", e)
            return 1

    deleted = len(plan.deletes) if do_delete else 0
    dup_deleted = len(plan.dup_deletes) if do_delete else 0
    log.info("%s추가 %d건 / 갱신 %d건 / 삭제 %d건 / 중복 삭제 %d건 / 이미 있음 %d건 / 기간 밖 %d건 / 중복 %d건",
             "[dry-run] " if args.dry_run else "", len(plan.creates), len(plan.updates), deleted, dup_deleted,
             plan.existing, plan.too_old, plan.duplicates)
    if failed:
        log.error("실패: %s", ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
