from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from webtoon2pdf.cli import run
from webtoon2pdf.models import Chapter, ImageItem, JobConfig
from webtoon2pdf.pdf_builder import build_pdf, normalize_image


def test_normalize_converts_webp_and_alpha(tmp_path):
    webp = tmp_path / "a.webp"
    Image.new("RGB", (100, 50), "red").save(webp, "WEBP")
    png_a = tmp_path / "b.png"
    Image.new("RGBA", (100, 50), (0, 255, 0, 128)).save(png_a, "PNG")
    jpg = tmp_path / "c.jpg"
    Image.new("RGB", (100, 50), "blue").save(jpg, "JPEG")

    out = tmp_path / "norm"
    assert normalize_image(webp, out).suffix == ".jpg"
    assert normalize_image(png_a, out).suffix == ".jpg"
    assert normalize_image(jpg, out) == jpg  # 원본 그대로


def test_build_pdf_adds_outline(tmp_path):
    chapters = []
    for ci in (1, 2):
        ch = Chapter(index=ci, url=f"u{ci}", title=f"{ci}화")
        for n in range(3):
            p = tmp_path / f"{ci}_{n}.jpg"
            Image.new("RGB", (800, 1200), "white").save(p, "JPEG")
            ch.images.append(ImageItem(src="", width=0, height=0, top=0, left=0, selector="", path=p))
        chapters.append(ch)
    out = tmp_path / "book.pdf"
    assert build_pdf(chapters, out, tmp_dir=tmp_path / "t", title="책", author="저자 A", log=lambda s: None) == 6
    r = PdfReader(out)
    assert len(r.pages) == 6
    assert r.metadata.title == "책" and r.metadata.author == "저자 A"
    assert [o.title for o in r.outline] == ["1화", "2화"]
    assert r.outline[1].page.indirect_reference == r.pages[3].indirect_reference


def test_end_to_end_follows_next_links(site, tmp_path):
    out = tmp_path / "book.pdf"
    cfg = JobConfig(
        start_url=f"{site}/ep1.html", output=out, max_chapters=10, delay=0,
        work_dir=tmp_path / "work", scroll_wait=0.1, keep_images=True,
    )
    result = run(cfg)
    assert result == out
    r = PdfReader(out)
    assert len(r.pages) == 5 + 4 + 3
    assert [o.title for o in r.outline] == ["테스트웹툰 1화", "테스트웹툰 2화", "테스트웹툰 3화 (완결)"]
    # 3화의 picture/srcset은 800w 원본을 골라야 한다
    saved = sorted((tmp_path / "work").rglob("003/*"))
    assert len(saved) == 3
    with Image.open(saved[0]) as im:
        assert im.width == 800


def test_blog_page_end_to_end(site, tmp_path):
    out = tmp_path / "blog.pdf"
    run(JobConfig(start_url=f"{site}/blog.html", output=out, max_chapters=1, delay=0,
                  work_dir=tmp_path / "work", scroll_wait=0.1))
    r = PdfReader(out)
    assert len(r.pages) == 4
    assert [o.title for o in r.outline] == ["여행 블로그 글"]


def test_max_chapters_limit(site, tmp_path):
    out = tmp_path / "book.pdf"
    cfg = JobConfig(start_url=f"{site}/ep1.html", output=out, max_chapters=2, delay=0,
                    work_dir=tmp_path / "work", scroll_wait=0.1)
    run(cfg)
    assert len(PdfReader(out).pages) == 9
    assert not (tmp_path / "work").exists() or not any((tmp_path / "work").iterdir())
