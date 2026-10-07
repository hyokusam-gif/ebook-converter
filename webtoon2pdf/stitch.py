"""캔버스 뷰어가 한 페이지를 여러 조각으로 나눠 그린 경우, 조각들을 원래 페이지로 이어 붙인다."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Callable

from PIL import Image

from .models import Chapter, ImageItem

Log = Callable[[str], None]


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
