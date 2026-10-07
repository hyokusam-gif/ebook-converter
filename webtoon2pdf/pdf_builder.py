"""이미지 정규화 → img2pdf → pypdf 북마크."""
from __future__ import annotations

import io
from collections import Counter
from pathlib import Path
from typing import Callable

import img2pdf
from PIL import Image
from pypdf import PdfWriter

from .models import Chapter

MAX_PAGE_PT = 14000.0  # PDF 뷰어 호환 한계(14400pt)보다 조금 작게
BASE_DPI = 96.0


def _layout(imgwidthpx: int, imgheightpx: int, ndpi):
    """이미지 픽셀을 96dpi 기준 pt로 놓되, 한 변이 한계를 넘으면 비례 축소."""
    w = imgwidthpx * 72.0 / BASE_DPI
    h = imgheightpx * 72.0 / BASE_DPI
    scale = min(1.0, MAX_PAGE_PT / max(w, h))
    w, h = w * scale, h * scale
    return (w, h, w, h)


def normalize_image(path: Path, tmp_dir: Path, target_width: int | None = None) -> Path | None:
    """img2pdf가 그대로 넣을 수 있는 JPEG/PNG로 만든다. 손상 파일이면 None."""
    try:
        with Image.open(path) as im:
            im.load()
            fmt = im.format
            mode = im.mode
            width = im.width
            has_alpha = mode in ("RGBA", "LA", "PA") or (mode == "P" and "transparency" in im.info)
            need_resize = target_width is not None and width != target_width

            if fmt in ("JPEG", "PNG") and not has_alpha and not need_resize and mode in ("RGB", "L", "CMYK", "1", "P"):
                return path

            tmp_dir.mkdir(parents=True, exist_ok=True)
            out = tmp_dir / f"{path.stem}.jpg"
            if im.mode == "P":
                im = im.convert("RGBA" if has_alpha else "RGB")
            if has_alpha:
                bg = Image.new("RGB", im.size, (255, 255, 255))
                bg.paste(im, mask=im.getchannel("A") if "A" in im.getbands() else None)
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")
            if need_resize and target_width:
                new_h = max(1, round(im.height * target_width / im.width))
                im = im.resize((target_width, new_h), Image.LANCZOS)
            im.save(out, "JPEG", quality=95, optimize=True)
            return out
    except Exception:  # noqa: BLE001
        return None


def most_common_width(paths: list[Path]) -> int | None:
    widths: Counter[int] = Counter()
    for p in paths:
        try:
            with Image.open(p) as im:
                widths[im.width] += 1
        except Exception:  # noqa: BLE001
            continue
    return widths.most_common(1)[0][0] if widths else None


def build_pdf(
    chapters: list[Chapter],
    out_path: Path,
    fit_width: bool = False,
    tmp_dir: Path | None = None,
    title: str | None = None,
    author: str | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """chapters의 이미지를 순서대로 PDF 한 권으로 만들고 회차 북마크를 단다. 총 페이지 수를 돌려준다."""
    tmp_dir = tmp_dir or out_path.parent / ".tmp_normalized"
    all_paths = [p for ch in chapters for p in ch.image_paths]
    target = most_common_width(all_paths) if fit_width else None

    pages: list[Path] = []
    outline: list[tuple[str, int]] = []  # (제목, 시작 페이지 인덱스)
    for ch in chapters:
        start = len(pages)
        for p in ch.image_paths:
            norm = normalize_image(p, tmp_dir / f"ch{ch.index:03d}", target_width=target)
            if norm is None:
                log(f"  [건너뜀] 손상된 이미지: {p.name}")
                continue
            pages.append(norm)
        if len(pages) > start:
            outline.append((ch.title or f"{ch.index}화", start))

    if not pages:
        raise RuntimeError("PDF에 넣을 이미지가 없습니다.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_bytes = img2pdf.convert([str(p) for p in pages], layout_fun=_layout)

    writer = PdfWriter()
    writer.append(fileobj=io.BytesIO(pdf_bytes))
    for bm_title, idx in outline:
        writer.add_outline_item(bm_title, idx)
    meta = {"/Title": title or out_path.stem, "/Producer": "webtoon2pdf"}
    if author:
        meta["/Author"] = author
    writer.add_metadata(meta)
    with open(out_path, "wb") as f:
        writer.write(f)
    return len(pages)
