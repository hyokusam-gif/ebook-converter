from pathlib import Path

from PIL import Image

from webtoon2pdf.models import Chapter, ImageItem
from webtoon2pdf.stitch import _infer_page_height, stitch_canvas_strips


def _canvas_item(tmp_path, n, w, h, color):
    p = tmp_path / f"{n:04d}.png"
    Image.new("RGB", (w, h), color).save(p, "PNG")
    return ImageItem(src=f"canvas:{n}", width=w, height=h, top=0, left=0, selector="", kind="canvas", path=p)


def test_infer_page_height():
    assert _infer_page_height([372, 668, 359, 681, 466, 574, 300, 740], 720) == 1040
    assert _infer_page_height([300, 300, 400, 300, 300, 400], 720) == 1000          # 세 조각짜리
    assert _infer_page_height([520, 520, 520, 520], 720) == 1040                     # 정확히 반으로 나눈 경우
    assert _infer_page_height([1041, 1041, 1041, 1041], 720) is None                # 이미 페이지 단위 → 이어 붙이지 않음
    assert _infer_page_height([500, 700, 400], 720) is None


def test_stitch_pairs_and_drops_ad_canvas(tmp_path):
    ch = Chapter(index=1, url="u", title="t", dir=tmp_path)
    ch.images = [
        _canvas_item(tmp_path, 1, 720, 372, "red"), _canvas_item(tmp_path, 2, 720, 668, "blue"),
        _canvas_item(tmp_path, 3, 720, 359, "green"), _canvas_item(tmp_path, 4, 600, 300, "yellow"),  # 광고
        _canvas_item(tmp_path, 5, 720, 681, "black"),
        _canvas_item(tmp_path, 6, 720, 466, "white"), _canvas_item(tmp_path, 7, 720, 574, "gray"),
        _canvas_item(tmp_path, 8, 720, 300, "pink"), _canvas_item(tmp_path, 9, 720, 740, "cyan"),
    ]
    logs: list[str] = []
    stitch_canvas_strips(ch, logs.append)
    assert len(ch.images) == 4
    sizes = [Image.open(i.path).size for i in ch.images]
    assert sizes == [(720, 1040), (720, 1040), (720, 1040), (720, 1040)]
    with Image.open(ch.images[0].path) as im:  # 위 조각(빨강) 아래 조각(파랑) 순서
        assert im.getpixel((10, 10)) == (255, 0, 0) and im.getpixel((10, 1000)) == (0, 0, 255)
    assert any("4페이지로 이어 붙임" in m and "광고 조각 1장 제외" in m for m in logs)


def test_stitch_leaves_non_canvas_alone(tmp_path):
    ch = Chapter(index=1, url="u", title="t", dir=tmp_path)
    it = _canvas_item(tmp_path, 1, 720, 372, "red")
    it.kind = "img"
    ch.images = [it, _canvas_item(tmp_path, 2, 720, 668, "blue")]
    stitch_canvas_strips(ch, lambda s: None)
    assert len(ch.images) == 2
