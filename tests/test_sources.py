import json
import re
from pathlib import Path

import pytest

from src.models import Item
import requests

from src import sources
from src.sources import collect, is_safe_link, keep_safe, sw_didisam, ulsan_board, ustep

FIX = Path(__file__).parent / "fixtures"
USTEP = {"type": "ustep", "name": "U-STEP"}
ICT = {"type": "ulsan_board", "name": "ICT융합학부", "url": "https://ict.ulsan.ac.kr/ict/5786"}
CIC = {"type": "ulsan_board", "name": "IT융합전공", "url": "https://cicweb.ulsan.ac.kr/cicweb/1024"}
SW = {"type": "sw_didisam", "name": "SW중심대학사업단", "slug": "swulsan"}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def load_json(name):
    return json.loads((FIX / name).read_text())


# ---------- 개인정보 (PLAN.md 8-3) ----------

class TestFixturePrivacy:
    @pytest.mark.parametrize("path", sorted(p for p in FIX.iterdir() if p.suffix in (".json", ".html")),
                             ids=lambda p: p.name)
    def test_no_author_fields(self, path):
        text = path.read_text()
        for field in sw_didisam.AUTHOR_FIELDS:
            assert f'"{field}"' not in text
        assert not re.search(r'<td class="bdlUser">\s*[^<\s]', text)

    @pytest.mark.parametrize("path", sorted(p for p in FIX.iterdir() if p.suffix in (".json", ".html")),
                             ids=lambda p: p.name)
    def test_no_contact_patterns(self, path):
        text = path.read_text()
        assert not re.search(r"01[016789]-?\d{3,4}-?\d{4}", text), "휴대폰 번호 형식"
        assert not re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", text), "이메일 형식"

    def test_parsed_items_do_not_carry_author(self):
        items = sw_didisam.parse(load_json("sw_didisam.json"), SW)
        raw_names = {"nick_name", "user_id"}
        assert all(not (raw_names & set(vars(it))) for it in items)


# ---------- 링크 검증 (PLAN.md 8-3) ----------

class TestSafeLink:
    @pytest.mark.parametrize("url", [
        "https://ulsan.ac.kr/x",
        "https://ict.ulsan.ac.kr/ict/5786?action=view&no=1",
        "https://sw.ulsan.ac.kr/site/swulsan/notices/1",
        "https://USTEP.ulsan.ac.kr/home",
    ])
    def test_allowed(self, url):
        assert is_safe_link(url)

    @pytest.mark.parametrize("url", [
        "http://ict.ulsan.ac.kr/ict/5786",          # https 아님
        "https://evilulsan.ac.kr/",                 # 비슷한 도메인
        "https://ulsan.ac.kr.evil.com/",            # 뒤에 붙인 도메인
        "https://user:pw@ict.ulsan.ac.kr/",         # 계정 정보
        "https://ict.ulsan.ac.kr:8443/",            # 다른 포트
        "javascript:alert(1)",
        "//ict.ulsan.ac.kr/x",
        "",
        "https://[::1/",
    ])
    def test_rejected(self, url):
        assert not is_safe_link(url)

    def test_keep_safe_drops_bad(self, caplog):
        good = Item("A", "notice", "t", "https://ict.ulsan.ac.kr/a", "2026-11-11")
        bad = Item("A", "notice", "t", "https://evil.com/a", "2026-11-11")
        assert keep_safe([good, bad]) == [good]
        assert "허용되지 않은 링크" in caplog.text


# ---------- U-STEP ----------

class TestUstep:
    def test_fixture(self):
        pages = load_json("ustep.json")
        items = ustep.parse(pages, USTEP)
        assert len(items) == int(pages[0]["list"][0]["TOT_CNT"])
        for it in items:
            assert it.kind == "program" and it.source == "U-STEP" and it.title
            assert it.url.startswith("https://ustep.ulsan.ac.kr/home/sub/prog-detail?MA_IDX=")
            assert is_safe_link(it.url)
            assert DATE.match(it.date)
            assert it.apply[0] and DATE.match(it.apply[0])
        assert len({it.url for it in items}) == len(items)

    def test_same_name_different_sub_idx_kept(self):
        row = {"PG_NM": "취업특강", "APPLY_DATE": "2026.11.03(화) ~ 2026.11.18(수)",
               "SUBJECT_DATE": "2026.11.20(금) ~ 2026.11.20(금)", "MA_IDX": 10, "TOT_CNT": 2}
        pages = [{"list": [{**row, "SUB_IDX": 1}, {**row, "SUB_IDX": 2}]}]
        items = ustep.parse(pages, USTEP)
        assert [it.url[-1] for it in items] == ["1", "2"]
        assert items[0].apply == ("2026-11-03", "2026-11-18")
        assert items[0].run == ("2026-11-20", None)

    def test_duplicate_ids_across_pages_once(self):
        row = {"PG_NM": "A", "APPLY_DATE": "2026.11.03(화)", "MA_IDX": 1, "SUB_IDX": 1, "TOT_CNT": 1}
        assert len(ustep.parse([{"list": [row]}, {"list": [row]}], USTEP)) == 1

    def test_bad_rows_skipped(self):
        rows = [
            {"PG_NM": "ok", "APPLY_DATE": "2026.11.03(화)", "MA_IDX": 1, "SUB_IDX": 1},
            {"PG_NM": "no id", "APPLY_DATE": "2026.11.03(화)"},
            {"PG_NM": "id injection", "MA_IDX": "1&x=y", "SUB_IDX": 1},
            {"PG_NM": "bad date", "APPLY_DATE": "상시", "MA_IDX": 2, "SUB_IDX": 1},
        ]
        assert [it.title for it in ustep.parse([{"list": rows}], USTEP)] == ["ok"]


# ---------- 학과 게시판 ----------

def _board_html(rows):
    trs = "".join(
        f'<tr><td class="bdlTitle"><a href="?action=view&amp;no={no}">{t}</a></td>'
        f'<td class="bdlUser">홍길동</td><td class="bdlDate">{d}</td></tr>'
        for no, t, d in rows)
    return f"<table><tbody>{trs}</tbody></table>"


class TestUlsanBoard:
    @pytest.mark.parametrize("cfg, fixture", [(ICT, "ulsan_board_0.html"), (CIC, "ulsan_board_1.html")])
    def test_fixture(self, cfg, fixture):
        items = ulsan_board.parse((FIX / fixture).read_text(), cfg)
        assert items
        titles = [it.title for it in items]
        assert len(titles) == len(set(titles))
        for it in items:
            assert it.kind == "notice" and it.source == cfg["name"]
            assert it.url.startswith(cfg["url"] + "?action=view&no=")
            assert DATE.match(it.date)

    def test_same_title_different_no_is_one(self):
        html = _board_html([(100, "같은 공지", "2026-11-11"), (200, "같은 공지", "2026-11-11"),
                            (150, "다른 공지", "2026-11-10")])
        items = ulsan_board.parse(html, ICT)
        assert len(items) == 2
        same = next(it for it in items if it.title == "같은 공지")
        assert same.url.endswith("no=200")

    def test_dedup_independent_of_order(self):
        rows = [(100, "A", "2026-11-12"), (200, "A", "2026-11-11")]
        a = ulsan_board.parse(_board_html(rows), ICT)
        b = ulsan_board.parse(_board_html(rows[::-1]), ICT)
        assert a == b and a[0].url.endswith("no=100")

    def test_author_not_read(self):
        items = ulsan_board.parse(_board_html([(1, "제목", "2026-11-11")]), ICT)
        assert "홍길동" not in repr(items)

    def test_external_href_caught_by_link_check(self):
        html = ('<table><tbody><tr><td class="bdlTitle"><a href="https://evil.com/x">t</a></td>'
                '<td class="bdlDate">2026-11-11</td></tr></tbody></table>')
        assert keep_safe(ulsan_board.parse(html, ICT)) == []


# ---------- SW중심대학사업단 ----------

class TestSwDidisam:
    def test_fixture(self):
        data = load_json("sw_didisam.json")
        items = sw_didisam.parse(data, SW)
        assert len(items) == len(data["result"]["data"])
        for it in items:
            assert it.url.startswith("https://sw.ulsan.ac.kr/site/swulsan/notices/")
            assert DATE.match(it.date)

    def test_kst_and_private_skip(self):
        data = {"result": {"data": [
            {"notice_id": 1, "title": " 공지 ", "insert_date": "2026-10-07T17:32:56Z", "is_private": False,
             "nick_name": "홍길동", "user_id": 99},
            {"notice_id": 2, "title": "비공개", "insert_date": "2026-10-07T05:00:00Z", "is_private": True},
            {"notice_id": "x", "title": "bad", "insert_date": "2026-10-07T05:00:00Z"},
        ]}}
        items = sw_didisam.parse(data, SW)
        assert items == [Item("SW중심대학사업단", "notice", "공지",
                              "https://sw.ulsan.ac.kr/site/swulsan/notices/1", "2026-10-08")]
        assert "홍길동" not in repr(items)


# ---------- 연결 오류 재시도 ----------

class FlakyModule:
    def __init__(self, errors):
        self.errors, self.calls = list(errors), 0

    def fetch_raw(self, cfg, session):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return {"result": {"data": [{"notice_id": 1, "title": "t", "insert_date": "2026-11-11T00:00:00Z"}]}}

    parse = staticmethod(sw_didisam.parse)


class TestCollectRetry:
    def run(self, monkeypatch, errors):
        mod = FlakyModule(errors)
        monkeypatch.setitem(sources.REGISTRY, "flaky", mod)
        sleeps = []
        cfg = {**SW, "type": "flaky"}
        return mod, sleeps, (lambda: collect(cfg, session=object(), sleep=sleeps.append))

    @pytest.mark.parametrize("err", [requests.ConnectionError("reset"), requests.Timeout("slow")])
    def test_retry_once_after_2s(self, monkeypatch, err, caplog):
        mod, sleeps, go = self.run(monkeypatch, [err])
        assert len(go()) == 1
        assert mod.calls == 2 and sleeps == [2]
        assert "한 번 더 시도" in caplog.text

    def test_second_failure_raises(self, monkeypatch):
        mod, sleeps, go = self.run(monkeypatch, [requests.ConnectionError("a"), requests.ConnectionError("b")])
        with pytest.raises(requests.ConnectionError):
            go()
        assert mod.calls == 2

    def test_http_error_not_retried(self, monkeypatch):
        mod, sleeps, go = self.run(monkeypatch, [requests.HTTPError("500")])
        with pytest.raises(requests.HTTPError):
            go()
        assert mod.calls == 1 and sleeps == []
