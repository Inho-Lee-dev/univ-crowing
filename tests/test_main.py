import os

import pytest

from src import main as m
from src.models import Item

REAL_LOAD_ENV = m.load_env  # autouse 픽스처가 바꾸기 전 원본


def fake_collect(fail=()):
    def collect(src, session):
        if src["name"] in fail:
            raise ConnectionError("일부러 실패")
        if src["type"] == "ustep":
            return [Item(src["name"], "program", "프로그램", "https://ustep.ulsan.ac.kr/p?1", "2026-11-11",
                         ("2026-11-03", "2026-11-18"))]
        return [Item(src["name"], "notice", f"{src['name']} 공지", f"https://ict.ulsan.ac.kr/{src['name']}",
                     "2099-01-01")]
    return collect


class FakeClient:
    instances = []

    def __init__(self, rows=(), schema=None, fail_query=False):
        self.rows, self.fail_query = list(rows), fail_query
        self.schema = schema or {"공지사항": "title", "주관부서": "select", "게시된 날짜": "date",
                                 "신청기간": "date", "운영기간": "date", "링크": "url"}
        self.writes = []
        FakeClient.instances.append(self)

    def get_schema(self):
        return self.schema

    def query_all(self):
        if self.fail_query:
            raise m.NotionError("노션 500 internal")
        return self.rows

    def create_page(self, props):
        self.writes.append(("create", props))

    def update_page(self, pid, props):
        self.writes.append(("update", pid, props))

    def archive_page(self, pid):
        self.writes.append(("archive", pid))


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    # 테스트는 실제 state/seen.json을 건드리지 않는다. 기본은 '빈 기록이 있음'.
    monkeypatch.setattr(m.state, "STATE_PATH", tmp_path / "seen.json")
    m.state.save({})
    monkeypatch.setattr(m.time, "sleep", lambda s: None)
    monkeypatch.setattr(m, "load_env", lambda: None)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_DATABASE_ID", raising=False)
    FakeClient.instances = []


def with_notion(monkeypatch, **kw):
    monkeypatch.setenv("NOTION_TOKEN", "fake")
    monkeypatch.setenv("NOTION_DATABASE_ID", "fake")
    monkeypatch.setattr(m.NotionClient, "from_env", classmethod(lambda cls: FakeClient(**kw)))


class TestErrorHandling:
    def test_one_site_fails_others_continue(self, monkeypatch, caplog):
        caplog.set_level("INFO")
        monkeypatch.setattr(m, "collect", fake_collect(fail={"ICT융합학부"}))
        assert m.main(["--dry-run"]) == 1
        assert "[ICT융합학부] 수집 실패: ConnectionError" in caplog.text
        for name in ("U-STEP", "IT융합전공", "SW중심대학사업단"):
            assert f"[{name}] 수집 1건 / 신규 1건" in caplog.text

    def test_failed_site_still_writes_others(self, monkeypatch):
        with_notion(monkeypatch)
        monkeypatch.setattr(m, "collect", fake_collect(fail={"U-STEP"}))
        assert m.main([]) == 1
        assert len([w for w in FakeClient.instances[0].writes if w[0] == "create"]) == 3

    def test_all_ok_exit_0(self, monkeypatch):
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main(["--dry-run"]) == 0

    def test_notion_query_failure_exit_1_no_writes(self, monkeypatch):
        with_notion(monkeypatch, fail_query=True)
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main([]) == 1
        assert FakeClient.instances[0].writes == []

    def test_schema_mismatch_stops(self, monkeypatch, caplog):
        with_notion(monkeypatch, schema={"공지사항": "title"})
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main([]) == 1
        assert "링크" in caplog.text and FakeClient.instances[0].writes == []


class TestOptions:
    def test_real_run_without_token_refused(self, monkeypatch):
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main([]) == 2

    def test_dry_run_never_writes(self, monkeypatch, caplog):
        caplog.set_level("INFO")
        with_notion(monkeypatch)
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main(["--dry-run"]) == 0
        assert FakeClient.instances[0].writes == []
        assert "[추가] U-STEP / 프로그램" in caplog.text

    def test_only(self, monkeypatch):
        called = []
        monkeypatch.setattr(m, "collect", lambda src, s: called.append(src["name"]) or [])
        assert m.main(["--dry-run", "--only", "IT융합전공"]) == 0
        assert called == ["IT융합전공"]

    def test_only_unknown(self, monkeypatch):
        assert m.main(["--dry-run", "--only", "없는곳"]) == 2

    def test_only_limits_deletes_to_that_site(self, monkeypatch):
        old = {"id": "old", "properties": {
            "공지사항": {"title": [{"plain_text": "옛 공지"}]}, "주관부서": {"select": {"name": "ICT융합학부"}},
            "게시된 날짜": {"date": {"start": "2020-01-01"}}, "링크": {"url": "https://ict.ulsan.ac.kr/old"}}}
        with_notion(monkeypatch, rows=[old])
        monkeypatch.setattr(m, "collect", lambda src, s: [])
        assert m.main(["--only", "IT융합전공"]) == 0
        assert FakeClient.instances[0].writes == []
        with_notion(monkeypatch, rows=[old])
        assert m.main(["--only", "ICT융합학부"]) == 0
        assert FakeClient.instances[1].writes == [("archive", "old")]

    def test_no_delete(self, monkeypatch):
        old = {"id": "old", "properties": {
            "공지사항": {"title": [{"plain_text": "옛 공지"}]}, "주관부서": {"select": {"name": "ICT융합학부"}},
            "게시된 날짜": {"date": {"start": "2020-01-01"}}, "링크": {"url": "https://ict.ulsan.ac.kr/old"}}}
        with_notion(monkeypatch, rows=[old])
        monkeypatch.setattr(m, "collect", lambda src, s: [])
        assert m.main(["--no-delete"]) == 0
        assert FakeClient.instances[0].writes == []


class TestLoadEnv:
    def test_parse(self, tmp_path, monkeypatch):
        for k in ("A", "B", "C", "EMPTY"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("D", "keep")
        f = tmp_path / ".env"
        f.write_text('# 주석\nA=1\nexport B="two"\nC = \'three\'\nD=override\nEMPTY=\nbroken\n')
        REAL_LOAD_ENV(f)
        assert (os.environ["A"], os.environ["B"], os.environ["C"], os.environ["D"]) == ("1", "two", "three", "keep")
        assert "EMPTY" not in os.environ

    def test_missing_file_ok(self, tmp_path):
        REAL_LOAD_ENV(tmp_path / "없음")

    def test_values_not_logged(self, tmp_path, monkeypatch, caplog):
        monkeypatch.delenv("SECRET_X", raising=False)
        f = tmp_path / ".env"
        f.write_text("SECRET_X=very-secret-value\n")
        caplog.set_level("DEBUG")
        REAL_LOAD_ENV(f)
        assert "very-secret-value" not in caplog.text


class TestSeenState:
    def test_saved_after_real_run(self, monkeypatch):
        with_notion(monkeypatch)
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main([]) == 0
        assert set(m.state.load()) == {"https://ustep.ulsan.ac.kr/p?1"} | {
            f"https://ict.ulsan.ac.kr/{n}" for n in ("ICT융합학부", "IT융합전공", "SW중심대학사업단")}

    def test_deleted_by_user_not_recreated(self, monkeypatch, caplog):
        caplog.set_level("INFO")
        m.state.save({"https://ustep.ulsan.ac.kr/p?1": "2026-11-11"})
        with_notion(monkeypatch)                     # 노션은 비어 있음 = 사용자가 지움
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main([]) == 0
        created = [w[1]["공지사항"]["title"][0]["text"]["content"] for w in FakeClient.instances[0].writes]
        assert "프로그램" not in created and len(created) == 3
        assert "전에 본 글 1건" in caplog.text

    def test_dry_run_does_not_save(self, monkeypatch):
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main(["--dry-run"]) == 0
        assert m.state.load() == {}

    def test_not_saved_when_notion_fails(self, monkeypatch):
        with_notion(monkeypatch, fail_query=True)
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main([]) == 1
        assert m.state.load() == {}

    def test_missing_file_records_only(self, monkeypatch):
        m.state.STATE_PATH.unlink()
        with_notion(monkeypatch)
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main([]) == 0
        assert FakeClient.instances[0].writes == []
        assert len(m.state.load()) == 4

    def test_broken_file_stops_without_writing(self, monkeypatch, caplog):
        m.state.STATE_PATH.write_text("{broken")
        with_notion(monkeypatch)
        monkeypatch.setattr(m, "collect", fake_collect())
        assert m.main([]) == 1
        assert FakeClient.instances == [] and "기록 파일 오류" in caplog.text
