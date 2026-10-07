from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from webtoon2pdf.cli import resolve_input, run
from webtoon2pdf.models import JobConfig


def test_resolve_input(tmp_path):
    assert resolve_input("https://a.b/c") == ("url", "https://a.b/c")
    html = tmp_path / "page.html"
    html.write_text("<html></html>", encoding="utf-8")
    kind, uri = resolve_input(f'"{html}"')
    assert kind == "html" and uri.startswith("file:///")
    mh = tmp_path / "page.mhtml"
    mh.write_bytes(b"MIME-Version: 1.0\r\n\r\n")
    assert resolve_input(str(mh)) == ("mhtml", str(mh.resolve()))
    assert resolve_input(str(tmp_path)) == ("dir", str(tmp_path.resolve()))
    with pytest.raises(RuntimeError, match="올바르지"):
        resolve_input("없는파일.html")


def test_saved_page_reads_local_files_not_data_src(tmp_path):
    """크롬 '웹페이지, 전체' 저장본: src는 로컬 파일, data-src는 차단된 웹 주소로 남아 있다."""
    files = tmp_path / "saved_files"
    files.mkdir()
    for n in range(1, 4):
        Image.new("RGB", (800, 600 + n * 100), "white").save(files / f"img{n}.jpg", "JPEG")
    imgs = "\n".join(
        f'<img src="./saved_files/img{n}.jpg" data-src="https://blocked.invalid/img{n}.jpg" style="width:800px">'
        for n in range(1, 4)
    )
    (tmp_path / "saved.html").write_text(
        f"<html><head><meta charset='utf-8'><title>저장한 글</title></head><body>{imgs}"
        "<a href='https://blocked.invalid/next'>다음</a></body></html>", encoding="utf-8")

    logs: list[str] = []
    out = tmp_path / "saved.pdf"
    run(JobConfig(start_url=str(tmp_path / "saved.html"), output=out, max_chapters=5, delay=0,
                  work_dir=tmp_path / "work", scroll_wait=0.1), log=logs.append)
    r = PdfReader(out)
    assert len(r.pages) == 3
    assert [o.title for o in r.outline] == ["저장한 글"]
    assert not any("대체" in m or "건너뜀" in m for m in logs), logs
    assert any("이 페이지만" in m for m in logs)


def _make_mhtml(path, images: dict[str, bytes], html_body: str, title="단일 파일 저장") -> None:
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("related", type="text/html")
    msg["Subject"] = title
    msg["Snapshot-Content-Location"] = "https://example.test/post/1"
    h = MIMEText(f"<html><head><title>{title}</title></head><body>{html_body}</body></html>", "html", "utf-8")
    h["Content-Location"] = "https://example.test/post/1"
    msg.attach(h)
    for url, data in images.items():
        part = MIMEImage(data, "jpeg")
        part["Content-Location"] = url
        msg.attach(part)
    path.write_bytes(msg.as_bytes())


def _jpeg(w: int, h: int = 600) -> bytes:
    import io
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "JPEG")
    return buf.getvalue()


def test_mhtml_input_follows_html_order_and_skips_icons(tmp_path):
    imgs = {
        "https://cdn.test/a.jpg": _jpeg(400),
        "https://cdn.test/b.jpg": _jpeg(500),
        "https://cdn.test/c.jpg": _jpeg(600),
        "https://cdn.test/icon.png": _jpeg(16),
        "https://cdn.test/unused.jpg": _jpeg(700),   # HTML에서 참조되지 않음 → 제외
    }
    body = ('<img src="https://cdn.test/icon.png"><img src="https://cdn.test/b.jpg">'
            '<p>글</p><img src="https://cdn.test/a.jpg"><img src="https://cdn.test/c.jpg">'
            '<img src="https://cdn.test/b.jpg">')  # 중복
    f = tmp_path / "saved.mhtml"
    _make_mhtml(f, imgs, body)
    logs: list[str] = []
    out = tmp_path / "m.pdf"
    run(JobConfig(start_url=str(f), output=out, work_dir=tmp_path / "work"), log=logs.append)
    r = PdfReader(out)
    widths = [round(float(p.mediabox.width)) for p in r.pages]
    assert widths == [375, 300, 450]  # b(500), a(400), c(600) → HTML 순서
    assert any("MHTML" in m for m in logs)


def _saved_page(tmp_path, name: str, title: str, n_images: int) -> Path:
    files = tmp_path / f"{name}_files"
    files.mkdir(exist_ok=True)
    tags = []
    for i in range(n_images):
        Image.new("RGB", (600, 800), "white").save(files / f"{i}.jpg", "JPEG")
        tags.append(f'<img src="./{name}_files/{i}.jpg" style="width:600px">')
    p = tmp_path / f"{name}.html"
    p.write_text(f"<html><head><meta charset='utf-8'><title>{title}</title></head><body>{''.join(tags)}</body></html>",
                 encoding="utf-8")
    return p


def test_multiple_saved_pages_become_chapters_in_natural_order(tmp_path):
    import zipfile
    paths = [_saved_page(tmp_path, f"나루토 {n}화", f"나루토 {n}화 제목", n) for n in (10, 2, 1)]
    out = tmp_path / "book.epub"
    logs: list[str] = []
    ticks: list[tuple[int, int]] = []
    run(JobConfig(start_url=" | ".join(str(p) for p in paths), output=out, work_dir=tmp_path / "work",
                  format="epub"), log=logs.append, progress=lambda d, t: ticks.append((d, t)))
    assert ticks == [(0, 3), (1, 3), (2, 3), (3, 3)]  # 파일마다 진행 콜백
    with zipfile.ZipFile(out) as z:
        nav = z.read("OEBPS/nav.xhtml").decode("utf-8")
        opf = z.read("OEBPS/content.opf").decode("utf-8")
    import re as _re
    assert _re.findall(r"<li><a[^>]*>(.*?)</a></li>", nav) == ["나루토 1화 제목", "나루토 2화 제목", "나루토 10화 제목"]
    assert "<dc:title>나루토 1화 제목 ~ 나루토 10화 제목</dc:title>" in opf
    assert opf.count("<itemref") == 1 + 2 + 10


def test_folder_of_saved_pages_is_expanded_to_chapters(tmp_path):
    import zipfile
    folder = tmp_path / "saved"
    folder.mkdir()
    for n in (10, 2, 1):
        _saved_page(folder, f"나루토 {n}화", f"나루토 {n}화", n)
    out = tmp_path / "book.epub"
    run(JobConfig(start_url=str(folder), output=out, work_dir=tmp_path / "work", format="epub"), log=lambda s: None)
    with zipfile.ZipFile(out) as z:
        nav = z.read("OEBPS/nav.xhtml").decode("utf-8")
    import re as _re
    assert _re.findall(r"<li><a[^>]*>(.*?)</a></li>", nav) == ["나루토 1화", "나루토 2화", "나루토 10화"]


def test_group_splits_into_multiple_books(tmp_path):
    paths = [_saved_page(tmp_path, f"ep{n}", f"{n}화", 1) for n in range(1, 6)]
    out = tmp_path / "books.epub"
    result = run(JobConfig(start_url="|".join(str(p) for p in paths), output=out, work_dir=tmp_path / "work",
                           format="epub", group=2, first_volume=3), log=lambda s: None)
    assert result == tmp_path / "books"
    names = sorted(p.name for p in result.glob("*.epub"))
    assert names == ["books 3권 (1화~2화).epub", "books 4권 (3화~4화).epub", "books 5권 (5화).epub"]


def _saved_page_with_cover(tmp_path, name: str, title: str, cover_color: str, n_pages: int) -> Path:
    files = tmp_path / f"{name}_files"
    files.mkdir(exist_ok=True)
    Image.new("RGB", (600, 800), cover_color).save(files / "cover.jpg", "JPEG", quality=90)
    tags = [f'<img src="./{name}_files/cover.jpg" style="width:600px">']
    for i in range(n_pages):
        Image.new("RGB", (600, 800), (i * 20 % 255, 100, 100)).save(files / f"{i}.jpg", "JPEG")
        tags.append(f'<img src="./{name}_files/{i}.jpg" style="width:600px">')
    p = tmp_path / f"{name}.html"
    p.write_text(f"<html><head><meta charset='utf-8'><title>{title}</title></head><body>{''.join(tags)}</body></html>",
                 encoding="utf-8")
    return p


def test_group_by_cover_splits_volumes_and_dedupes_cover(tmp_path, monkeypatch):
    import zipfile
    from webtoon2pdf import volumes as vol
    def fake_ocr(p):
        with Image.open(p) as im:
            r, g, b = im.convert("RGB").getpixel((5, 5))
        return 1 if r > 200 and g < 80 else (2 if b > 200 and r < 80 else None)
    monkeypatch.setattr(vol, "ocr_volume_number", fake_ocr)
    paths = [_saved_page_with_cover(tmp_path, f"ep{n}", f"나루토 {n}화", "red" if n <= 3 else "blue", 2) for n in range(1, 6)]
    out = tmp_path / "books.epub"
    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(str(p) for p in paths), output=out, work_dir=tmp_path / "work",
                           format="epub", group_by_cover=True), log=logs.append)
    names = sorted(p.name for p in result.glob("*.epub"))
    assert names == ["나루토 1권 (1화~3화).epub", "나루토 2권 (4화~5화).epub"]
    with zipfile.ZipFile(result.with_suffix(".zip")) as z:  # 여러 권 한 번에 보내기용 zip
        assert sorted(z.namelist()) == names
    with zipfile.ZipFile(result / names[0]) as z:
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        assert opf.count("<itemref") == 3 + 2 + 2  # 1화는 표지 포함, 2·3화는 반복 표지 제거
    with zipfile.ZipFile(result / names[1]) as z:
        assert z.read("OEBPS/content.opf").decode("utf-8").count("<itemref") == 3 + 2

    # 이름이 다른 이전 결과물이 폴더에 남아 있으면, 다시 변환할 때 지우고 새로 만든다
    (result / "나루토 99권 (옛날).epub").write_bytes(b"stale")
    logs2: list[str] = []
    run(JobConfig(start_url="|".join(str(p) for p in paths), output=out, work_dir=tmp_path / "work",
                  format="epub", group_by_cover=True), log=logs2.append)
    assert sorted(p.name for p in result.glob("*.epub")) == names  # 옛 파일 사라짐
    assert any("이전 결과물" in m and "지우고" in m for m in logs2)
    assert any("2권으로 나눔" in m and "반복 표지 3장 제거" in m for m in logs)


def test_mixed_url_and_file_is_rejected(tmp_path):
    p = _saved_page(tmp_path, "a", "a", 1)
    with pytest.raises(RuntimeError, match="하나씩"):
        run(JobConfig(start_url=f"https://x.test/1 | {p}", output=tmp_path / "x.epub", work_dir=tmp_path / "work"))


def test_folder_input_natural_order(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    for name, w in (("10.jpg", 300), ("2.jpg", 200), ("1.jpg", 100)):
        Image.new("RGB", (w, 200), "white").save(folder / name, "JPEG")
    Image.new("RGB", (320, 80), "white").save(folder / "0_ad.jpg", "JPEG")  # 배너 모양 → 제외
    (folder / "note.txt").write_text("x")
    out = tmp_path / "folder.pdf"
    run(JobConfig(start_url=str(folder), output=out, work_dir=tmp_path / "work", min_width=100), log=lambda s: None)
    r = PdfReader(out)
    widths = [round(float(p.mediabox.width)) for p in r.pages]
    assert widths == [75, 150, 225]  # 1, 2, 10 순서 (100/200/300px → 96dpi 기준 pt)
