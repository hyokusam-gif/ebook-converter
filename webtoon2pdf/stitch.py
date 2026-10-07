"""캔버스 뷰어가 한 페이지를 여러 조각으로 나눠 그린 경우, 조각들을 원래 페이지로 이어 붙인다.
또 웹툰(세로 스크롤)을 변환할 때 작게 잘린 조각들을 '긴 한 쪽'으로 이어 붙여 끊김을 줄인다."""
from __future__ import annotations

import statistics
from collections import Counter
from pathlib import Path
from typing import Callable

from PIL import Image

from .models import Chapter, ImageItem

Log = Callable[[str], None]

# 웹툰 '길게 이어붙이기' 한 쪽 최대 높이(px). PDF 한 변 한계(_layout: 14000pt≈18,666px) 아래로 둔다.
DEFAULT_WEBTOON_PAGE_PX = 16000
_PROBE_W = 64  # 거터(빈 구간) 찾을 때 가로를 이 폭으로 축소해 빠르게 분석


MAX_PAGE_ASPECT = 1.8   # 페이지 높이/폭 상한 (만화 페이지는 1.3~1.6)
MIN_STRIP_ASPECT = 1.2  # 조각 높이/폭이 이 이상이면 이미 페이지 단위 → 이어 붙이지 않음


def _infer_page_height(heights: list[int], width: int) -> int | None:
    """연속 조각의 합이 일정한 높이 H가 되는지 찾는다. 조각 2~4개가 한 페이지를 이룬다고 가정.
    찾으면 H, 조각 나누기가 아니면 None. 조각이 이미 페이지 비율이면 None."""
    if len(heights) < 4 or width <= 0:
        return None
    med = sorted(heights)[len(heights) // 2]
    if med / width >= MIN_STRIP_ASPECT:
        return None
    for k in (2, 3, 4):
        H = sum(heights[:k])
        if H <= 0 or H / width > MAX_PAGE_ASPECT:
            continue
        groups, acc, ok = [], 0, 0
        for h in heights:
            acc += h
            if acc >= H * 0.97:
                groups.append(acc)
                acc = 0
        closed = groups if acc == 0 else groups  # 마지막 미완성 묶음은 제외하고 평가
        if len(closed) >= 2 and sum(1 for g in closed if H * 0.97 <= g <= H * 1.03) >= len(closed) * 0.9:
            return H
    return None


def stitch_canvas_strips(ch: Chapter, log: Log) -> None:
    """회차의 이미지가 전부 캔버스 캡처이고 조각 구조가 보이면 세로로 이어 붙여 페이지 단위로 만든다.
    본문 폭과 다른 조각(광고 캔버스)은 버린다."""
    items = [i for i in ch.images if i.path is not None]
    if not items or any(i.kind != "canvas" for i in items):
        return
    sizes = []
    for i in items:
        try:
            with Image.open(i.path) as im:
                sizes.append(im.size)
        except Exception:  # noqa: BLE001
            sizes.append((0, 0))
    dominant_w = Counter(w for w, _ in sizes).most_common(1)[0][0]
    keep = [(i, s) for i, s in zip(items, sizes) if s[0] == dominant_w]
    dropped = len(items) - len(keep)
    heights = [h for _, (_, h) in keep]
    H = _infer_page_height(heights, dominant_w)
    if H is None:
        if dropped:
            ch.images = [i for i, _ in keep]
            log(f"  폭이 다른 캔버스 조각(광고) {dropped}장 제외")
        return

    out_dir = ch.dir or items[0].path.parent
    pages: list[ImageItem] = []
    group: list[tuple[ImageItem, tuple[int, int]]] = []
    acc = 0

    def flush() -> None:
        nonlocal group, acc
        if not group:
            return
        total_h = sum(h for _, (_, h) in group)
        canvas = Image.new("RGB", (dominant_w, total_h), "white")
        y = 0
        for it, (_, h) in group:
            with Image.open(it.path) as im:
                canvas.paste(im.convert("RGB"), (0, y))
            y += h
        dest = out_dir / f"s{len(pages) + 1:04d}.png"
        canvas.save(dest, "PNG")
        pages.append(ImageItem(src=f"stitched:{len(pages) + 1}", width=dominant_w, height=total_h, top=0, left=0,
                               selector="", kind="canvas", path=dest))
        group, acc = [], 0

    for it, size in keep:
        group.append((it, size))
        acc += size[1]
        if acc >= H * 0.97:
            flush()
    flush()
    ch.images = pages
    log(f"  캔버스 조각 {len(keep)}장을 {len(pages)}페이지로 이어 붙임 (페이지 높이 약 {H}px)"
        + (f", 광고 조각 {dropped}장 제외" if dropped else ""))


# ---------- 웹툰 길게 이어붙이기 ----------

def _best_cut_row(strip: Image.Image, target: int, window: int) -> int:
    """strip(높이>=target)에서 target 부근의 '가장 평평한 행'(단색 띠=장면 사이 빈 구간)을 찾아 그 y를 돌려준다.
    뚜렷한 빈 구간이 없으면 target 경계로 자른다."""
    H = strip.height
    lo = max(1, target - window)
    hi = min(H - 1, target + window)
    if hi <= lo:
        return min(target, H)
    gray = strip.convert("L").resize((_PROBE_W, H))  # 가로만 축소(세로 행 구조 유지)
    px = gray.load()
    cache: dict[int, float] = {}

    def row_std(y: int) -> float:
        v = cache.get(y)
        if v is None:
            vals = [px[x, y] for x in range(_PROBE_W)]
            v = cache[y] = statistics.pstdev(vals)
        return v

    best_y, best_score = min(target, H - 1), None
    y = lo
    while y <= hi:
        band = [row_std(yy) for yy in range(max(0, y - 2), min(H, y + 3))]
        # 5px 밴드 평균이 작을수록 '띠'처럼 평평. 동점이면 target에 가까운 쪽(거리 패널티는 아주 작게).
        score = sum(band) / len(band) + 0.002 * abs(y - target)
        if best_score is None or score < best_score:
            best_score, best_y = score, y
        y += 2
    return best_y


def merge_slices_to_pages(ch: Chapter, target_px: int, log: Log) -> None:
    """웹툰처럼 작게 잘린 조각들을 세로로 이어 붙여 '긴 한 쪽'들로 다시 나눈다.
    쪽 경계는 장면 사이 빈 구간(거터)에서 자른다. 조각 1장뿐이면 아무것도 안 함."""
    items = [i for i in ch.images if i.path is not None]
    if len(items) < 2:
        return
    target_px = target_px if target_px and target_px > 0 else DEFAULT_WEBTOON_PAGE_PX

    widths: Counter[int] = Counter()
    for it in items:
        try:
            with Image.open(it.path) as im:
                widths[im.width] += 1
        except Exception:  # noqa: BLE001
            continue
    if not widths:
        return
    dom_w = widths.most_common(1)[0][0]
    if dom_w <= 0:
        return

    out_dir = ch.dir or items[0].path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    window = max(200, int(target_px * 0.12))
    pages: list[ImageItem] = []

    def open_sized(it: ImageItem) -> Image.Image | None:
        try:
            with Image.open(it.path) as im:
                im = im.convert("RGB")
                if im.width != dom_w:  # 폭이 다르면 대표 폭에 맞춰 비율 리사이즈
                    nh = max(1, round(im.height * dom_w / im.width))
                    im = im.resize((dom_w, nh), Image.LANCZOS)
                return im.copy()
        except Exception:  # noqa: BLE001
            return None

    def compose(imgs: list[Image.Image]) -> Image.Image:
        h = sum(i.height for i in imgs)
        strip = Image.new("RGB", (dom_w, h), "white")
        y = 0
        for im in imgs:
            strip.paste(im, (0, y))
            y += im.height
        return strip

    def save_page(img: Image.Image) -> None:
        dest = out_dir / f"w{len(pages) + 1:04d}.png"
        img.save(dest, "PNG")
        pages.append(ImageItem(src=f"webtoon:{len(pages) + 1}", width=img.width, height=img.height,
                               top=0, left=0, selector="", kind=items[0].kind, path=dest))

    pending: list[Image.Image] = []
    pending_h = 0
    for it in items:
        img = open_sized(it)
        if img is None:
            continue
        pending.append(img)
        pending_h += img.height
        while pending_h >= target_px:
            strip = compose(pending)
            cut = _best_cut_row(strip, target_px, window)
            save_page(strip.crop((0, 0, dom_w, cut)))
            rem = strip.crop((0, cut, dom_w, strip.height))
            pending = [rem] if rem.height > 0 else []
            pending_h = rem.height
    if pending:
        save_page(compose(pending))

    if pages:
        ch.images = pages
        log(f"  웹툰 조각 {len(items)}장을 {len(pages)}개의 긴 페이지로 이어 붙임 (한 쪽 최대 약 {target_px}px)")
