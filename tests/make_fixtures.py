"""실제 응답을 받아 작성자 정보를 지운 뒤 tests/fixtures/에 저장한다.

수동 실행용 (pytest가 수집하지 않음):
    .venv/bin/python -m tests.make_fixtures
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import yaml
from bs4 import BeautifulSoup

from src.sources import PAGE_DELAY, make_session, sw_didisam, ulsan_board, ustep

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def scrub_board(html: str) -> str:
    """게시판 표만 남기고 작성자 칸은 비운다."""
    table = BeautifulSoup(html, "html.parser").select_one("table")
    if table is None:
        raise RuntimeError("게시판 표를 찾지 못함")
    for td in table.select("td.bdlUser"):
        td.clear()
    return str(table) + "\n"


def scrub_json(obj):
    """작성자 필드를 모든 깊이에서 지운다."""
    if isinstance(obj, dict):
        return {k: scrub_json(v) for k, v in obj.items() if k not in sw_didisam.AUTHOR_FIELDS}
    if isinstance(obj, list):
        return [scrub_json(v) for v in obj]
    return obj


def dump(name: str, obj) -> None:
    (FIXTURES / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n")


def main() -> None:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    by_type = {}
    for src in cfg["sources"]:
        by_type.setdefault(src["type"], []).append(src)
    session = make_session()

    dump("ustep.json", scrub_json(ustep.fetch_raw(by_type["ustep"][0], session)))
    for i, src in enumerate(by_type["ulsan_board"]):
        time.sleep(PAGE_DELAY)
        (FIXTURES / f"ulsan_board_{i}.html").write_text(scrub_board(ulsan_board.fetch_raw(src, session)))
    time.sleep(PAGE_DELAY)
    dump("sw_didisam.json", scrub_json(sw_didisam.fetch_raw(by_type["sw_didisam"][0], session)))
    print("저장 완료:", sorted(p.name for p in FIXTURES.iterdir() if p.name != ".gitkeep"))


if __name__ == "__main__":
    main()
