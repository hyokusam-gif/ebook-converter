from pathlib import Path

from webtoon2pdf import browser as br
from webtoon2pdf.downloader import ext_from_bytes
from webtoon2pdf.extractor import collect_candidates, extract_images, select_body_images
from webtoon2pdf.models import ImageItem


def _it(src, w, h, top, natural=0, original=""):
    return ImageItem(src=src, width=w, height=h, top=top, left=0, selector="x",
                     natural_width=natural, original_src=original)


WEBTOON_PAGE = [
    _it("/s/logo.png", 120, 40, 0),
    _it("/s/agerate.jpg", 800, 360, 50),      # 본문 폭과 같지만 다른 경로 + 낮음 → 앞에서 제거
    _it("/w/1/b1.jpg", 800, 1200, 100),
    _it("/w/1/b2.jpg", 800, 1200, 1300),
    _it("/w/1/b2.jpg", 800, 1200, 1300),      # 중복
    _it("/w/1/b3.jpg", 790, 1200, 2500),      # 폭 15% 이내
    _it("/w/1/b4.jpg", 800, 80, 3700),        # 같은 경로지만 배너 모양(80px) → 제외
    _it("/ads/banner.jpg", 728, 90, 3800),    # 배너 모양 → 제외
    _it("/t/t1.jpg", 200, 280, 3900),
    _it("/t/t2.jpg", 200, 280, 3900),
]

BLOG_PAGE = [
    _it("/s/logo.png", 120, 40, 0),
    _it("/p/a.jpg", 800, 600, 100),
    _it("/p/b.jpg", 600, 800, 800),
    _it("/p/c.jpg", 1000, 500, 1700),
    _it("/p/d.jpg", 800, 600, 2300),
    _it("/p/t1.jpg", 200, 200, 3000),
]


def test_webtoon_page_auto_picks_body_run():
    out = select_body_images(WEBTOON_PAGE, min_width=300, mode="auto")
    assert [i.src for i in out] == ["/w/1/b1.jpg", "/w/1/b2.jpg", "/w/1/b3.jpg"]
    assert select_body_images(WEBTOON_PAGE, mode="webtoon") == out


def test_blog_page_auto_keeps_all_large_images():
    out = select_body_images(BLOG_PAGE, min_width=300, mode="auto")
    assert [i.src for i in out] == ["/p/a.jpg", "/p/b.jpg", "/p/c.jpg", "/p/d.jpg"]
    assert select_body_images(BLOG_PAGE, mode="all") == out
    # webtoon 모드는 억지로 한 구간만 고른다
    assert len(select_body_images(BLOG_PAGE, mode="webtoon")) < 4


def test_all_mode_keeps_extras_but_not_banners():
    srcs = [i.src for i in select_body_images(WEBTOON_PAGE, min_width=300, mode="all")]
    assert "/s/agerate.jpg" in srcs          # 큰 부속물은 '전부' 모드에서 남는다
    assert "/ads/banner.jpg" not in srcs     # 배너 모양은 어느 모드에서도 제외


def test_min_width_uses_natural_width_and_original_link():
    items = [
        _it("/p/thumb-small.jpg", 220, 150, 0, natural=220),          # 정말 작은 것 → 제외
        _it("/p/thumb-hires.jpg", 220, 150, 100, natural=660),        # 표시만 작고 원본 큼 → 포함
        _it("/p/thumb.jpg", 150, 100, 200, natural=150, original="/p/original.jpg"),  # 원본 링크 → 포함
        _it("/s/icon.png", 24, 24, 300, natural=948),                 # 고해상도 아이콘 → 표시 폭이 너무 작아 제외
        _it("/s/icon2.png", 60, 80, 400, natural=480),
    ]
    out = select_body_images(items, min_width=300, mode="all")
    assert [i.src for i in out] == ["/p/thumb-hires.jpg", "/p/thumb.jpg"]


def test_auto_falls_back_to_all_when_uniform_blog_has_one_odd_photo():
    items = [_it(f"/p/{n}.jpg", 800, 600, n * 700) for n in range(6)]
    items.append(_it("/p/pano.jpg", 1000, 500, 5000))  # 같은 경로, 폭만 다름 → 부속물 아님
    out = select_body_images(items, min_width=300, mode="auto")
    assert len(out) == 7


def test_auto_treats_naver_style_extras_as_webtoon():
    items = [
        _it("/static/agerate/age_all.jpg", 690, 360, 280),
        _it("/webtoon/1/c1.jpg", 500, 3300, 640),
        _it("/webtoon/1/c2.jpg", 500, 2983, 3940),
        _it("/phinf/banner1.jpg", 300, 130, 8616, natural=640),
        _it("/phinf/banner2.jpg", 300, 130, 9919),
    ]
    out = select_body_images(items, min_width=300, mode="auto")
    assert [i.src for i in out] == ["/webtoon/1/c1.jpg", "/webtoon/1/c2.jpg"]


def test_banner_shaped_images_are_dropped_even_in_same_folder():
    """크롬 저장본처럼 모든 파일이 한 폴더(_files)에 있을 때: 광고 배너·구분선은 모양으로 걸러진다."""
    items = [_it(f"/p_files/main_{n}.png", 320, 80, n * 90) for n in range(10)]      # 광고 배너
    items += [_it("/p_files/line.png", 870, 26, 1000)]                               # 구분선
    items += [_it(f"/p_files/{n:04d}_x.jpg", 622, 900, 1100 + n * 910) for n in range(5)]  # 본문
    items += [_it("/p_files/viewbottom.png", 655, 80, 9000)]
    out = select_body_images(items, min_width=300, mode="auto")
    assert [i.width for i in out] == [622] * 5
    assert [i.width for i in select_body_images(items, min_width=300, mode="all")] == [622] * 5


def test_select_body_images_keeps_order_by_top():
    items = [_it("c", 800, 1000, 3000), _it("a", 800, 1000, 100), _it("b", 800, 1000, 2000)]
    assert [i.src for i in select_body_images(items)] == ["a", "b", "c"]


def test_ext_from_bytes():
    assert ext_from_bytes(b"\xff\xd8\xff\xe0") == ".jpg"
    assert ext_from_bytes(b"\x89PNG\r\n") == ".png"
    assert ext_from_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"
    assert ext_from_bytes(b"<html>", "text/html") is None
    assert ext_from_bytes(b"????", "image/png") == ".png"


def test_lazy_loaded_webtoon_page(site):
    with br.launch() as (_b, _c, page):
        br.load(page, f"{site}/ep1.html")
        br.scroll_to_bottom(page, step_wait=0.1)
        br.wait_images(page)
        cands = collect_candidates(page)
        assert not any(c.src.endswith("logo.png") for c in cands), "sticky header는 제외"
        names = [Path(i.src).name for i in extract_images(page)]
        assert names == [f"body_0{i}.jpg" for i in range(1, 6)]


def test_blog_page_resolves_thumbnail_to_original(site):
    with br.launch() as (_b, _c, page):
        br.load(page, f"{site}/blog.html")
        br.scroll_to_bottom(page, step_wait=0.1)
        br.wait_images(page)
        items = extract_images(page)
        names = [Path(i.original_src or i.src).name for i in items]
        assert names == ["body_01.jpg", "body_02.jpg", "body_03.png", "body_04.jpg"]
        assert Path(items[2].src).name == "thumb_a.jpg"  # 원본이 이미지가 아닐 때의 대체 주소


def test_canvas_viewer_pages_are_captured(site, tmp_path):
    """<canvas>에 그리는 뷰어: 캔버스를 본문으로 잡고 화면 캡처로 저장한다. 광고 배너는 제외."""
    from PIL import Image
    from webtoon2pdf.downloader import download_images
    with br.launch() as (_b, _c, page):
        br.load(page, f"{site}/canvas.html")
        br.scroll_to_bottom(page, step_wait=0.2)
        items = extract_images(page)
        assert [i.kind for i in items] == ["canvas"] * 3
        saved = download_images(page, items, tmp_path / "out", delay=0, min_width=300, log=lambda s: None)
        assert len(saved) == 3
        sizes = [Image.open(p).size for p in [s.path for s in saved]]
        assert sizes == [(720, 500), (720, 700), (720, 400)]
        with Image.open(saved[0].path) as im:  # 실제로 그려진 내용(빨강)이 캡처됐는지
            r, g, b = im.convert("RGB").getpixel((360, 250))
            assert r > 150 and g < 100
        with Image.open(saved[2].path) as im:  # 마지막 캔버스 아래쪽에 노란 고정 막대가 겹치지 않았는지
            r, g, b = im.convert("RGB").getpixel((360, 395))
            assert not (r > 200 and g > 200 and b < 80), "고정 막대가 캡처에 겹침"
        assert page.evaluate("getComputedStyle(document.getElementById('bar')).visibility") == "visible"  # 복구


def test_images_inside_iframe_are_collected(site):
    with br.launch() as (_b, _c, page):
        br.load(page, f"{site}/iframe.html")
        br.scroll_to_bottom(page, step_wait=0.1)
        br.wait_images(page)
        items = extract_images(page)
        assert [Path(i.src).name for i in items] == [f"body_0{i}.jpg" for i in range(1, 5)]
        assert all(i.frame_url.endswith("ep2.html") for i in items)
        assert items[0].top >= 150, "iframe 위치만큼 오프셋이 더해져야 한다"
