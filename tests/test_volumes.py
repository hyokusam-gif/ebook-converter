import zipfile
import zlib
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont

from webtoon2pdf import volumes as vol
from webtoon2pdf.cli import run
from webtoon2pdf.models import JobConfig


def _page(tmp_path, name: str, title: str, first_color, n_pages: int, site_img=None, seed: str | None = None) -> Path:
    files = tmp_path / f"{name}_files"
    files.mkdir(exist_ok=True)
    tags = []
    if site_img is not None:  # 사이트 공통 이미지(본문과 같은 폭이라 본문 구간에 섞여 들어온다)
        Image.new("RGB", (600, 400), site_img).save(files / "site.png", "PNG")
        tags.append(f'<img src="./{name}_files/site.png" style="width:600px;display:block">')
    Image.new("RGB", (600, 800), first_color).save(files / "cover.jpg", "JPEG", quality=90)
    tags.append(f'<img src="./{name}_files/cover.jpg" style="width:600px;display:block">')
    for i in range(n_pages):
        key = zlib.crc32((seed or name).encode()) + i * 7919  # 회차·장마다 고유한 색 (실제 본문은 회차 간 겹치지 않음)
        Image.new("RGB", (600, 800), (key % 200 + 20, (key // 200) % 200 + 20, (key // 40000) % 200 + 20)).save(files / f"{i}.jpg", "JPEG")
        tags.append(f'<img src="./{name}_files/{i}.jpg" style="width:600px;display:block">')
    p = tmp_path / f"{name}.html"
    p.write_text(f"<html><head><meta charset='utf-8'><title>{title}</title></head><body>{''.join(tags)}</body></html>",
                 encoding="utf-8")
    return p


def _names(result: Path) -> list[str]:
    return sorted(p.name for p in result.glob("*.epub"))


def _nav_titles(epub: Path) -> list[str]:
    import re
    with zipfile.ZipFile(epub) as z:
        return re.findall(r"<li><a[^>]*>(.*?)</a></li>", z.read("OEBPS/nav.xhtml").decode("utf-8"))


def _fake_ocr(p: Path):
    """테스트용 OCR: 빨간 표지 → 1권, 그 밖의 표지 → 읽지 못함."""
    with Image.open(p) as im:
        r, g, b = im.convert("RGB").getpixel((5, 5))
    return 1 if (r > 200 and g < 80 and b < 80) else None


@pytest.fixture(autouse=True)
def no_ocr(monkeypatch):
    """묶기 테스트는 실제 OCR 대신 색 기반 스텁으로 결정적으로. (OCR 자체는 test_ocr_reads_volume에서 따로)"""
    monkeypatch.setattr(vol, "ocr_volume_number", _fake_ocr)


def test_first_volume_number_is_used_when_cover_has_no_text(tmp_path):
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화 - 어떤사이트", "red" if n <= 3 else "blue", 2) for n in range(1, 6)]
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True, first_volume=12), log=logs.append)
    # 빨간 표지는 OCR로 1권을 읽고, 파란 표지는 못 읽어 앞 권+1 → 2권 (첫 권 번호 12는 표지를 읽었으므로 무시)
    assert _names(result) == ["나루토 1권 (1화~3화).epub", "나루토 2권 (4화~5화).epub"]
    assert _nav_titles(result / "나루토 1권 (1화~3화).epub") == ["나루토 1화", "나루토 2화", "나루토 3화"]  # 공통 꼬리표 제거
    assert any("표지에서 권 번호 읽음: 1권" in m for m in logs)
    assert any("꼬리표 제거" in m for m in logs)


def test_site_image_without_covers_falls_back_to_count(tmp_path):
    # 모든 회차가 같은 사이트 이미지로 시작하고 권 표지는 없다(첫 장이 회차마다 다름)
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", (n * 9 % 200 + 20, 50, 50), 2, site_img="gray") for n in range(100, 110)]
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True, group=4, first_volume=12), log=logs.append)
    assert any("표지 구조를 확인하지 못해" in m and "4개씩" in m for m in logs)
    assert any("앞쪽 반복 이미지" in m for m in logs)  # 사이트 공통 이미지(회색)가 걷혀야 한다
    assert _names(result) == ["나루토 12권 (100화~103화).epub", "나루토 13권 (104화~107화).epub", "나루토 14권 (108화~109화).epub"]
    with zipfile.ZipFile(result / "나루토 14권 (108화~109화).epub") as z:
        assert z.read("OEBPS/content.opf").decode("utf-8").count("<itemref") == 2 * 3  # 사이트 이미지 제거, 표지+2장 × 2회차


def test_duplicate_chapter_removed_and_incomplete_warned(tmp_path):
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "red", 6) for n in range(1, 5)]
    paths.append(_page(tmp_path, "ep3b", "나루토 3화", "red", 6, seed="ep3"))   # 3화 두 번 저장(내용 동일)
    paths.append(_page(tmp_path, "ep5", "나루토 5화", "red", 1))                # 저장 덜 됨(표지+1장)
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True), log=logs.append)
    assert any("중복 저장된 회차 제외: 나루토 3화" in m for m in logs)
    assert any("저장이 덜 된 회차 제외: 나루토 5화 (2장뿐)" in m for m in logs)
    assert _names(result) == ["나루토 1권 (1화~4화).epub"]  # 5화는 책에서 빠짐
    assert _nav_titles(result / "나루토 1권 (1화~4화).epub") == ["나루토 1화", "나루토 2화", "나루토 3화", "나루토 4화"]


def test_fake_site_notice_pages_do_not_pass_as_covers(tmp_path):
    """마나토끼처럼 안내 페이지(글자 없음)가 여러 회차 첫 장에 반복되는 경우: 표지로 믿지 않고 개수로 나눈다."""
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "gray" if n < 105 else "silver", 2) for n in range(100, 112)]
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True, group=9, first_volume=12), log=logs.append)
    # 회색/은색 '안내 페이지'는 세로형이라 표지로 인식되고, OCR은 못 읽으니 첫 권 번호 12부터 차례로 붙는다
    assert _names(result) == ["나루토 12권 (100화~104화).epub", "나루토 13권 (105화~111화).epub"]
    with zipfile.ZipFile(result / "나루토 13권 (105화~111화).epub") as z:
        assert z.read("OEBPS/content.opf").decode("utf-8").count("<itemref") == 3 + 2 * 6  # 표지는 첫 회차에만


def _page_with_ads(tmp_path, name: str, title: str, cover_color, n_pages: int) -> Path:
    """마나토끼형: 사이트 로고(정사각 300) → 광고(정사각 500) → 권 표지(세로형, 권마다 같음) → 본문."""
    files = tmp_path / f"{name}_files"
    files.mkdir(exist_ok=True)
    Image.new("RGB", (300, 300), "orange").save(files / "logo.png", "PNG")
    Image.new("RGB", (500, 500), "purple").save(files / "ad.png", "PNG")
    Image.new("RGB", (720, 1041), cover_color).save(files / "cover.jpg", "JPEG", quality=90)
    tags = [f'<img src="./{name}_files/logo.png" style="width:300px;display:block">',
            f'<img src="./{name}_files/ad.png" style="width:500px;display:block">',
            f'<img src="./{name}_files/cover.jpg" style="width:720px;display:block">']
    for i in range(n_pages):
        key = zlib.crc32(name.encode()) + i * 7919
        Image.new("RGB", (720, 1041), (key % 200 + 20, (key // 200) % 200 + 20, (key // 40000) % 200 + 20)).save(files / f"{i}.jpg", "JPEG")
        tags.append(f'<img src="./{name}_files/{i}.jpg" style="width:720px;display:block">')
    p = tmp_path / f"{name}.html"
    p.write_text(f"<html><head><meta charset='utf-8'><title>{title}</title></head><body>{''.join(tags)}</body></html>",
                 encoding="utf-8")
    return p


def test_cover_after_site_logo_and_ad_is_found(tmp_path):
    paths = [_page_with_ads(tmp_path, f"ep{n}", f"나루토 {n}화", "red" if n < 200 else "blue", 3) for n in range(191, 205)]
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True, first_volume=22), log=logs.append)
    # 빨간 표지 → OCR 1권(스텁), 파란 표지 → 못 읽어 앞 권+1 = 2권
    assert _names(result) == ["나루토 1권 (191화~199화).epub", "나루토 2권 (200화~204화).epub"]
    assert any("표지 이미지 기준으로 2권으로 나눔" in m for m in logs)
    with zipfile.ZipFile(result / "나루토 1권 (191화~199화).epub") as z:
        # 로고·광고는 전부 제거, 표지는 191화에만: (1+3) + 3×8
        assert z.read("OEBPS/content.opf").decode("utf-8").count("<itemref") == 4 + 3 * 8


def test_singleton_cover_group_is_merged_not_fallback(tmp_path):
    """한 회차만 다른 표지(변형)를 가져도 표지 묶기를 버리지 않고 앞 묶음에 합친다."""
    colors = ["red"] * 4 + ["gray"] + ["blue"] * 4       # 5번째만 홀로 다른 표지
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", colors[n - 1], 2) for n in range(1, 10)]
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True), log=logs.append)
    assert _names(result) == ["나루토 1권 (1화~5화).epub", "나루토 2권 (6화~9화).epub"]
    assert not any("개씩 묶습니다" in m for m in logs)
    with zipfile.ZipFile(result / "나루토 1권 (1화~5화).epub") as z:  # 1권 첫 장은 빨간 표지 그대로
        first = sorted(n for n in z.namelist() if n.startswith("OEBPS/images/"))[0]
        import io
        assert Image.open(io.BytesIO(z.read(first))).convert("RGB").getpixel((5, 5))[0] > 200


def test_count_fallback_keeps_first_cover(tmp_path):
    """개수 묶기로 떨어져도 표지(페이지 크기 반복 이미지)의 첫 등장은 남긴다."""
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "gray", 2) for n in range(1, 16)]  # 15회차 모두 같은 회색 표지
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True, group=5), log=lambda s: None)
    names = _names(result)
    assert names == ["나루토 1권 (1화~5화).epub", "나루토 2권 (6화~10화).epub", "나루토 3권 (11화~15화).epub"]
    with zipfile.ZipFile(result / names[0]) as z:
        assert z.read("OEBPS/content.opf").decode("utf-8").count("<itemref") == 1 + 5 * 2   # 표지 1장 + 본문
    with zipfile.ZipFile(result / names[1]) as z:
        assert z.read("OEBPS/content.opf").decode("utf-8").count("<itemref") == 5 * 2       # 반복 표지 없음


def test_same_volume_number_groups_are_merged(tmp_path, monkeypatch):
    """한 권이 표지 두 개로 갈려도(앞쪽은 OCR 실패→앞권+1, 뒤쪽은 OCR로 같은 번호) 한 권으로 합친다.
    23권(빨강 표지, OCR 23) → 24권 앞부분(초록, OCR 실패→24) + 24권 뒷부분(파랑, OCR 24) → 24권 하나."""
    def ocr(p):
        with Image.open(p) as im:
            r, g, b = im.convert("RGB").getpixel((5, 5))
        if r > 200 and g < 80 and b < 80:
            return 23
        if b > 200 and r < 80 and g < 80:
            return 24
        return None  # 초록 표지는 못 읽음
    monkeypatch.setattr(vol, "ocr_volume_number", ocr)
    paths = []
    paths += [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "red", 2) for n in (200, 201, 202)]     # 23권
    paths += [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "green", 2) for n in (203, 204)]         # 24권 앞(OCR 실패)
    paths += [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "blue", 2) for n in (205, 206, 207)]     # 24권 뒤(OCR 24)
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True), log=logs.append)
    assert _names(result) == ["나루토 23권 (200화~202화).epub", "나루토 24권 (203화~207화).epub"]
    assert any("갈린 묶음을 합쳤습니다" in m for m in logs)


def test_same_cover_split_by_other_source_is_coalesced(tmp_path, monkeypatch):
    """한 권 중간에 다른 소스(일일툰)로 대체한 회차가 끼어 표지가 갈려도, 앞뒤 표지가 같으면 한 권으로 합친다.
    24권: 209·210(제24권 표지) + 211~213(끼어든 다른 그림) + 214~217(다시 제24권 표지) → 24권 하나."""
    def ocr(p):
        with Image.open(p) as im:
            r, g, b = im.convert("RGB").getpixel((5, 5))
        return 24 if (r > 200 and g < 80 and b < 80) else None  # 빨강 = 제24권 표지
    monkeypatch.setattr(vol, "ocr_volume_number", ocr)
    paths = []
    paths += [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "red", 2) for n in (209, 210)]          # 제24권 표지
    paths += [_page(tmp_path, f"ep{n}", f"나루토 {n}화", ("cyan" if n == 211 else "magenta"), 2, seed=f"ins{n}")
              for n in (211, 212, 213)]                                                          # 끼어든 다른 그림
    paths += [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "red", 2) for n in (214, 215, 216, 217)]  # 다시 제24권 표지
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True, first_volume=24), log=logs.append)
    assert _names(result) == ["나루토 24권 (209화~217화).epub"]


def test_gap_in_episode_numbers_is_warned(tmp_path):
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "red", 2) for n in (86, 87, 88, 91, 92)]
    logs: list[str] = []
    run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                  format="epub", group_by_cover=True), log=logs.append)
    assert any("빠진 회차: 89화, 90화" in m for m in logs)


def test_ocr_text_normalization():
    assert vol._volume_in_text(vol._normalize_ocr("CHAMP COMCS 碍1()췋 -4루트-")) == 10
    assert vol._volume_in_text(vol._normalize_ocr("碍2())d 한국어판")) == 20
    assert vol._volume_in_text(vol._normalize_ocr("CHAW COMICS 碍21)d")) == 21
    assert vol._volume_in_text(vol._normalize_ocr("碍23)! -4루트-")) == 23
    assert vol._volume_in_text(vol._normalize_ocr("CHAMP COMICS 碍1권 -4루트-")) == 1
    assert vol._volume_in_text(vol._normalize_ocr("째1())»el")) == 10
    assert vol._volume_in_text("Masashi Kishimoto") is None


def test_inconsistent_ocr_value_is_discarded():
    assert vol.discard_inconsistent([10, 11, 1, 13]) == [10, 11, None, 13]
    assert vol.discard_inconsistent([22, None, 24]) == [22, None, 24]          # 2개면 판단 안 함
    assert vol.fill_volume_numbers([10, 11, 1, 13], 1) == [10, 11, 12, 13]


def test_precheck_asks_for_replacements_and_continues(tmp_path):
    """점검 단계: 저장이 덜 된 회차를 알리고, 다시 저장한 파일을 받아 회차로 넣은 뒤 책을 만든다."""
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "red", 6) for n in (1, 2, 3, 5)]
    paths.append(_page(tmp_path, "ep4bad", "나루토 4화", "red", 0))       # 4화 저장 덜 됨(표지만)
    redo = tmp_path / "redo"; redo.mkdir()
    good4 = _page(redo, "ep4", "나루토 4화", "red", 6)                      # 다시 저장한 4화
    asked: list = []

    def ask(bad, missing):
        asked.append(([t for t, _ in bad], missing))
        return [str(good4)]

    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True), log=logs.append, ask_replacements=ask)
    assert asked == [(["나루토 4화 (1장뿐)"], [])]
    assert _names(result) == ["나루토 1권 (1화~5화).epub"]
    assert _nav_titles(result / "나루토 1권 (1화~5화).epub") == [f"나루토 {n}화" for n in (1, 2, 3, 4, 5)]
    assert any("다시 저장한 파일·주소 1개를 추가" in m for m in logs)


def test_precheck_accepts_url_replacement(tmp_path, site):
    """점검 창에서 파일 대신 주소를 넣으면 그 페이지를 직접 받아(캔버스 캡처 포함) 회차로 끼워 넣는다."""
    paths = [_page(tmp_path, f"ep{n}", f"테스트웹툰 {n}화", "red", 6) for n in (1, 3)]
    paths.append(_page(tmp_path, "ep2bad", "테스트웹툰 2화", "red", 0))       # 2화 저장 덜 됨
    # 대체: 로컬 fixture 서버의 ep2.html (제목 '테스트웹툰 2화', 본문 4장)
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=False, delay=0, scroll_wait=0.1),
                 log=lambda s: None, ask_replacements=lambda bad, missing: [f"{site}/ep2.html"])
    assert _nav_titles(result) == ["테스트웹툰 1화", "테스트웹툰 2화", "테스트웹툰 3화"]  # 회차 번호 순으로 끼워 넣음


def test_replacement_title_trimmed_and_sorted_by_episode(tmp_path, site):
    """다른 사이트 주소로 155화 대체 시: 제목 꼬리표를 잘라내고 회차 번호(155) 자리에 넣는다."""
    from webtoon2pdf.cli import _trim_after_episode
    assert _trim_after_episode("나루토 155화 - 일일툰 일본만화 무료") == "나루토 155화"
    assert _trim_after_episode("오프닝") == "오프닝"
    # 로컬 fixture: ep2.html 제목이 '테스트웹툰 2화'
    paths = [_page(tmp_path, f"ep{n}", f"테스트웹툰 {n}화", "red", 6) for n in (1, 3)]
    paths.append(_page(tmp_path, "ep2bad", "테스트웹툰 2화", "red", 0))
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=False, delay=0, scroll_wait=0.1),
                 log=lambda s: None, ask_replacements=lambda bad, missing: [f"{site}/ep2.html"])
    assert _nav_titles(result) == ["테스트웹툰 1화", "테스트웹툰 2화", "테스트웹툰 3화"]


def test_precheck_cancel_raises(tmp_path):
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "red", 6) for n in (1, 2, 3)]
    paths.append(_page(tmp_path, "ep4bad", "나루토 4화", "red", 0))
    with pytest.raises(RuntimeError, match="취소"):
        run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                      format="epub"), log=lambda s: None, ask_replacements=lambda bad, missing: None)


def test_parse_range():
    from webtoon2pdf.cli import parse_range
    assert parse_range("1-10") == (1, 10)
    assert parse_range("11~20") == (11, 20)
    assert parse_range(" 11 - 20 ") == (11, 20)
    assert parse_range("7") == (7, 7)
    for bad in ("a-b", "10-1", "", "0-3"):
        with pytest.raises(RuntimeError):
            parse_range(bad)


def test_volume_range_builds_only_selected_volumes(tmp_path, monkeypatch):
    colors = {1: "red", 2: "blue", 3: "green", 4: "yellow", 5: "purple"}
    def ocr(p):
        with Image.open(p) as im:
            px = im.convert("RGB").getpixel((5, 5))
        return {(255, 0, 0): 1, (0, 0, 255): 2, (0, 128, 0): 3, (255, 255, 0): 4, (128, 0, 128): 5}.get(
            tuple(255 if v > 200 else (128 if 100 < v <= 200 else 0) for v in px))
    monkeypatch.setattr(vol, "ocr_volume_number", ocr)
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", colors[(n - 1) // 2 + 1], 2) for n in range(1, 11)]
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=True, volume_range=(2, 3)), log=logs.append)
    assert _names(result) == ["나루토 2권 (3화~4화).epub", "나루토 3권 (5화~6화).epub"]
    assert any("5권 중 2권만" in m for m in logs)


def test_episode_range_skips_reading_other_files(tmp_path):
    paths = [_page(tmp_path, f"나루토 {n}화", f"나루토 {n}화", "red", 2) for n in range(1, 11)]
    ticks: list = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=False, episode_range=(4, 9)),
                 log=lambda s: None, progress=lambda d, t: ticks.append((d, t)))
    assert ticks[-1] == (6, 6)  # 10개 중 6개만 읽음
    assert _nav_titles(result) == [f"나루토 {n}화" for n in range(4, 10)]


def test_fill_volume_numbers():
    assert vol.fill_volume_numbers([None, 23, None, None, 27], 1) == [22, 23, 24, 25, 27]
    assert vol.fill_volume_numbers([None, None], 12) == [12, 13]
    assert vol.fill_volume_numbers([5, None], 1) == [5, 6]


def test_helpers():
    assert vol.episode_label("나루토 100화 - 마나토끼") == "100화"
    assert vol.episode_label("오프닝") == "오프닝"
    assert vol.common_suffix(["a 1화 - 사이트", "a 2화 - 사이트"]) == " - 사이트"
    assert vol.common_suffix(["나루토 1화", "나루토 2화"]) == ""


def test_analyze_reports_volumes_and_marks_last_unconfirmed(tmp_path):
    """권 분석(변환 없이): 표지로 권을 나누고, 마지막 권은 '미확정', 앞 권은 '완결'로 표시."""
    from webtoon2pdf.cli import analyze_volumes, resolve_inputs
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화 - 사이트", "red" if n <= 3 else "blue", 2) for n in range(1, 6)]
    cfg = JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                    format="epub", group_by_cover=True, first_volume=12)
    report = analyze_volumes(cfg, resolve_inputs(cfg.start_url), log=lambda s: None)
    assert report.covers_detected is True
    assert [v.number for v in report.volumes] == [1, 2]              # 빨강 OCR 1권, 파랑 못 읽어 2권
    assert [v.status for v in report.volumes] == ["완결", "미확정"]
    assert [v.chapter_count for v in report.volumes] == [3, 2]
    assert report.total_chapters == 5
    # 분석만 했으므로 책 파일은 만들지 않는다
    assert not list(tmp_path.glob("*.epub")) and not (tmp_path / "b").exists()


def test_analyze_without_covers_reports_count_fallback(tmp_path):
    """표지가 없는 소스(미리보기형): covers_detected=False, 첫 권 번호부터 N개씩 추정."""
    from webtoon2pdf.cli import analyze_volumes, resolve_inputs
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", (n * 9 % 200 + 20, 50, 50), 2, site_img="gray")
             for n in range(100, 108)]
    cfg = JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                    format="epub", group_by_cover=True, group=4, first_volume=51)
    report = analyze_volumes(cfg, resolve_inputs(cfg.start_url), log=lambda s: None)
    assert report.covers_detected is False
    assert report.count_per_volume == 4
    assert [v.number for v in report.volumes] == [51, 52]            # 첫 권 번호 51부터
    assert all(v.status == "개수묶기" for v in report.volumes)


def test_build_images_jpg_and_webp(tmp_path):
    """낱장 이미지 내보내기: 투명 PNG를 흰 배경 JPG로, WEBP로 순번 파일 저장."""
    from webtoon2pdf.image_builder import build_images
    from webtoon2pdf.models import Chapter, ImageItem
    src = tmp_path / "src"; src.mkdir()
    items = []
    for i in range(3):
        p = src / f"{i}.png"
        Image.new("RGBA", (50, 60), (i * 80, 10, 10, 128)).save(p)
        items.append(ImageItem(src="", width=50, height=60, top=0, left=0, selector="", path=p))
    ch = Chapter(index=1, url="", title="1화", images=items)
    out = tmp_path / "책"
    assert build_images([ch], out, "jpg", log=lambda s: None) == 3
    assert sorted(p.name for p in out.glob("*.jpg")) == ["0001.jpg", "0002.jpg", "0003.jpg"]
    with Image.open(out / "0001.jpg") as im:
        assert im.mode == "RGB"                       # 알파가 흰 배경에 합성됨
    outw = tmp_path / "책w"
    assert build_images([ch], outw, "webp", log=lambda s: None) == 3
    assert len(list(outw.glob("*.webp"))) == 3


def test_run_jpg_output_is_image_folder(tmp_path):
    """format=jpg면 책 파일이 아니라 낱장 이미지 폴더(0001.jpg…)로 저장된다."""
    paths = [_page(tmp_path, f"ep{n}", f"나루토 {n}화", "red", 2) for n in (1, 2)]  # 표지1+본문2 ×2 = 6장
    out = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "책", work_dir=tmp_path / "w",
                        format="jpg", group_by_cover=False), log=lambda s: None)
    assert out.is_dir()
    assert [p.name for p in sorted(out.glob("*.jpg"))] == [f"{i:04d}.jpg" for i in range(1, 7)]
    assert not list(tmp_path.glob("*.epub")) and not list(tmp_path.glob("*.pdf"))


def test_ocr_reads_volume_number(tmp_path, monkeypatch):
    monkeypatch.undo()  # autouse 스텁 해제
    font_path = Path(r"C:\Windows\Fonts\malgun.ttf")
    if not font_path.exists():
        pytest.skip("한글 폰트 없음")
    im = Image.new("RGB", (600, 800), "white")
    d = ImageDraw.Draw(im)
    d.text((150, 300), "제 7 권", fill="black", font=ImageFont.truetype(str(font_path), 96))
    p = tmp_path / "cover.jpg"
    im.save(p, "JPEG", quality=95)
    n = vol.ocr_volume_number(p)
    if n is None:
        pytest.skip("이 PC에 윈도우 OCR 한국어 팩이 없음")
    assert n == 7
