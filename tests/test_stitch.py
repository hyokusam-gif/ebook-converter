from pathlib import Path

from PIL import Image

import random

from webtoon2pdf.models import Chapter, ImageItem
from webtoon2pdf.stitch import _infer_page_height, merge_slices_to_pages, stitch_canvas_strips


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


def _slice_item(tmp_path, n, w, h, solid=False):
    """웹툰 조각: solid면 단색 띠(거터), 아니면 가로 질감이 있는 노이즈."""
    p = tmp_path / f"wt{n:04d}.png"
    im = Image.new("RGB", (w, h), (250, 250, 250))
    if not solid:
        px = im.load()
        rnd = random.Random(n)
        for y in range(h):
            for x in range(w):
                px[x, y] = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
    im.save(p, "PNG")
    return ImageItem(src="", width=w, height=h, top=0, left=0, selector="", kind="img", path=p)


def test_merge_slices_reduces_pages_and_preserves_size(tmp_path):
    ch = Chapter(index=1, url="u", title="t", dir=tmp_path)
    ch.images = [_slice_item(tmp_path, i, 100, 150) for i in range(20)]  # 총 3000px
    merge_slices_to_pages(ch, 900, lambda s: None)
    heights = [Image.open(it.path).height for it in ch.images]
    assert 1 < len(ch.images) < 20                       # 20조각 → 몇 개의 긴 쪽으로
    assert all(Image.open(it.path).width == 100 for it in ch.images)  # 폭 유지
    assert sum(heights) == 3000                          # 총 높이 보존(조각 전부 포함)
    assert all(h <= 900 + 200 for h in heights[:-1])     # 각 쪽은 목표+창 이내(마지막 제외)


def test_merge_slices_cuts_at_gutter(tmp_path):
    ch = Chapter(index=1, url="u", title="t", dir=tmp_path)
    # 20조각(각 150px), 10번째만 단색 거터. 총 3000. target 1550 → 창[1350,1750], 거터는 행[1500,1650].
    ch.images = [_slice_item(tmp_path, i, 100, 150, solid=(i == 10)) for i in range(20)]
    merge_slices_to_pages(ch, 1550, lambda s: None)
    first_h = Image.open(ch.images[0].path).height
    assert 1500 <= first_h <= 1650                       # 장면 중간이 아니라 거터(단색 띠)에서 잘림


def test_merge_slices_single_image_noop(tmp_path):
    ch = Chapter(index=1, url="u", title="t", dir=tmp_path)
    ch.images = [_slice_item(tmp_path, 1, 100, 150)]
    merge_slices_to_pages(ch, 900, lambda s: None)
    assert len(ch.images) == 1                           # 1장뿐이면 그대로
