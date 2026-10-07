"""DOM에서 본문 이미지 후보를 추출하고 필터링한다."""
from __future__ import annotations

from collections import Counter
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Frame, Page

from .models import ImageItem, is_banner_shaped

MODES = ("auto", "all", "webtoon")

# 브라우저(프레임) 안에서 실행: DOM 순서대로 이미지 후보를 수집한다.
COLLECT_JS = r"""
(preferCurrent) => {
  const out = [];
  const seen = new Set();
  const IMG_URL = /\.(jpe?g|png|webp|gif|avif|bmp)(\?.*)?$/i;
  const SVG = /\.svg(\?.*)?$/i;

  function cssPath(el) {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    while (el && el.nodeType === 1 && el !== document.body) {
      let part = el.tagName.toLowerCase();
      const parent = el.parentElement;
      if (parent) {
        const sib = Array.from(parent.children).filter(c => c.tagName === el.tagName);
        if (sib.length > 1) part += ':nth-of-type(' + (sib.indexOf(el) + 1) + ')';
      }
      parts.unshift(part);
      el = parent;
    }
    return 'body > ' + parts.join(' > ');
  }

  function isFixed(el) {
    while (el && el !== document.body) {
      const pos = getComputedStyle(el).position;
      if (pos === 'fixed' || pos === 'sticky') return true;
      el = el.parentElement;
    }
    return false;
  }

  // srcset에서 가장 큰 후보와 그 추정 폭(px). "Nw"는 N, "Nx"는 표시폭×N
  function pickLargest(srcset, renderedW) {
    let best = null, bestW = -1;
    for (const cand of srcset.split(',')) {
      const parts = cand.trim().split(/\s+/);
      const u = parts[0];
      if (!u) continue;
      let w = renderedW;
      if (parts[1]) {
        const n = parseFloat(parts[1]);
        w = /w$/i.test(parts[1]) ? n : Math.round(renderedW * n);
      }
      if (w > bestW) { bestW = w; best = u; }
    }
    return best ? { url: best, w: bestW } : null;
  }

  function linkedOriginal(img) {
    // 썸네일을 감싼 링크가 이미지 파일처럼 보이면 그 URL (실제 이미지인지는 다운로드 때 확인)
    const a = img.closest('a[href]');
    if (!a) return null;
    const h = a.getAttribute('href') || '';
    return IMG_URL.test(h.split('#')[0]) ? h : null;
  }

  function bestSrc(img, renderedW) {
    // 저장한 페이지(file://)는 로컬로 바뀐 currentSrc를 우선. data-src는 웹 주소로 남아 있어 쓰면 안 됨
    if (preferCurrent) {
      const cur = img.currentSrc || img.src || '';
      if (cur && !cur.startsWith('data:image/gif')) return { url: cur, w: 0 };
    }
    const lazy = ['data-src', 'data-original', 'data-lazy-src', 'data-url', 'data-lazy', 'data-echo', 'data-large', 'data-full'];
    for (const attr of lazy) {
      const v = img.getAttribute(attr);
      if (v && !v.startsWith('data:image/gif')) return { url: v, w: 0 };
    }
    let srcset = img.getAttribute('srcset');
    if (!srcset && img.parentElement && img.parentElement.tagName === 'PICTURE') {
      const source = img.parentElement.querySelector('source[srcset]');
      if (source) srcset = source.getAttribute('srcset');
    }
    if (srcset) {
      const best = pickLargest(srcset, renderedW);
      if (best) return best;
    }
    return { url: img.currentSrc || img.src || '', w: 0 };
  }

  function webSrc(img) {
    // 저장한 페이지에서 로컬 파일 대신 남아 있는 원래 웹 주소(빠진 이미지를 다시 받을 때 사용)
    const lazy = ['data-src', 'data-original', 'data-lazy-src', 'data-url', 'data-lazy', 'data-echo', 'data-large', 'data-full'];
    for (const attr of lazy) {
      const v = img.getAttribute(attr);
      if (v && /^https?:/i.test(v)) return v;
    }
    const c = img.currentSrc || img.src || '';
    return /^https?:/i.test(c) ? c : '';
  }

  const push = (el, src, kind, natural, original, web) => {
    if (!src || src.startsWith('data:image/gif') || src.startsWith('data:image/svg')) return;
    if (SVG.test(src.split('#')[0])) return;  // 아이콘·장식용 SVG
    const r = el.getBoundingClientRect();
    const w = Math.round(r.width), h = Math.round(r.height);
    if (w === 0 || h === 0) return;
    if (isFixed(el)) return;
    const top = Math.round(r.top + window.scrollY);
    const left = Math.round(r.left + window.scrollX);
    const key = src + '@' + top;
    if (seen.has(key)) return;
    seen.add(key);
    out.push({ src, width: w, height: h, top, left, selector: cssPath(el), kind,
               natural: natural || 0, original: original || null, web: web || '' });
  };

  for (const img of document.querySelectorAll('img')) {
    const rw = Math.round(img.getBoundingClientRect().width);
    const best = bestSrc(img, rw);
    push(img, best.url, 'img', Math.max(img.naturalWidth || 0, best.w || 0), linkedOriginal(img), webSrc(img));
  }

  // <canvas>에 그림을 그리는 뷰어: 주소가 없으니 자리만 기록하고, 다운로드 단계에서 화면 캡처로 받는다
  let ci = 0;
  for (const cv of document.querySelectorAll('canvas')) {
    ci += 1;
    if (cv.width < 50 || cv.height < 50) continue;
    push(cv, 'canvas:' + ci, 'canvas', cv.width, null, '');
  }

  for (const el of document.querySelectorAll('div, section, span, a, li, figure')) {
    const bg = getComputedStyle(el).backgroundImage;
    if (!bg || bg === 'none') continue;
    const m = bg.match(/url\(["']?([^"')]+)["']?\)/);
    if (m) push(el, m[1], 'background', 0, null, '');
  }
  return out;
}
"""


def _abs(base: str, src: str) -> str:
    return src if src.startswith(("data:", "blob:", "canvas:")) else urljoin(base, src)


def _collect_frame(frame: Frame, base: str, offset_top: int, offset_left: int, frame_url: str) -> list[ImageItem]:
    try:
        raw = frame.evaluate(COLLECT_JS, base.lower().startswith("file://"))
    except Exception:  # noqa: BLE001
        return []
    items: list[ImageItem] = []
    for r in raw:
        items.append(
            ImageItem(
                src=_abs(base, r["src"]),
                width=int(r["width"]),
                height=int(r["height"]),
                top=int(r["top"]) + offset_top,
                left=int(r["left"]) + offset_left,
                selector=r["selector"],
                kind=r["kind"],
                natural_width=int(r.get("natural") or 0),
                original_src=_abs(base, r["original"]) if r.get("original") else "",
                web_src=r.get("web") or "",
                frame_url=frame_url,
            )
        )
    return items


def collect_candidates(page: Page) -> list[ImageItem]:
    """메인 문서와 모든 iframe에서 이미지 후보를 모은다. iframe 요소의 위치를 더해 페이지 좌표로 맞춘다."""
    items = _collect_frame(page.main_frame, page.url, 0, 0, "")
    scroll_y = page.evaluate("window.scrollY") or 0
    scroll_x = page.evaluate("window.scrollX") or 0
    for frame in page.frames:
        if frame is page.main_frame or frame.url in ("", "about:blank"):
            continue
        try:
            box = frame.frame_element().bounding_box()
        except Exception:  # noqa: BLE001
            box = None
        if not box or box["width"] < 50 or box["height"] < 50:
            continue
        items += _collect_frame(frame, frame.url, int(box["y"] + scroll_y), int(box["x"] + scroll_x), frame.url)
    return items


def _similar(a: int, b: int, tol: float = 0.15) -> bool:
    return abs(a - b) <= max(a, b) * tol


def _src_dir(src: str) -> str:
    """호스트를 뺀 경로의 디렉터리. CDN 샤딩(img1., img2.)이 있어도 같은 값이 나온다."""
    path = urlparse(src).path if not src.startswith(("data:", "blob:", "canvas:")) else src[:6]
    return path.rsplit("/", 1)[0]


def passes_size(it: ImageItem, min_width: int) -> bool:
    """크기 필터. 표시 폭이 기준 이상이면 통과. 표시 폭이 기준의 절반 이상이면서
    원본(또는 srcset 추정) 폭이 기준 이상이거나 원본 링크가 있으면 통과(고해상도 썸네일)."""
    if it.width >= min_width:
        return True
    if it.width * 2 >= min_width and (it.natural_width >= min_width or it.original_link):
        return True
    return False


def _is_extra(it: ImageItem, run_width: int, median_h: int, dominant_dir: str) -> bool:
    """본문 구간에 딸린 부속물인가: 본문보다 확연히 좁거나, 다른 경로에서 왔고 높이가 낮다."""
    if it.width < run_width * 0.6:
        return True
    return _src_dir(it.src) != dominant_dir and it.height < median_h * 0.6


def _trim_edges(run: list[ImageItem]) -> list[ImageItem]:
    """구간 양끝에서 본문과 다른 경로에서 왔고 높이도 작은 이미지(연령 안내, 광고 배너 등)를 떼어낸다."""
    if len(run) < 3:
        return run
    heights = sorted(i.height for i in run)
    median = heights[len(heights) // 2]
    dominant = Counter(_src_dir(i.src) for i in run).most_common(1)[0][0]

    def extra(it: ImageItem) -> bool:
        return _src_dir(it.src) != dominant and it.height < median * 0.6

    lo, hi = 0, len(run)
    while hi - lo > 1 and extra(run[hi - 1]):
        hi -= 1
    while hi - lo > 1 and extra(run[lo]):
        lo += 1
    return run[lo:hi]


def _webtoon_run(cands: list[ImageItem]) -> list[ImageItem]:
    """폭이 비슷한 이미지가 세로로 연속되는 구간 중 높이 합이 가장 큰 것."""
    runs: list[list[ImageItem]] = [[cands[0]]]
    for it in cands[1:]:
        if _similar(it.width, runs[-1][-1].width):
            runs[-1].append(it)
        else:
            runs.append([it])
    best = max(runs, key=lambda r: (sum(i.height for i in r), len(r)))
    return _trim_edges(best)


def _looks_like_webtoon(cands: list[ImageItem], run: list[ImageItem]) -> bool:
    """구간이 2장 이상이고, 구간 밖 이미지가 모두 부속물로 설명되면 웹툰형."""
    if len(run) < 2:
        return False
    run_ids = {id(i) for i in run}
    run_width = max(i.width for i in run)
    heights = sorted(i.height for i in run)
    median_h = heights[len(heights) // 2]
    dominant = Counter(_src_dir(i.src) for i in run).most_common(1)[0][0]
    return all(_is_extra(i, run_width, median_h, dominant) for i in cands if id(i) not in run_ids)


def select_body_images(items: list[ImageItem], min_width: int = 300, mode: str = "auto") -> list[ImageItem]:
    """필터링 + 본문 판정.

    공통: 크기 필터(passes_size), 동일 URL 중복 제거, 세로 위치순 정렬.
    - all: 남은 이미지 전부
    - webtoon: 폭이 비슷한 이미지가 연속되는 구간만 (양끝 부속물 제거)
    - auto: 구간 밖 이미지가 전부 부속물(더 좁거나, 다른 경로의 낮은 이미지)이면 webtoon, 아니면 all
    """
    if mode not in MODES:
        raise ValueError(f"mode는 {MODES} 중 하나여야 합니다: {mode}")
    seen: set[str] = set()
    cands: list[ImageItem] = []
    for it in sorted(items, key=lambda i: (i.top, i.left)):
        if not passes_size(it, min_width) or it.src in seen:
            continue
        if is_banner_shaped(it.width, it.height):
            continue
        seen.add(it.src)
        cands.append(it)

    if mode == "all" or len(cands) <= 2:
        return cands

    run = _webtoon_run(cands)
    if mode == "webtoon":
        return run
    return run if _looks_like_webtoon(cands, run) else cands


def extract_images(page: Page, min_width: int = 300, mode: str = "auto") -> list[ImageItem]:
    return select_body_images(collect_candidates(page), min_width=min_width, mode=mode)
