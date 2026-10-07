"""브라우저 컨텍스트로 이미지를 저장한다. 실패하면 요소 스크린샷으로 대체."""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from urllib.request import url2pathname

from PIL import Image
from playwright.sync_api import Page

from .models import ImageItem, is_banner_shaped

CT_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
}


def ext_from_bytes(data: bytes, content_type: str = "") -> str | None:
    """바이트 시그니처로 이미지 확장자를 판정한다. 이미지가 아니면 None."""
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG"):
        return ".png"
    if data.startswith(b"GIF8"):
        return ".gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data.startswith(b"BM"):
        return ".bmp"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    ct = content_type.split(";")[0].strip().lower()
    return CT_EXT.get(ct)


def fetch_image(context, url: str, referer: str = "") -> tuple[bytes, str] | None:
    """브라우저 컨텍스트로 이미지 하나를 받는다(Referer 지정). 이미지가 아니거나 실패하면 None. 차단 우회는 하지 않는다."""
    headers = {"Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    try:
        resp = context.request.get(url, headers=headers, timeout=30000)
    except Exception:  # noqa: BLE001
        return None
    if not resp.ok:
        return None
    data = resp.body()
    return (data, resp.headers.get("content-type", "")) if ext_from_bytes(data, resp.headers.get("content-type", "")) else None


def _fetch_via_context(page: Page, src: str) -> tuple[bytes, str] | None:
    resp = page.context.request.get(
        src,
        headers={"Referer": page.url, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"},
        timeout=30000,
    )
    if not resp.ok:
        return None
    return resp.body(), resp.headers.get("content-type", "")


FETCH_IN_PAGE_JS = """async (u) => {
  try {
    const r = await fetch(u); if (!r.ok) return null;
    const b = await r.blob();
    const buf = await b.arrayBuffer();
    let s = ''; const bytes = new Uint8Array(buf);
    for (let i = 0; i < bytes.length; i += 0x8000) {
      s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return { b64: btoa(s), type: b.type };
  } catch (e) { return null; }
}"""


def _fetch_in_page(page: Page, src: str) -> tuple[bytes, str] | None:
    """blob: URL 등 페이지 안에서만 접근 가능한 자원을 fetch로 읽는다."""
    res = page.evaluate(FETCH_IN_PAGE_JS, src)
    if not res:
        return None
    return base64.b64decode(res["b64"]), res.get("type", "")


def _screenshot(page: Page, item: ImageItem, path: Path, settle_ms: int = 0) -> bool:
    try:
        scope = page
        if item.frame_url:
            scope = next((f for f in page.frames if f.url == item.frame_url), page)
        loc = scope.locator(item.selector).first
        loc.scroll_into_view_if_needed(timeout=5000)
        if settle_ms:
            page.wait_for_timeout(settle_ms)
        loc.screenshot(path=str(path), timeout=10000)
        return path.exists() and path.stat().st_size > 0
    except Exception:  # noqa: BLE001
        return False


def _looks_blank(path: Path) -> bool:
    """캔버스가 아직 그려지지 않아 단색인지 (16×16으로 줄여 밝기 편차로 판단)."""
    try:
        with Image.open(path) as im:
            small = im.convert("L").resize((16, 16))
            vals = list(small.tobytes())
            return max(vals) - min(vals) < 8
    except Exception:  # noqa: BLE001
        return False


HIDE_OVERLAYS_JS = """(hide) => {
  // 화면에 고정된 요소(상단바·하단 이전/다음 막대·광고)가 캡처에 겹치지 않게 잠시 숨긴다
  if (hide) {
    for (const el of document.querySelectorAll('body *')) {
      const pos = getComputedStyle(el).position;
      if ((pos === 'fixed' || pos === 'sticky') && !el.dataset.w2pHidden) {
        el.dataset.w2pHidden = el.style.visibility || '-';
        el.style.visibility = 'hidden';
      }
    }
  } else {
    for (const el of document.querySelectorAll('[data-w2p-hidden]')) {
      el.style.visibility = el.dataset.w2pHidden === '-' ? '' : el.dataset.w2pHidden;
      delete el.dataset.w2pHidden;
    }
  }
}"""


def set_overlays_hidden(page: Page, hide: bool) -> None:
    try:
        page.evaluate(HIDE_OVERLAYS_JS, hide)
    except Exception:  # noqa: BLE001
        pass


def _capture_canvas(page: Page, item: ImageItem, path: Path) -> bool:
    """<canvas> 뷰어: 그려질 때까지 잠깐 기다렸다가 화면을 캡처한다. 단색이면 더 기다려 한 번 더."""
    for wait in (400, 1500, 3000):
        if _screenshot(page, item, path, settle_ms=wait) and not _looks_blank(path):
            return True
    return path.exists()


def _fetch_any(page: Page, src: str, retries: int) -> tuple[bytes, str] | None:
    """URL 종류에 맞게 받아서 (bytes, content-type)을 돌려준다. 이미지가 아니면 None."""
    if src.startswith("data:"):
        header, b64 = src.split(",", 1)
        data = base64.b64decode(b64)
        ctype = header[5:].split(";")[0]
        return (data, ctype) if ext_from_bytes(data, ctype) else None
    if src.startswith("blob:"):
        r = _fetch_in_page(page, src)
        return r if r and ext_from_bytes(r[0], r[1]) else None
    if src.lower().startswith("file://"):
        local = Path(url2pathname(urlparse(src).path))
        if not local.is_file():
            return None
        data = local.read_bytes()
        return (data, "") if ext_from_bytes(data) else None
    for attempt in range(retries):
        try:
            r = _fetch_via_context(page, src)
        except Exception:  # noqa: BLE001
            r = None
        if r and ext_from_bytes(r[0], r[1]):
            return r
        if r is not None and not ext_from_bytes(r[0], r[1]):
            break  # 응답은 왔지만 이미지가 아님(HTML 등) → 재시도 무의미
        time.sleep(0.5 * (attempt + 1))
    r = _fetch_in_page(page, src)
    return r if r and ext_from_bytes(r[0], r[1]) else None


def _pixel_size(path: Path) -> tuple[int, int] | None:
    try:
        with Image.open(path) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None


def download_images(
    page: Page,
    items: list[ImageItem],
    out_dir: Path,
    delay: float = 0.3,
    retries: int = 3,
    min_width: int = 0,
    log: Callable[[str], None] = print,
    missing: list[tuple[int, str]] | None = None,
) -> list[ImageItem]:
    """items를 순서대로 out_dir에 저장하고, path가 채워진 항목만 돌려준다.

    original_src(썸네일을 감싼 원본 링크)가 있으면 먼저 시도하고, 이미지가 아니면 src로 받는다.
    min_width가 주어지면 실제 픽셀 폭이 그 절반 미만인 파일(아이콘 등)은 버린다.
    missing 리스트를 주면 저장한 페이지에서 로컬 파일이 없던 항목의 (끼워 넣을 위치, 원래 웹 주소)를 담아 준다.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[ImageItem] = []
    has_canvas = any(i.kind == "canvas" for i in items)
    if has_canvas:
        set_overlays_hidden(page, True)
    for n, item in enumerate(items, start=1):
        stem = f"{n:04d}"
        result: tuple[bytes, str] | None = None
        if item.kind != "canvas":
            try:
                for src in ([item.original_src] if item.original_src else []) + [item.src]:
                    result = _fetch_any(page, src, retries)
                    if result:
                        break
            except Exception as e:  # noqa: BLE001
                log(f"  [경고] {stem} 다운로드 오류: {e}")

        path: Path | None = None
        if item.kind == "canvas":
            shot = out_dir / f"{stem}.png"
            if _capture_canvas(page, item, shot):
                path = shot
            else:
                log(f"  [건너뜀] {stem} 캔버스 캡처 실패")
        elif result:
            data, ctype = result
            path = out_dir / f"{stem}{ext_from_bytes(data, ctype)}"
            path.write_bytes(data)
        elif item.src.lower().startswith("file://"):
            # 저장한 페이지에서 빠진 이미지(깨진 아이콘)는 스크린샷해도 빈 상자라 건너뛴다
            log(f"  [건너뜀] {stem} 저장된 파일에 없는 이미지 (저장 전 스크롤이 부족했을 수 있음)")
            if missing is not None and item.web_src:
                missing.append((len(saved), item.web_src))
        else:
            shot = out_dir / f"{stem}.png"
            if _screenshot(page, item, shot):
                log(f"  [대체] {stem} 스크린샷으로 저장")
                path = shot
            else:
                log(f"  [건너뜀] {stem} 저장 실패: {item.src[:80]}")

        if path is not None:
            size = _pixel_size(path)
            if size is None:
                log(f"  [건너뜀] {stem} 손상된 이미지")
                path.unlink(missing_ok=True)
            elif min_width and size[0] * 2 < min_width:
                log(f"  [건너뜀] {stem} 실제 폭 {size[0]}px로 너무 작음")
                path.unlink(missing_ok=True)
            elif is_banner_shaped(*size):
                log(f"  [건너뜀] {stem} 배너 모양({size[0]}×{size[1]})")
                path.unlink(missing_ok=True)
            else:
                item.path = path
                saved.append(item)
        if delay:
            time.sleep(delay)
    if has_canvas:
        set_overlays_hidden(page, False)
    return saved
