import json
from datetime import date

import pytest

from src import state


def test_missing_is_none(tmp_path):
    assert state.load(tmp_path / "없음.json") is None


def test_roundtrip_sorted(tmp_path):
    p = tmp_path / "s.json"
    state.save({"https://b.ulsan.ac.kr/2": "2026-11-12", "https://a.ulsan.ac.kr/1": "2026-11-11"}, p)
    assert list(state.load(p)) == ["https://a.ulsan.ac.kr/1", "https://b.ulsan.ac.kr/2"]
    assert json.loads(p.read_text())["version"] == state.VERSION
    assert not (tmp_path / "s.tmp").exists()


@pytest.mark.parametrize("body", [
    "{broken",
    "[]",
    '{"version": 99, "seen": {}}',
    '{"version": 1, "seen": []}',
    '{"version": 1, "seen": {"https://evil.com/x": "2026-11-12"}}',
    '{"version": 1, "seen": {"http://ict.ulsan.ac.kr/x": "2026-11-12"}}',
    '{"version": 1, "seen": {"https://ict.ulsan.ac.kr/x": "어제"}}',
    '{"version": 1, "seen": {"https://ict.ulsan.ac.kr/x": 1}}',
])
def test_broken_raises(tmp_path, body):
    p = tmp_path / "s.json"
    p.write_text(body)
    with pytest.raises(state.StateError):
        state.load(p)


def test_merge_refreshes_and_prunes():
    today = date(2026, 11, 12)
    seen = {"https://ict.ulsan.ac.kr/old": "2026-05-15",      # 181일 전 → 지움
            "https://ict.ulsan.ac.kr/edge": "2026-05-16",     # 180일 전 → 남김
            "https://ict.ulsan.ac.kr/again": "2026-01-01"}    # 오래됐지만 이번에 다시 봄 → 갱신
    out = state.merge(seen, ["https://ict.ulsan.ac.kr/again", "https://ict.ulsan.ac.kr/new"], today)
    assert out == {"https://ict.ulsan.ac.kr/edge": "2026-05-16",
                   "https://ict.ulsan.ac.kr/again": "2026-11-12",
                   "https://ict.ulsan.ac.kr/new": "2026-11-12"}
    assert seen["https://ict.ulsan.ac.kr/again"] == "2026-01-01"   # 원본은 그대로
