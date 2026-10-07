"""'다음 화' 링크 탐지."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Page, TimeoutError as PWTimeout

NEXT_TEXTS = (
    "다음화", "다음 화", "다음편", "다음 편", "다음",
    "next", "next chapter", "next episode", "→", ">", "»", "▶",
)

FIND_NEXT_JS = r"""
(args) => {
  const [texts, userSelector] = args;
  const norm = s => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const visible = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const hrefOf = el => {
    if (!el) return null;
    const h = el.getAttribute('href') || el.getAttribute('data-href') || el.getAttribute('data-url');
    if (h && h !== '#' && !h.startsWith('javascript:')) return h;
    // onclick="location.href='...'" 형태의 버튼 (그누보드 뷰어 등)
    const oc = el.getAttribute('onclick') || '';
    const m = oc.match(/(?:location(?:\.href)?|window\.location(?:\.href)?)\s*=\s*['"]([^'"]+)['"]/);
    if (m) return m[1];
    return null;
  };
  const label = el => norm(el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title'));
  const disabled = el => el.disabled || el.getAttribute('aria-disabled') === 'true' || /disabled|inactive/i.test(String(el.className));
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

  // (d) 사용자 지정 selector가 있으면 그것만 사용 (href 없으면 클릭 대상으로)
  if (userSelector) {
    for (const el of document.querySelectorAll(userSelector)) {
      const h = hrefOf(el) || hrefOf(el.closest('a'));
      if (h) return { href: h, how: 'selector' };
      if (visible(el) && !disabled(el)) return { click: cssPath(el), how: 'selector-click' };
    }
    return null;
  }
  // (a) rel=next
  for (const el of document.querySelectorAll('link[rel~="next"], a[rel~="next"]')) {
    const h = hrefOf(el);
    if (h) return { href: h, how: 'rel' };
  }
  // (b) 텍스트 매칭: 정확히 일치 → 짧은 라벨에 포함
  const anchors = Array.from(document.querySelectorAll('a[href], button')).filter(visible);
  for (const t of texts) {
    const tn = norm(t);
    for (const el of anchors) {
      if (label(el) === tn) {
        const h = hrefOf(el) || hrefOf(el.closest('a'));
        if (h) return { href: h, how: 'text' };
      }
    }
  }
  for (const t of texts) {
    const tn = norm(t);
    if (tn.length < 2) continue;
    for (const el of anchors) {
      const l = label(el);
      if (l.length <= 12 && l.includes(tn) && !l.includes('이전') && !l.includes('prev')) {
        const h = hrefOf(el) || hrefOf(el.closest('a'));
        if (h) return { href: h, how: 'text' };
      }
    }
  }
  // (c) class/id에 next
  for (const el of document.querySelectorAll('a[href]')) {
    const key = ((typeof el.className === 'string' ? el.className : '') + ' ' + (el.id || '')).toLowerCase();
    if (/\bnext\b|next[-_]|[-_]next|btn_next|nextepisode/.test(key) && !/prev/.test(key)) {
      const h = hrefOf(el);
      if (h) return { href: h, how: 'class' };
    }
  }
  // (e) href 없는 버튼: JS로 이동하는 사이트. 텍스트가 정확히 일치하는 것을 클릭 후보로
  const clickables = Array.from(document.querySelectorAll('button, [role="button"], a:not([href]), a[href="#"], a[href^="javascript:"]'))
    .filter(el => visible(el) && !disabled(el));
  for (const t of texts) {
    const tn = norm(t);
    for (const el of clickables) {
      if (label(el) === tn) return { click: cssPath(el), how: 'click-text' };
    }
  }
  for (const el of clickables) {
    const key = ((typeof el.className === 'string' ? el.className : '') + ' ' + (el.id || '')).toLowerCase();
    if (/\bnext\b|next[-_]|[-_]next|btn_next|nextepisode/.test(key) && !/prev/.test(key)) {
      return { click: cssPath(el), how: 'click-class' };
    }
  }
  return null;
}
"""


def _strip_sub(host: str) -> str:
    return re.sub(r"^(www|m|comic|mobile)\.", "", host)


def same_site(a: str, b: str) -> bool:
    ha, hb = urlparse(a).hostname or "", urlparse(b).hostname or ""
    return _strip_sub(ha) == _strip_sub(hb)


def _accept(current_url: str, candidate: str) -> str | None:
    href = urljoin(current_url, candidate).split("#", 1)[0]
    if href == current_url.split("#", 1)[0]:
        return None
    if not same_site(current_url, href):
        return None
    return href


def _click_and_get_url(page: Page, selector: str, current_url: str, timeout_ms: int = 8000) -> str | None:
    """href 없는 버튼을 클릭해 URL이 바뀌면 그 URL을 돌려준다. (페이지는 이미 이동한 상태)"""
    base = current_url.split("#", 1)[0]
    try:
        loc = page.locator(selector).first
        loc.scroll_into_view_if_needed(timeout=3000)
        loc.click(timeout=3000)
        page.wait_for_url(lambda u: u.split("#", 1)[0] != base, timeout=timeout_ms)
    except PWTimeout:
        return None
    except Exception:  # noqa: BLE001
        return None
    return _accept(current_url, page.url)


def find_next_url(page: Page, current_url: str, selector: str | None = None) -> str | None:
    """다음 화 URL을 찾는다. 링크가 없고 버튼만 있으면 클릭해서 이동한 URL을 돌려준다."""
    res = page.evaluate(FIND_NEXT_JS, [list(NEXT_TEXTS), selector])
    if not res:
        return None
    if "href" in res:
        return _accept(current_url, res["href"])
    return _click_and_get_url(page, res["click"], current_url)
