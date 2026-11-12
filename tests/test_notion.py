from datetime import date

import pytest
import requests

from src import notion
from src.models import Item
from src.notion import (NotionClient, NotionError, Plan, Row, TooManyDeletes, check_schema,
                        execute, item_properties, make_plan, parse_row)

COLS = {"title": "이름", "select": "선택", "date": "날짜", "apply": "신청기간", "run": "운영기간", "link": "링크"}
KINDS = {"U-STEP": "program", "ICT융합학부": "notice", "IT융합전공": "notice", "SW중심대학사업단": "notice"}
TODAY = date(2026, 11, 11)          # 삭제 기준일 2026-08-11, 추가 기준일 2026-10-11
ADD_MONTHS = 1
TOKEN, DB = "fake-token-for-tests", "db0123456789abcdef"


# ---------------------------------------------------------------- 가짜 노션

class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body or {}, headers or {}

    def json(self):
        if self._body is ValueError:
            raise ValueError
        return self._body


class FakeSession:
    def __init__(self, responses=None):
        self.headers = {}
        self.calls = []
        self.responses = list(responses or [])

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url.removeprefix(notion.API), json))
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return FakeResponse(200, {"object": "page"})


def client(responses=None):
    s = FakeSession(responses)
    sleeps = []
    return NotionClient(TOKEN, DB, session=s, sleep=sleeps.append), s, sleeps


def page(pid, title="", select=None, day=None, apply=(None, None), run=(None, None), link=None):
    def d(r):
        return {"type": "date", "date": {"start": r[0], "end": r[1]} if r[0] else None}
    return {"id": pid, "properties": {
        "이름": {"type": "title", "title": [{"plain_text": title}] if title else []},
        "선택": {"type": "select", "select": {"name": select} if select else None},
        "날짜": d((day, None)), "신청기간": d(apply), "운영기간": d(run),
        "링크": {"type": "url", "url": link},
    }}


def row(pid, title, select, day, apply=(None, None), run=(None, None), link=None):
    return parse_row(page(pid, title, select, day, apply, run, link), COLS)


def prog(title="프로그램", url="https://ustep.ulsan.ac.kr/p?1", apply=("2026-11-03", "2026-11-18"),
         run=("2026-11-20", None)):
    return Item("U-STEP", "program", title, url, "2026-11-11", apply, run)


def notice(title="공지", url="https://ict.ulsan.ac.kr/ict/5786?no=1", day="2026-11-11", source="ICT융합학부"):
    return Item(source, "notice", title, url, day)


def plan_of(items, rows):
    return make_plan(items, rows, KINDS, TODAY, ADD_MONTHS, 3)


# ---------------------------------------------------------------- 클라이언트

class TestClient:
    def test_headers_and_repr(self):
        c, s, _ = client()
        assert s.headers["Authorization"] == f"Bearer {TOKEN}"
        assert s.headers["Notion-Version"] == notion.NOTION_VERSION
        assert TOKEN not in repr(c) and DB not in repr(c)

    def test_missing_env(self, monkeypatch):
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
        with pytest.raises(NotionError, match="환경변수"):
            NotionClient.from_env(session=FakeSession())

    def test_query_all_paginates(self):
        c, s, _ = client([
            FakeResponse(200, {"results": [page("a")], "has_more": True, "next_cursor": "C1"}),
            FakeResponse(200, {"results": [page("b")], "has_more": False}),
        ])
        assert [p["id"] for p in c.query_all()] == ["a", "b"]
        assert s.calls[0][2] == {"page_size": 100}
        assert s.calls[1][2] == {"page_size": 100, "start_cursor": "C1"}

    def test_429_retries_with_retry_after(self):
        c, s, sleeps = client([FakeResponse(429, headers={"Retry-After": "2"}),
                               FakeResponse(429, headers={"Retry-After": "2"}),
                               FakeResponse(200, {"ok": 1})])
        assert c._request("GET", "/x") == {"ok": 1}
        assert len(s.calls) == 3
        assert sleeps.count(2.0) == 2

    def test_429_gives_up_after_3_retries(self):
        c, s, _ = client([FakeResponse(429)] * 5)
        with pytest.raises(NotionError, match="재시도"):
            c._request("GET", "/x")
        assert len(s.calls) == 4   # 처음 1회 + 재시도 3회

    def test_connection_error_retried_once_after_2s(self):
        c, s, sleeps = client([requests.ConnectionError("reset"), FakeResponse(200, {"ok": 1})])
        assert c._request("GET", "/x") == {"ok": 1}
        assert len(s.calls) == 2 and notion.CONN_RETRY_DELAY in sleeps

    def test_timeout_retried_once(self):
        c, s, _ = client([requests.Timeout("slow"), FakeResponse(200, {"ok": 1})])
        assert c._request("GET", "/x") == {"ok": 1}

    def test_connection_error_twice_fails(self):
        c, s, _ = client([requests.ConnectionError("a"), requests.ConnectionError("b"), FakeResponse(200)])
        with pytest.raises(NotionError, match="연결 실패"):
            c._request("GET", "/x")
        assert len(s.calls) == 2

    def test_create_not_retried_on_connection_error(self):
        c, s, _ = client([requests.ConnectionError("reset"), FakeResponse(200)])
        with pytest.raises(NotionError):
            c.create_page({"p": 1})
        assert len(s.calls) == 1

    def test_update_and_archive_retried(self):
        c, s, _ = client([requests.ConnectionError("a"), FakeResponse(200), requests.ConnectionError("b"),
                          FakeResponse(200)])
        c.update_page("pid", {"q": 1})
        c.archive_page("pid")
        assert len(s.calls) == 4

    def test_other_request_errors_not_retried(self):
        c, s, _ = client([requests.exceptions.InvalidURL("x"), FakeResponse(200)])
        with pytest.raises(NotionError):
            c._request("GET", "/x")
        assert len(s.calls) == 1

    def test_throttles_between_requests(self):
        c, _, sleeps = client()
        c._request("GET", "/a")
        c._request("GET", "/b")
        assert sleeps and 0 < sleeps[-1] <= notion.REQUEST_DELAY

    def test_error_hides_secrets_and_body(self):
        body = {"code": "validation_error", "message": "bad " + "x" * 500, "detail": "전체 응답"}
        c, _, _ = client([FakeResponse(400, body)])
        with pytest.raises(NotionError) as e:
            c._request("POST", f"/databases/{DB}/query", {})
        msg = str(e.value)
        assert "400" in msg and "validation_error" in msg
        assert TOKEN not in msg and DB not in msg and "전체 응답" not in msg
        assert len(msg) < 260

    def test_connection_error_hides_url(self, caplog):
        err = requests.ConnectionError(f"https://api.notion.com/v1/databases/{DB}/query")
        c, _, _ = client([err, err])
        with pytest.raises(NotionError) as e:
            c._request("POST", f"/databases/{DB}/query", {})
        assert DB not in str(e.value) and e.value.__cause__ is None
        assert DB not in caplog.text and TOKEN not in caplog.text

    def test_write_methods(self):
        c, s, _ = client()
        c.create_page({"p": 1})
        c.update_page("pid", {"q": 2})
        c.archive_page("pid")
        assert s.calls == [
            ("POST", "/pages", {"parent": {"database_id": DB}, "properties": {"p": 1}}),
            ("PATCH", "/pages/pid", {"properties": {"q": 2}}),
            ("PATCH", "/pages/pid", {"archived": True}),
        ]


class TestSchema:
    def test_ok(self):
        check_schema({"이름": "title", "선택": "select", "날짜": "date", "신청기간": "date",
                      "운영기간": "date", "링크": "url", "기타": "rich_text"}, COLS)

    def test_missing_link_column(self):
        with pytest.raises(NotionError, match="링크"):
            check_schema({"이름": "title", "선택": "select", "날짜": "date", "신청기간": "date",
                          "운영기간": "date"}, COLS)

    def test_wrong_type(self):
        with pytest.raises(NotionError, match="url"):
            check_schema({"이름": "title", "선택": "select", "날짜": "date", "신청기간": "date",
                          "운영기간": "date", "링크": "rich_text"}, COLS)


# ---------------------------------------------------------------- 변환

class TestConvert:
    def test_parse_row_normalizes(self):
        p = page("x", "  제목 ", "U-STEP", "2026-11-11T09:00:00.000+09:00",
                 ("2026-11-03", "2026-11-03"), (None, None), "https://ustep.ulsan.ac.kr/p?1")
        r = parse_row(p, COLS)
        assert (r.title, r.date, r.apply, r.run) == ("제목", "2026-11-11", ("2026-11-03", None), (None, None))

    def test_parse_row_empty_cells(self):
        r = parse_row({"id": "x", "properties": {}}, COLS)
        assert (r.title, r.select, r.date, r.link) == ("", None, None, None)

    def test_item_properties_program(self):
        p = item_properties(prog(), COLS)
        assert set(p) == set(COLS.values())
        assert p["신청기간"] == {"date": {"start": "2026-11-03", "end": "2026-11-18"}}
        assert p["운영기간"] == {"date": {"start": "2026-11-20", "end": None}}

    def test_item_properties_notice_leaves_periods_empty(self):
        p = item_properties(notice(), COLS)
        assert "신청기간" not in p and "운영기간" not in p
        assert p["선택"] == {"select": {"name": "ICT융합학부"}}


# ---------------------------------------------------------------- 중복 판별 (5-3)

class TestDuplicate:
    def test_program_same_link_exists(self):
        p = plan_of([prog()], [row("a", "프로그램", "U-STEP", "2026-11-11", ("2026-11-03", "2026-11-18"),
                                   ("2026-11-20", None), "https://ustep.ulsan.ac.kr/p?1")])
        assert not p.creates and not p.updates and p.existing == 1

    def test_program_manual_row_same_name_and_apply_start(self):
        p = plan_of([prog()], [row("m", "프로그램", "U-STEP", "2026-10-01", ("2026-11-03", None))])
        assert not p.creates and not p.updates

    def test_program_manual_row_different_apply_start_is_new(self):
        p = plan_of([prog()], [row("m", "프로그램", "U-STEP", "2026-10-01", ("2026-10-01", None))])
        assert len(p.creates) == 1

    def test_ustep_same_name_different_period_both_created(self):
        p = plan_of([prog(url="https://ustep.ulsan.ac.kr/p?1"),
                     prog(url="https://ustep.ulsan.ac.kr/p?2", run=("2026-12-01", None))], [])
        assert len(p.creates) == 2

    def test_ustep_same_everything_but_link_created_once(self):
        p = plan_of([prog(url="https://ustep.ulsan.ac.kr/p?1"), prog(url="https://ustep.ulsan.ac.kr/p?2")], [])
        assert [it.url[-1] for it in p.creates] == ["1"] and p.duplicates == 1

    def test_removed_sibling_not_recreated(self):
        # 노션에 분반 1만 남아 있으면 분반 2는 다시 만들지 않는다 (날짜는 수집일이라 달라도 됨).
        kept = row("a", "프로그램", "U-STEP", "2026-11-01", ("2026-11-03", "2026-11-18"), ("2026-11-20", None),
                   "https://ustep.ulsan.ac.kr/p?1")
        p = plan_of([prog(url="https://ustep.ulsan.ac.kr/p?1"), prog(url="https://ustep.ulsan.ac.kr/p?2")], [kept])
        assert not p.creates and not p.updates

    def test_notice_same_source_and_title_exists(self):
        p = plan_of([notice(url="https://ict.ulsan.ac.kr/ict/5786?no=2")],
                    [row("a", "공지", "ICT융합학부", "2026-11-11", link="https://ict.ulsan.ac.kr/ict/5786?no=1")])
        assert not p.creates and not p.updates

    def test_notice_same_title_other_source_is_new(self):
        p = plan_of([notice()], [row("a", "공지", "IT융합전공", "2026-11-11", link="https://cicweb.ulsan.ac.kr/x")])
        assert len(p.creates) == 1

    def test_in_run_duplicates(self):
        p = plan_of([notice(), notice(), notice(url="https://ict.ulsan.ac.kr/ict/5786?no=9")], [])
        assert len(p.creates) == 1 and p.duplicates == 1


# ---------------------------------------------------------------- 갱신 (5-3)

class TestUpdate:
    LINK = "https://ustep.ulsan.ac.kr/p?1"

    def existing(self, **kw):
        base = dict(pid="a", title="프로그램", select="U-STEP", day="2026-11-01",
                    apply=("2026-11-03", "2026-11-18"), run=("2026-11-20", None), link=self.LINK)
        base.update(kw)
        return row(base["pid"], base["title"], base["select"], base["day"], base["apply"], base["run"], base["link"])

    def test_apply_end_changed_only_apply_sent(self):
        c, s, _ = client()
        p = plan_of([prog(apply=("2026-11-03", "2026-11-25"))], [self.existing()])
        assert [list(u.changes) for u in p.updates] == [["apply"]]
        execute(c, p, COLS, KINDS, 50)
        assert s.calls == [("PATCH", "/pages/a",
                            {"properties": {"신청기간": {"date": {"start": "2026-11-03", "end": "2026-11-25"}}}})]

    def test_same_values_no_request(self):
        c, s, _ = client()
        p = plan_of([prog(title="  프로그램 ")], [self.existing()])
        assert not p.updates
        execute(c, p, COLS, KINDS, 50)
        assert s.calls == []

    def test_date_select_link_never_updated(self):
        p = plan_of([prog()], [self.existing(day="2026-01-01", select="다른값")])
        assert not p.updates

    def test_notice_title_changed_by_link(self):
        link = "https://ict.ulsan.ac.kr/ict/5786?no=1"
        p = plan_of([notice(title="공지(수정)", url=link)], [row("a", "공지", "ICT융합학부", "2026-11-11", link=link)])
        assert [u.changes for u in p.updates] == [{"title": ("공지", "공지(수정)")}]

    def test_notice_matched_by_name_not_updated(self):
        p = plan_of([notice(url="https://ict.ulsan.ac.kr/ict/5786?no=2")],
                    [row("a", "공지", "ICT융합학부", "2026-01-01", link="https://ict.ulsan.ac.kr/ict/5786?no=1")])
        assert not p.updates and not p.creates

    def test_manual_row_not_updated(self):
        p = plan_of([prog(run=("2026-12-01", None))],
                    [row("m", "프로그램", "U-STEP", "2026-11-01", ("2026-11-03", None), ("2026-11-20", None))])
        assert not p.updates

    def test_update_log_format(self, caplog):
        c, _, _ = client()
        p = plan_of([prog(apply=("2026-11-03", "2026-11-25"))], [self.existing()])
        caplog.set_level("INFO")
        execute(c, p, COLS, KINDS, 50)
        assert "[갱신] U-STEP / 프로그램 / 신청기간: 2026-11-03~2026-11-18 → 2026-11-03~2026-11-25" in caplog.text


# ---------------------------------------------------------------- 보관 정책 (5-4)

class TestRetention:
    def test_notice_older_than_one_month_not_added(self):
        p = plan_of([notice(day="2026-10-10")], [])
        assert not p.creates and p.too_old == 1

    def test_notice_within_one_month_added(self):
        assert len(plan_of([notice(day="2026-10-20")], []).creates) == 1

    def test_notice_before_cutoff_not_added(self):
        p = make_plan([notice(day="2027-01-05")], [], KINDS, date(2027, 5, 1), ADD_MONTHS, 3)
        assert not p.creates

    def test_notice_on_add_cutoff_added(self):
        assert len(plan_of([notice(day="2026-10-11")], []).creates) == 1

    def test_add_window_month_end(self):
        # 03-31에서 1개월 전은 02-28 (없는 날짜는 말일)
        p = make_plan([notice(day="2026-02-28"), notice(day="2026-02-27", url="https://ict.ulsan.ac.kr/b",
                                                         title="b")], [], KINDS, date(2026, 3, 31), 1, 3)
        assert [it.date for it in p.creates] == ["2026-02-28"]

    def test_add_window_never_longer_than_retention(self):
        # 설정 실수로 추가 기간(6개월)이 보관 기간(3개월)보다 길어도 넣자마자 지워질 글은 넣지 않는다.
        p = make_plan([notice(day="2026-07-01")], [], KINDS, TODAY, 6, 3)
        assert not p.creates

    def test_program_without_window_setting_unlimited(self):
        assert len(plan_of([prog(apply=("2026-06-01", "2026-12-01"))], []).creates) == 1

    def test_program_apply_start_within_two_months(self):
        # 2026-11-11 기준 2개월 전 = 2026-09-11
        items = [prog(url="https://ustep.ulsan.ac.kr/p?1", apply=("2026-09-11", "2026-12-01")),
                 prog(url="https://ustep.ulsan.ac.kr/p?2", apply=("2026-09-10", "2026-12-01")),
                 prog(url="https://ustep.ulsan.ac.kr/p?3", apply=("2026-11-20", None)),
                 prog(url="https://ustep.ulsan.ac.kr/p?4", apply=(None, None))]
        p = make_plan(items, [], KINDS, TODAY, ADD_MONTHS, 3, program_add_months=2)
        assert [it.url[-1] for it in p.creates] == ["1", "3"]
        assert p.too_old == 2

    def test_program_window_does_not_block_update(self):
        link = "https://ustep.ulsan.ac.kr/p?1"
        r = row("a", "p", "U-STEP", "2026-11-01", ("2026-05-01", "2026-11-18"), link=link)
        item = Item("U-STEP", "program", "p", link, "2026-11-11", ("2026-05-01", "2026-11-25"))
        p = make_plan([item], [r], KINDS, TODAY, ADD_MONTHS, 3, program_add_months=2)
        assert p.updates and not p.creates

    def test_notice_without_date_not_added(self):
        assert not plan_of([notice(day=None)], []).creates

    def test_old_bot_notice_deleted(self):
        r = row("a", "공지", "ICT융합학부", "2026-08-10", link="https://ict.ulsan.ac.kr/x")
        assert plan_of([], [r]).deletes == [r]

    def test_on_cutoff_kept(self):
        r = row("a", "공지", "ICT융합학부", "2026-08-11", link="https://ict.ulsan.ac.kr/x")
        assert not plan_of([], [r]).deletes

    def test_manual_row_never_deleted(self):
        assert not plan_of([], [row("m", "오래된 메모", "ICT융합학부", "2020-01-01")]).deletes

    def test_other_select_never_deleted(self):
        r = row("a", "공지", "개인일정", "2020-01-01", link="https://ict.ulsan.ac.kr/x")
        assert not plan_of([], [r]).deletes

    def test_row_without_date_never_deleted(self):
        assert not plan_of([], [row("a", "공지", "ICT융합학부", None, link="https://ict.ulsan.ac.kr/x")]).deletes

    def test_ustep_still_open_not_deleted(self):
        r = row("a", "p", "U-STEP", "2026-06-01", ("2026-06-01", "2026-11-11"), link="https://ustep.ulsan.ac.kr/x")
        assert not plan_of([], [r]).deletes

    def test_ustep_closed_deleted(self):
        r = row("a", "p", "U-STEP", "2026-06-01", ("2026-06-01", "2026-11-10"), link="https://ustep.ulsan.ac.kr/x")
        assert plan_of([], [r]).deletes == [r]

    def test_ustep_deadline_falls_back_to_start(self):
        r = row("a", "p", "U-STEP", "2026-06-01", ("2026-06-01", None), link="https://ustep.ulsan.ac.kr/x")
        assert plan_of([], [r]).deletes == [r]

    def test_ustep_extended_apply_recomputed(self):
        link = "https://ustep.ulsan.ac.kr/x"
        r = row("a", "p", "U-STEP", "2026-06-01", ("2026-06-01", "2026-11-01"), link=link)
        item = Item("U-STEP", "program", "p", link, "2026-11-11", ("2026-06-01", "2026-11-30"))
        p = plan_of([item], [r])
        assert p.updates and not p.deletes

    def test_only_selected_sources_considered(self):
        r = row("a", "공지", "ICT융합학부", "2026-01-01", link="https://ict.ulsan.ac.kr/x")
        assert not make_plan([], [r], {"U-STEP": "program"}, TODAY, ADD_MONTHS, 3).deletes

    def test_more_than_limit_writes_nothing(self):
        rows = [row(f"r{i}", f"공지{i}", "ICT융합학부", "2026-01-01", link=f"https://ict.ulsan.ac.kr/{i}")
                for i in range(51)]
        p = plan_of([notice()], rows)
        assert len(p.deletes) == 51
        c, s, _ = client()
        with pytest.raises(TooManyDeletes):
            execute(c, p, COLS, KINDS, 50)
        assert s.calls == []

    def test_exactly_limit_ok_and_archived(self, caplog):
        rows = [row(f"r{i}", f"공지{i}", "ICT융합학부", "2026-01-01", link=f"https://ict.ulsan.ac.kr/{i}")
                for i in range(50)]
        c, s, _ = client()
        caplog.set_level("INFO")
        execute(c, plan_of([], rows), COLS, KINDS, 50)
        assert len(s.calls) == 50 and all(b == {"archived": True} for _, _, b in s.calls)
        assert "[삭제] ICT융합학부 / 공지0 / 2026-01-01" in caplog.text

    def test_no_delete_option(self):
        rows = [row("a", "공지", "ICT융합학부", "2026-01-01", link="https://ict.ulsan.ac.kr/x")] * 60
        c, s, _ = client()
        execute(c, plan_of([], rows), COLS, KINDS, 50, do_delete=False)
        assert s.calls == []

    def test_execute_refuses_manual_row_delete(self):
        c, s, _ = client()
        bad = Plan(deletes=[row("m", "메모", "ICT융합학부", "2020-01-01")])
        with pytest.raises(RuntimeError):
            execute(c, bad, COLS, KINDS, 50)
        assert s.calls == []


# ---------------------------------------------------------------- 튜플 중복 제거

def dup_row(pid, created, title="공지", select="ICT융합학부", day="2026-11-01",
            link="https://ict.ulsan.ac.kr/ict/5786?no=1", apply=(None, None)):
    p = page(pid, title, select, day, apply, (None, None), link)
    p["created_time"] = created
    return parse_row(p, COLS)


class TestDuplicateRows:
    def test_identical_tuples_keep_oldest(self):
        rows = [dup_row("b", "2026-11-12T01:00:00.000Z"), dup_row("a", "2026-11-12T00:00:00.000Z"),
                dup_row("c", "2026-11-12T02:00:00.000Z")]
        p = plan_of([], rows)
        assert sorted(r.page_id for r in p.dup_deletes) == ["b", "c"]

    def test_same_created_time_tiebreak_by_id(self):
        rows = [dup_row("z", "2026-11-12T00:00:00.000Z"), dup_row("m", "2026-11-12T00:00:00.000Z")]
        assert [r.page_id for r in plan_of([], rows).dup_deletes] == ["z"]

    @pytest.mark.parametrize("field, value", [
        ("title", "다른 제목"), ("day", "2026-11-02"), ("select", "IT융합전공"),
        ("apply", ("2026-11-01", None)),
    ])
    def test_one_value_differs_not_duplicate(self, field, value):
        rows = [dup_row("a", "1"), dup_row("b", "2", **{field: value})]
        assert plan_of([], rows).dup_deletes == []

    def test_only_link_differs_is_duplicate(self):
        rows = [dup_row("a", "1"), dup_row("b", "2", link="https://ict.ulsan.ac.kr/ict/5786?no=2")]
        assert [r.page_id for r in plan_of([], rows).dup_deletes] == ["b"]

    def test_manual_row_and_bot_row_same_values_manual_kept(self):
        rows = [dup_row("m", "1", link=None), dup_row("b", "2")]
        assert plan_of([], rows).dup_deletes == []

    def test_manual_rows_never_touched(self):
        rows = [dup_row("a", "1", link=None), dup_row("b", "2", link=None)]
        assert plan_of([], rows).dup_deletes == []

    def test_other_source_never_touched(self):
        rows = [dup_row("a", "1", select="개인일정"), dup_row("b", "2", select="개인일정")]
        assert plan_of([], rows).dup_deletes == []

    def test_only_option_limits_to_that_source(self):
        rows = [dup_row("a", "1"), dup_row("b", "2")]
        assert make_plan([], rows, {"U-STEP": "program"}, TODAY, ADD_MONTHS, 3).dup_deletes == []

    def test_duplicate_not_also_in_deletes_or_updates(self):
        link = "https://ict.ulsan.ac.kr/ict/5786?no=1"
        rows = [dup_row("a", "1", day="2026-01-01"), dup_row("b", "2", day="2026-01-01")]
        p = plan_of([notice(title="공지(수정)", url=link)], rows)
        assert [r.page_id for r in p.dup_deletes] == ["b"]
        assert [r.page_id for r in p.deletes] == ["a"]
        assert [u.row.page_id for u in p.updates] == ["a"]
        assert not p.creates

    def test_collected_item_not_recreated(self):
        link = "https://ict.ulsan.ac.kr/ict/5786?no=1"
        p = plan_of([notice(url=link, day="2026-11-01")], [dup_row("a", "1"), dup_row("b", "2")])
        assert not p.creates and not p.updates and len(p.dup_deletes) == 1

    def test_execute_archives_duplicates(self, caplog):
        c, s, _ = client()
        caplog.set_level("INFO")
        execute(c, plan_of([], [dup_row("a", "1"), dup_row("b", "2")]), COLS, KINDS, 50)
        assert s.calls == [("PATCH", "/pages/b", {"archived": True})]
        assert "[중복 삭제] ICT융합학부 / 공지 / 2026-11-01" in caplog.text

    def test_duplicates_count_toward_delete_limit(self):
        rows = [dup_row(f"r{i}", str(i)) for i in range(52)]   # 51건 삭제 대상
        c, s, _ = client()
        with pytest.raises(TooManyDeletes):
            execute(c, plan_of([], rows), COLS, KINDS, 50)
        assert s.calls == []

    def test_no_delete_skips_duplicates(self):
        c, s, _ = client()
        execute(c, plan_of([], [dup_row("a", "1"), dup_row("b", "2")]), COLS, KINDS, 50, do_delete=False)
        assert s.calls == []

    def test_execute_refuses_manual_duplicate(self):
        c, s, _ = client()
        with pytest.raises(RuntimeError):
            execute(c, Plan(dup_deletes=[dup_row("m", "1", link=None)]), COLS, KINDS, 50)
        assert s.calls == []


# ---------------------------------------------------------------- 학과 간 중복 (ICT 우선)

CROSS = [("ICT융합학부", "IT융합전공")]
ICT_URL, IT_URL = "https://ict.ulsan.ac.kr/ict/5786?no=", "https://cicweb.ulsan.ac.kr/cicweb/1024?no="


def cross_plan(items, rows, kinds=KINDS):
    return make_plan(items, rows, kinds, TODAY, ADD_MONTHS, 3, cross_dedup=CROSS)


class TestCrossSourceDedup:
    def test_new_both_only_ict_created(self):
        items = [notice("시험 안내", IT_URL + "1", "2026-11-01", "IT융합전공"),   # IT가 먼저 와도
                 notice("시험 안내", ICT_URL + "1", "2026-11-01", "ICT융합학부")]
        p = cross_plan(items, [])
        assert [it.source for it in p.creates] == ["ICT융합학부"]
        assert p.duplicates == 1

    def test_whitespace_difference_still_same(self):
        items = [notice("시험 부정행위  관련 안내", IT_URL + "1", "2026-11-01", "IT융합전공"),
                 notice("시험 부정행위 관련 안내", ICT_URL + "1", "2026-11-01", "ICT융합학부")]
        assert [it.source for it in cross_plan(items, []).creates] == ["ICT융합학부"]

    def test_different_date_both_kept(self):
        items = [notice("모집 안내", IT_URL + "1", "2026-11-02", "IT융합전공"),
                 notice("모집 안내", ICT_URL + "1", "2026-11-01", "ICT융합학부")]
        assert len(cross_plan(items, []).creates) == 2

    def test_different_title_both_kept(self):
        items = [notice("설명회 홍보", IT_URL + "1", "2026-11-01", "IT융합전공"),
                 notice("설명회", ICT_URL + "1", "2026-11-01", "ICT융합학부")]
        assert len(cross_plan(items, []).creates) == 2

    def test_existing_it_row_archived_when_ict_row_exists(self):
        rows = [row("ict", "시험 안내", "ICT융합학부", "2026-11-01", link=ICT_URL + "1"),
                row("it", "시험 안내", "IT융합전공", "2026-11-01", link=IT_URL + "1")]
        p = cross_plan([notice("시험 안내", IT_URL + "1", "2026-11-01", "IT융합전공")], rows)
        assert [r.page_id for r in p.dup_deletes] == ["it"]
        assert not p.creates and not p.updates and not p.deletes

    def test_existing_it_row_archived_when_ict_created_now(self):
        rows = [row("it", "시험 안내", "IT융합전공", "2026-11-01", link=IT_URL + "1")]
        p = cross_plan([notice("시험 안내", ICT_URL + "1", "2026-11-01", "ICT융합학부")], rows)
        assert [it.source for it in p.creates] == ["ICT융합학부"]
        assert [r.page_id for r in p.dup_deletes] == ["it"]

    def test_it_title_becomes_same_updated_then_archived_next_run(self):
        # 노션의 IT 행 제목은 아직 달라서 이번 실행은 제목 갱신만 하고, 같아진 다음 실행에서 정리된다.
        ict = row("ict", "시험 안내", "ICT융합학부", "2026-11-01", link=ICT_URL + "1")
        item = notice("시험 안내", IT_URL + "1", "2026-11-01", "IT융합전공")
        p = cross_plan([item], [ict, row("it", "시험 안내(옛)", "IT융합전공", "2026-11-01", link=IT_URL + "1")])
        assert p.updates and not p.dup_deletes
        p2 = cross_plan([item], [ict, row("it", "시험 안내", "IT융합전공", "2026-11-01", link=IT_URL + "1")])
        assert [r.page_id for r in p2.dup_deletes] == ["it"] and not p2.updates

    def test_ict_row_never_archived(self):
        rows = [row("ict", "시험 안내", "ICT융합학부", "2026-11-01", link=ICT_URL + "1"),
                row("it", "시험 안내", "IT융합전공", "2026-11-01", link=IT_URL + "1")]
        assert "ict" not in [r.page_id for r in cross_plan([], rows).dup_deletes]

    def test_manual_it_row_not_touched(self):
        rows = [row("ict", "시험 안내", "ICT융합학부", "2026-11-01", link=ICT_URL + "1"),
                row("it", "시험 안내", "IT융합전공", "2026-11-01")]
        assert cross_plan([], rows).dup_deletes == []

    def test_only_ict_run_does_not_touch_it_rows(self):
        rows = [row("ict", "시험 안내", "ICT융합학부", "2026-11-01", link=ICT_URL + "1"),
                row("it", "시험 안내", "IT융합전공", "2026-11-01", link=IT_URL + "1")]
        assert cross_plan([], rows, {"ICT융합학부": "notice"}).dup_deletes == []

    def test_only_it_run_uses_ict_rows_in_notion(self):
        rows = [row("ict", "시험 안내", "ICT융합학부", "2026-11-01", link=ICT_URL + "1")]
        p = cross_plan([notice("시험 안내", IT_URL + "1", "2026-11-01", "IT융합전공")], rows,
                       {"IT융합전공": "notice"})
        assert not p.creates

    def test_without_setting_both_kept(self):
        items = [notice("시험 안내", IT_URL + "1", "2026-11-01", "IT융합전공"),
                 notice("시험 안내", ICT_URL + "1", "2026-11-01", "ICT융합학부")]
        assert len(plan_of(items, []).creates) == 2

    def test_archived_it_row_not_also_in_retention_deletes(self):
        rows = [row("ict", "시험 안내", "ICT융합학부", "2026-01-01", link=ICT_URL + "1"),
                row("it", "시험 안내", "IT융합전공", "2026-01-01", link=IT_URL + "1")]
        p = cross_plan([], rows)
        assert [r.page_id for r in p.dup_deletes] == ["it"]
        assert [r.page_id for r in p.deletes] == ["ict"]


# ---------------------------------------------------------------- 전에 본 글 / 사용자가 지운 행

class TestSeen:
    def test_seen_url_not_recreated(self):
        p = make_plan([notice()], [], KINDS, TODAY, ADD_MONTHS, 3, seen={notice().url})
        assert not p.creates and p.seen_before == 1

    def test_unseen_url_created(self):
        p = make_plan([notice()], [], KINDS, TODAY, ADD_MONTHS, 3, seen={"https://ict.ulsan.ac.kr/other"})
        assert len(p.creates) == 1

    def test_seen_does_not_block_update_of_existing_row(self):
        link = notice().url
        rows = [row("a", "공지", "ICT융합학부", "2026-11-11", link=link)]
        p = make_plan([notice(title="공지(수정)")], rows, KINDS, TODAY, ADD_MONTHS, 3, seen={link})
        assert [u.changes for u in p.updates] == [{"title": ("공지", "공지(수정)")}]

    def test_seen_program_not_recreated(self):
        p = make_plan([prog()], [], KINDS, TODAY, ADD_MONTHS, 3, seen={prog().url})
        assert not p.creates


class TestUserDeletedDuringRun:
    LINK = "https://ict.ulsan.ac.kr/ict/5786?no=1"

    @pytest.mark.parametrize("resp", [
        FakeResponse(404, {"code": "object_not_found", "message": "Could not find page"}),
        FakeResponse(400, {"code": "validation_error", "message": "Can't edit block that is archived."}),
    ])
    def test_update_on_deleted_row_skipped(self, resp, caplog):
        c, s, _ = client([resp])
        rows = [row("a", "공지", "ICT융합학부", "2026-11-11", link=self.LINK)]
        execute(c, plan_of([notice(title="공지(수정)", url=self.LINK)], rows), COLS, KINDS, 50)
        assert "[건너뜀]" in caplog.text and "[갱신]" not in caplog.text

    def test_archive_on_deleted_row_skipped_and_continues(self, caplog):
        c, s, _ = client([FakeResponse(404, {"code": "object_not_found", "message": "x"}), FakeResponse(200)])
        rows = [row("a", "옛1", "ICT융합학부", "2026-01-01", link="https://ict.ulsan.ac.kr/1"),
                row("b", "옛2", "ICT융합학부", "2026-01-01", link="https://ict.ulsan.ac.kr/2")]
        caplog.set_level("INFO")
        execute(c, plan_of([], rows), COLS, KINDS, 50)
        assert len(s.calls) == 2
        assert "[건너뜀] ICT융합학부 / 옛1" in caplog.text and "[삭제] ICT융합학부 / 옛2" in caplog.text

    def test_other_errors_still_raise(self):
        c, _, _ = client([FakeResponse(400, {"code": "validation_error", "message": "bad property"})])
        rows = [row("a", "공지", "ICT융합학부", "2026-11-11", link=self.LINK)]
        with pytest.raises(NotionError):
            execute(c, plan_of([notice(title="공지(수정)", url=self.LINK)], rows), COLS, KINDS, 50)

    def test_error_carries_status_and_code(self):
        c, _, _ = client([FakeResponse(404, {"code": "object_not_found", "message": "x"})])
        with pytest.raises(NotionError) as e:
            c._request("GET", "/x")
        assert (e.value.status, e.value.code) == (404, "object_not_found")


class TestScrubIds:
    @pytest.mark.parametrize("db_id", ["0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0", "0f1e2d3c4b5a69788796a5b4c3d2e1f0",
                                       "0F1E2D3C-4B5A-6978-8796-A5B4C3D2E1F0"])
    def test_ids_removed_from_error(self, db_id, caplog):
        body = {"code": "object_not_found", "message": f"Could not find database with ID: {db_id}. Make sure ..."}
        c, _, _ = client([FakeResponse(404, body)])
        with pytest.raises(NotionError) as e:
            c.get_schema()
        assert db_id.lower() not in str(e.value).lower() and "***" in str(e.value)
        assert e.value.status == 404 and e.value.code == "object_not_found"

    def test_deleted_row_still_detected_after_scrub(self):
        c, _, _ = client([FakeResponse(400, {"code": "validation_error",
                          "message": "Can't edit block that is archived. 1a2b3c4d-1a2b-1a2b-1a2b-1a2b3c4d5e6f"})])
        rows = [row("a", "공지", "ICT융합학부", "2026-11-11", link="https://ict.ulsan.ac.kr/x")]
        execute(c, plan_of([notice(title="공지(수정)", url="https://ict.ulsan.ac.kr/x")], rows), COLS, KINDS, 50)
