import zipfile

from PIL import Image

from webtoon2pdf.cli import run
from webtoon2pdf.epub_builder import build_epub, split_tall_image
from webtoon2pdf.models import Chapter, ImageItem, JobConfig


def _ch(tmp_path, index, title, sizes):
    ch = Chapter(index=index, url=f"u{index}", title=title)
    for n, (w, h) in enumerate(sizes):
        p = tmp_path / f"c{index}_{n}.jpg"
        Image.new("RGB", (w, h), "white").save(p, "JPEG")
        ch.images.append(ImageItem(src="", width=0, height=0, top=0, left=0, selector="", path=p))
    return ch


def test_split_tall_image_prefers_blank_rows(tmp_path):
    # 500x4000: 위에서 1000px, 2000px 근처에 흰 여백 띠를 두고 나머지는 회색 내용
    im = Image.new("RGB", (500, 4000), (120, 120, 120))
    for y0 in (1000, 2000, 3000):
        im.paste((255, 255, 255), (0, y0 - 10, 500, y0 + 10))
    # 내용 부분에 세로 줄무늬를 넣어 '활동' 있는 행으로 만든다
    for x in range(0, 500, 20):
        im.paste((0, 0, 0), (x, 0, x + 5, 4000))
    for y0 in (1000, 2000, 3000):
        im.paste((255, 255, 255), (0, y0 - 10, 500, y0 + 10))
    src = tmp_path / "tall.jpg"
    im.save(src, "JPEG")
    pieces = split_tall_image(src, tmp_path / "split", max_aspect=1.6)  # 목표 800px, ±15% → 680~920 구간에 여백 없음
    assert len(pieces) >= 4
    total = sum(Image.open(p).height for p in pieces)
    assert total == 4000
    for p in pieces:
        with Image.open(p) as pi:
            assert pi.height <= 500 * 1.6 * 1.15 + 1

    # 목표 지점 근처에 여백이 있으면 그 줄에서 자른다: max_aspect=2.0 → 목표 1000px
    pieces2 = split_tall_image(src, tmp_path / "split2", max_aspect=2.0)
    with Image.open(pieces2[0]) as p0:
        assert 990 <= p0.height <= 1010

    short = tmp_path / "short.jpg"
    Image.new("RGB", (500, 600), "white").save(short, "JPEG")
    assert split_tall_image(short, tmp_path / "split3") == [short]


def test_build_epub_structure(tmp_path):
    chapters = [_ch(tmp_path, 1, "1화", [(800, 1200), (800, 600)]), _ch(tmp_path, 2, "2화", [(800, 900)])]
    out = tmp_path / "book.epub"
    assert build_epub(chapters, out, tmp_dir=tmp_path / "t", title="테스트 책", author="Kishimoto Masashi",
                      log=lambda s: None) == 3
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
        assert names[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"
        assert "META-INF/container.xml" in names
        opf = z.read("OEBPS/content.opf").decode("utf-8")
        assert "<dc:title>테스트 책</dc:title>" in opf
        assert '<dc:creator id="creator">Kishimoto Masashi</dc:creator>' in opf
        assert opf.count("<itemref") == 3
        assert 'properties="cover-image"' in opf
        assert '<meta property="rendition:layout">pre-paginated</meta>' in opf  # 고정 레이아웃(만화용)
        assert '<spine page-progression-direction="rtl">' in opf  # 기본: 오른쪽→왼쪽(일본 만화)

    out2 = tmp_path / "book_ltr.epub"
    build_epub(chapters, out2, tmp_dir=tmp_path / "t2", rtl=False, log=lambda s: None)
    with zipfile.ZipFile(out2) as z:
        opf2 = z.read("OEBPS/content.opf").decode("utf-8")
        assert "page-progression-direction" not in opf2 and "<spine>" in opf2
        page1 = z.read("OEBPS/p00001.xhtml").decode("utf-8")
        assert '<meta name="viewport" content="width=800, height=1200"/>' in page1
        assert "META-INF/com.apple.ibooks.display-options.xml" in names
        nav = z.read("OEBPS/nav.xhtml").decode("utf-8")
        assert "1화" in nav and "2화" in nav
        assert sum(1 for n in names if n.startswith("OEBPS/images/")) == 3


def test_run_format_both_writes_pdf_and_epub(tmp_path):
    folder = tmp_path / "imgs"
    folder.mkdir()
    for n in range(3):
        Image.new("RGB", (600, 400), "white").save(folder / f"{n}.jpg", "JPEG")
    out = tmp_path / "book.epub"
    result = run(JobConfig(start_url=str(folder), output=out, work_dir=tmp_path / "work", format="both"),
                 log=lambda s: None)
    assert result == tmp_path / "book.pdf"
    assert (tmp_path / "book.pdf").exists() and (tmp_path / "book.epub").exists()

    result = run(JobConfig(start_url=str(folder), output=tmp_path / "x.pdf", work_dir=tmp_path / "work", format="epub"),
                 log=lambda s: None)
    assert result == tmp_path / "x.epub"
