"""gui.py의 Tk 비의존 헬퍼 테스트."""
import os
import time
from pathlib import Path

from webtoon2pdf.cli import _natural_key
from webtoon2pdf.gui import _display_stem, find_latest_saved_page, latest_saved_batch


def _touch(p: Path, mtime: float) -> Path:
    p.write_bytes(b"x")
    os.utime(p, (mtime, mtime))
    return p


def test_picks_newest_saved_page_across_dirs(tmp_path):
    now = time.time()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _touch(a / "old.mhtml", now - 300)
    newest = _touch(b / "new.html", now - 10)
    _touch(a / "mid.htm", now - 100)
    _touch(a / "ignored.txt", now)          # 확장자가 다르면 무시
    (a / "sub").mkdir()
    _touch(a / "sub" / "deep.mhtml", now)   # 재귀하지 않음
    assert find_latest_saved_page([a, b]) == newest


def test_none_when_empty_or_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert find_latest_saved_page([empty, tmp_path / "missing"]) is None
    assert find_latest_saved_page([]) is None


def test_batch_returns_whole_batch_in_name_order(tmp_path):
    now = time.time()
    _touch(tmp_path / "혼자 저장_20260908-1400.mhtml", now - 900)          # 배치 밖, 오래됨
    _touch(tmp_path / "B20260908-1300_001_다른배치 1화.mhtml", now - 600)   # 다른 배치
    b3 = _touch(tmp_path / "B20260908-1500_003_나루토 13화.mhtml", now - 10)  # 가장 최근
    b1 = _touch(tmp_path / "B20260908-1500_001_나루토 11화.mhtml", now - 40)
    b2 = _touch(tmp_path / "B20260908-1500_002_나루토 12화.mhtml", now - 25)
    b10 = _touch(tmp_path / "B20260908-1500_010_나루토 20화.mhtml", now - 20)  # 010이 003 뒤에 와야 함(숫자순)
    assert latest_saved_batch([tmp_path]) == [b1, b2, b3, b10]


def test_batch_falls_back_to_single_latest_file(tmp_path):
    now = time.time()
    _touch(tmp_path / "B20260908-1500_001_나루토 11화.mhtml", now - 100)
    single = _touch(tmp_path / "블로그 글_20260908-1510.mhtml", now - 5)
    assert latest_saved_batch([tmp_path]) == [single]
    assert latest_saved_batch([tmp_path / "missing"]) == []


def test_display_stem_strips_batch_prefix_and_stamp():
    assert _display_stem(Path("B20260908-1500_003_나루토 13화.mhtml")) == "나루토 13화"
    assert _display_stem(Path("나루토 13화_20260908-1510.mhtml")) == "나루토 13화"
    assert _display_stem(Path("그냥 파일.html")) == "그냥 파일"


def test_mixed_single_and_batch_files_sort_by_episode_number():
    """낱개 저장(제목_시각)과 연속 저장(B시각_번호_제목)이 섞여도 회차 순. (1~11화 낱개 + 12~20화 배치가 4권으로 쪼개진 원인)"""
    names = [
        "B20260908-1542_001_나루토 12화.mhtml", "B20260908-1542_002_나루토 13화.mhtml",
        "B20260908-1546_001_나루토 15화.mhtml", "B20260908-1546_006_나루토 20화.mhtml",
        "나루토 1화_20260908-1515.mhtml", "나루토 2화_20260908-1514.mhtml",
        "나루토 10화_20260908-1529.mhtml", "나루토 11화_20260908-1529.mhtml",
    ]
    ordered = sorted(names, key=lambda n: _natural_key(Path(n)))
    assert [_display_stem(Path(n)) for n in ordered] == [
        "나루토 1화", "나루토 2화", "나루토 10화", "나루토 11화", "나루토 12화", "나루토 13화", "나루토 15화", "나루토 20화",
    ]
    # 제목에 숫자가 없으면 배치 번호 순을 유지
    same = ["B20260908-1500_002_프롤로그.mhtml", "B20260908-1500_001_프롤로그.mhtml"]
    assert sorted(same, key=lambda n: _natural_key(Path(n))) == same[::-1]
