"""이미지들을 EPUB 3 전자책으로 묶는다. 아이폰·아이패드 '책' 앱, 대부분의 리더에서 열린다."""
from __future__ import annotations

import html
import uuid
import zipfile
from pathlib import Path
from typing import Callable

from PIL import Image

from .models import Chapter
from .pdf_builder import normalize_image

MAX_ASPECT = 1.6          # 높이/폭이 이 값을 넘는 이미지는 잘라서 여러 페이지로
SEARCH_WINDOW = 0.15      # 자를 위치를 목표 지점 ±15% 범위에서 가장 빈 줄로 고른다


def _row_activity(im: Image.Image) -> list[float]:
    """각 행이 얼마나 '내용이 있는지'(0=완전히 균일). 폭을 줄여 빠르게 계산."""
    small = im.convert("L").resize((min(64, im.width), im.height))
    w, h = small.size
    data = small.tobytes()  # L 모드: 픽셀당 1바이트
    rows: list[float] = []
    for y in range(h):
        row = data[y * w:(y + 1) * w]
        mean = sum(row) / w
        rows.append(sum(abs(v - mean) for v in row) / w)
    return rows


def split_tall_image(path: Path, out_dir: Path, max_aspect: float = MAX_ASPECT) -> list[Path]:
    """세로로 긴 이미지를 화면에 맞는 조각으로 나눈다. 조각 경계는 가급적 빈 줄(여백)에 둔다."""
    with Image.open(path) as im:
        w, h = im.size
        if h <= w * max_aspect or w == 0:
            return [path]
        target = int(w * max_aspect)
        count = -(-h // target)               # 필요한 조각 수(올림)
        ideal = h / count                     # 조각을 균등하게 → 자투리가 남지 않음
        activity = _row_activity(im)
        out_dir.mkdir(parents=True, exist_ok=True)
        cuts: list[int] = []
        prev = 0
        for i in range(1, count):
            goal = round(i * ideal)
            lo = max(prev + int(ideal * 0.5), goal - int(ideal * SEARCH_WINDOW))
            hi = min(h - 1, goal + int(ideal * SEARCH_WINDOW))
            cut = min(range(lo, hi + 1), key=lambda r: activity[r]) if hi > lo else goal
            cuts.append(cut)
            prev = cut
        pieces: list[Path] = []
        y = 0
        for n, cut in enumerate(cuts + [h], start=1):
            piece = out_dir / f"{path.stem}_p{n:02d}.jpg"
            im.crop((0, y, w, cut)).convert("RGB").save(piece, "JPEG", quality=92)
            pieces.append(piece)
            y = cut
        return pieces


def _xhtml(title: str, img_name: str, w: int, h: int) -> str:
    """고정 레이아웃 페이지: 뷰포트를 이미지 크기로 맞춰 한 이미지가 한 화면을 채운다.
    도서(Books) 앱에서 핀치 확대 후에도 페이지 넘김이 유지된다."""
    t = html.escape(title)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><meta charset="utf-8"/><title>{t}</title>
<meta name="viewport" content="width={w}, height={h}"/>
<style>html,body{{margin:0;padding:0;width:{w}px;height:{h}px;background:#fff}} img{{width:{w}px;height:{h}px;display:block}}</style>
</head>
<body><img src="images/{img_name}" alt="{t}"/></body>
</html>"""


APPLE_DISPLAY_OPTIONS = """<?xml version="1.0" encoding="UTF-8"?>
<display_options>
  <platform name="*">
    <option name="fixed-layout">true</option>
    <option name="open-to-spread">false</option>
  </platform>
</display_options>"""


def build_epub(
    chapters: list[Chapter],
    out_path: Path,
    tmp_dir: Path | None = None,
    split_tall: bool = True,
    title: str | None = None,
    author: str | None = None,
    rtl: bool = True,
    log: Callable[[str], None] = print,
) -> int:
    """chapters의 이미지를 한 장에 한 페이지씩 EPUB으로 만든다. 총 페이지 수를 돌려준다."""
    tmp_dir = tmp_dir or out_path.parent / ".tmp_epub"
    book_title = title or out_path.stem
    book_id = f"urn:uuid:{uuid.uuid4()}"

    pages: list[tuple[str, Path, str | None]] = []  # (page_id, image path, 챕터 제목이면 문자열)
    for ch in chapters:
        first = True
        for p in ch.image_paths:
            norm = normalize_image(p, tmp_dir / f"ch{ch.index:03d}")
            if norm is None:
                log(f"  [건너뜀] 손상된 이미지: {p.name}")
                continue
            pieces = split_tall_image(norm, tmp_dir / f"ch{ch.index:03d}" / "split") if split_tall else [norm]
            for piece in pieces:
                pid = f"p{len(pages) + 1:05d}"
                pages.append((pid, piece, (ch.title or f"{ch.index}화") if first else None))
                first = False
    if not pages:
        raise RuntimeError("EPUB에 넣을 이미지가 없습니다.")

    manifest: list[str] = ['<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>']
    spine: list[str] = []
    nav_items: list[str] = []
    files: list[tuple[str, bytes]] = []

    for pid, img, ch_title in pages:
        ext = img.suffix.lower()
        mime = "image/png" if ext == ".png" else "image/jpeg"
        img_name = f"{pid}{ext}"
        with Image.open(img) as im:
            w, h = im.size
        files.append((f"OEBPS/images/{img_name}", img.read_bytes()))
        files.append((f"OEBPS/{pid}.xhtml", _xhtml(ch_title or book_title, img_name, w, h).encode("utf-8")))
        props = ' properties="cover-image"' if pid == pages[0][0] else ""
        manifest.append(f'<item id="img-{pid}" href="images/{img_name}" media-type="{mime}"{props}/>')
        manifest.append(f'<item id="{pid}" href="{pid}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="{pid}"/>')
        if ch_title:
            nav_items.append(f'<li><a href="{pid}.xhtml">{html.escape(ch_title)}</a></li>')

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid"
         prefix="rendition: http://www.idpf.org/vocab/rendition/#">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{book_id}</dc:identifier>
<dc:title>{html.escape(book_title)}</dc:title>
{f'<dc:creator id="creator">{html.escape(author)}</dc:creator><meta refines="#creator" property="role" scheme="marc:relators">aut</meta>' if author else ''}
<dc:language>ko</dc:language>
<meta property="dcterms:modified">2000-01-01T00:00:00Z</meta>
<meta property="rendition:layout">pre-paginated</meta>
<meta property="rendition:orientation">auto</meta>
<meta property="rendition:spread">none</meta>
</metadata>
<manifest>
{chr(10).join(manifest)}
</manifest>
<spine{' page-progression-direction="rtl"' if rtl else ''}>
{chr(10).join(spine)}
</spine>
</package>"""
    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><meta charset="utf-8"/><title>목차</title></head>
<body><nav epub:type="toc"><h1>목차</h1><ol>
{chr(10).join(nav_items)}
</ol></nav></body></html>"""
    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("META-INF/com.apple.ibooks.display-options.xml", APPLE_DISPLAY_OPTIONS, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, compress_type=zipfile.ZIP_DEFLATED)
        for name, data in files:
            z.writestr(name, data, compress_type=zipfile.ZIP_STORED if name.endswith((".jpg", ".png")) else zipfile.ZIP_DEFLATED)
    return len(pages)
