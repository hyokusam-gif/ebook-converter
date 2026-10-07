"""회차 안 일부 이미지 누락: 감지 → 원본 주소에서 보완 → 점검 창 통합."""
import io
import zipfile
from pathlib import Path

from PIL import Image

from webtoon2pdf import volumes as vol
from webtoon2pdf.cli import run
from webtoon2pdf.mhtml import chapter_from_mhtml
from webtoon2pdf.models import JobConfig


def _jpeg(w=800, h=1200, color="white") -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "JPEG")
    return buf.getvalue()


def _mhtml(path: Path, page_url: str, images: dict[str, bytes], srcs: list[str], title: str) -> Path:
    from email.mime.image import MIMEImage
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    msg = MIMEMultipart("related", type="text/html")
    msg["Subject"] = title
    msg["Snapshot-Content-Location"] = page_url
    body = "".join(f'<img src="{s}">' for s in srcs)
    h = MIMEText(f"<html><head><title>{title}</title></head><body>{body}</body></html>", "html", "utf-8")
    h["Content-Location"] = page_url
    msg.attach(h)
    for url, data in images.items():
        part = MIMEImage(data, "jpeg")
        part["Content-Location"] = url
        msg.attach(part)
    path.write_bytes(msg.as_bytes())
    return path


def test_mhtml_detects_missing_body_images_only(tmp_path, site):
    present = {f"{site}/img/body_01.jpg": _jpeg(), f"{site}/img/body_02.jpg": _jpeg()}
    srcs = [f"{site}/ads/banner.jpg",           # 광고 폴더 — 빠져도 세지 않음
            f"{site}/img/body_01.jpg", f"{site}/img/body_03.png",  # 본문 폴더인데 파일에 없음 → 빠진 장
            f"{site}/img/body_02.jpg"]
    f = _mhtml(tmp_path / "ep.mhtml", f"{site}/ep1.html", present, srcs, "나루토 3화")
    ch = chapter_from_mhtml(f, tmp_path / "out", log=lambda s: None)
    assert [Path(i.src).name for i in ch.images] == ["body_01.jpg", "body_02.jpg"]
    assert ch.missing == [(1, f"{site}/img/body_03.png")]   # body_01 뒤(위치 1)에 끼워 넣어야 함
    assert ch.page_url == f"{site}/ep1.html"


def test_repair_fetches_missing_from_origin_in_order(tmp_path, site):
    present = {f"{site}/img/body_01.jpg": _jpeg(color="red"), f"{site}/img/body_02.jpg": _jpeg(color="blue")}
    srcs = [f"{site}/img/body_01.jpg", f"{site}/img/body_03.png", f"{site}/img/body_02.jpg"]
    f = _mhtml(tmp_path / "ep.mhtml", f"{site}/ep1.html", present, srcs, "나루토 3화")
    ch = chapter_from_mhtml(f, tmp_path / "out", log=lambda s: None)
    logs: list[str] = []
    vol.repair_missing([ch], logs.append)
    assert ch.missing == []
    assert [Path(i.path).name[:1] for i in ch.images] == ["0", "r", "0"]  # 원래 자리(둘째)에 끼워 넣음
    with Image.open(ch.images[1].path) as im:
        assert im.width == 800  # conftest의 img/body_03.png (800×1200)
    assert any("모두 원본 주소에서 받았습니다" in m for m in logs)


def test_unrepairable_missing_goes_to_precheck(tmp_path, site):
    def make(n, srcs):
        return _mhtml(tmp_path / f"ep{n}.mhtml", f"{site}/ep{n}.html",
                      {f"{site}/img/body_01.jpg": _jpeg(color=(n, 0, 0)), f"{site}/img/body_02.jpg": _jpeg(color=(0, n, 0))},
                      srcs, f"나루토 {n}화")
    good = [f"{site}/img/body_01.jpg", f"{site}/img/body_02.jpg"]
    paths = [make(1, good), make(2, good + [f"{site}/img/nope.jpg"]), make(3, good), make(4, good)]
    asked: list = []

    def ask(bad, missing):
        asked.append(([t for t, _ in bad], missing))
        return []

    logs: list[str] = []
    result = run(JobConfig(start_url="|".join(map(str, paths)), output=tmp_path / "b.epub", work_dir=tmp_path / "w",
                           format="epub", group_by_cover=False), log=logs.append, ask_replacements=ask)
    assert asked == [(["나루토 2화 (일부 이미지 1장 빠짐)"], [])]
    assert any("받지 못했습니다" in m for m in logs)
    with zipfile.ZipFile(result) as z:
        nav = z.read("OEBPS/nav.xhtml").decode("utf-8")
    assert "나루토 2화" not in nav and "나루토 3화" in nav   # 못 채운 회차는 책에서 빠짐
