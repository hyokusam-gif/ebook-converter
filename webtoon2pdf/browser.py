"""Playwright 세션 관리, 페이지 로드, 끝까지 스크롤."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PWError,
    Page,
    TimeoutError as PWTimeout,
    sync_playwright,
)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1280, "height": 900}
# 설치된 크롬으로 실행할 때 자동화 표식(navigator.webdriver)을 끄는 플래그
CHROME_ARGS = ["--disable-blink-features=AutomationControlled"]
FALLBACK_NOTE = "설치된 크롬을 찾지 못해 Playwright 내장 Chromium으로 실행합니다."


def _launch_browser(p, headed: bool, log: Callable[[str], None] | None = None) -> tuple[Browser, bool]:
    """PC에 설치된 크롬(channel='chrome')을 우선 띄우고, 없으면 내장 Chromium.

    환경변수 WEBTOON2PDF_BROWSER=chromium 이면 내장 Chromium을 강제한다.
    (browser, 설치된 크롬 여부)를 돌려준다.
    """
    if os.environ.get("WEBTOON2PDF_BROWSER", "").lower() != "chromium":
        try:
            return p.chromium.launch(channel="chrome", headless=not headed, args=CHROME_ARGS), True
        except PWError as e:
            if log:
                log(f"  [yellow]{FALLBACK_NOTE}[/yellow] ({str(e).splitlines()[0]})")
    try:
        return p.chromium.launch(headless=not headed), False
    except PWError as e:
        # 설치 없는 exe 배포판에는 내장 Chromium이 없다. 크롬을 쓰라고 안내한다.
        raise RuntimeError(
            "주소 입력 기능은 크롬이 필요합니다. 크롬을 설치한 뒤 다시 시도하세요. "
            "(저장한 파일이나 폴더 변환은 크롬 없이도 됩니다.)"
        ) from e


def _context_kwargs(browser: Browser, real_chrome: bool, headed: bool) -> dict:
    """컨텍스트 옵션. 설치된 크롬이 창을 띄운 경우엔 실제 UA를 그대로 두고,
    헤드리스면 'HeadlessChrome' 표식만 실제 버전에 맞춘 일반 UA로 바꾼다."""
    kwargs: dict = {"viewport": VIEWPORT, "locale": "ko-KR"}
    if not real_chrome:
        kwargs["user_agent"] = DESKTOP_UA
    elif not headed:
        major = (browser.version or "").split(".")[0] or "126"
        kwargs["user_agent"] = DESKTOP_UA.replace("Chrome/126.0.0.0", f"Chrome/{major}.0.0.0")
    return kwargs


@contextmanager
def launch(
    headed: bool = False, cookies: Path | None = None, log: Callable[[str], None] | None = None
) -> Iterator[tuple[Browser, BrowserContext, Page]]:
    """설치된 크롬(없으면 Chromium)을 띄우고 (browser, context, page)를 넘겨준다."""
    with sync_playwright() as p:
        browser, real_chrome = _launch_browser(p, headed, log)
        kwargs = _context_kwargs(browser, real_chrome, headed)
        if cookies is not None and Path(cookies).exists():
            kwargs["storage_state"] = str(cookies)
        context = browser.new_context(**kwargs)
        page = context.new_page()
        try:
            yield browser, context, page
        finally:
            context.close()
            browser.close()


def load(page: Page, url: str, timeout_ms: int = 30000) -> None:
    """페이지를 열고 네트워크가 잠잠해질 때까지 잠시 기다린다."""
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    try:
        page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 10000))
    except PWTimeout:
        pass  # 광고 등으로 networkidle이 안 오는 사이트는 그냥 진행


def scroll_to_bottom(page: Page, step_wait: float = 0.4, max_steps: int = 400) -> int:
    """문서 높이가 더 이상 늘지 않을 때까지 뷰포트 단위로 스크롤한다. 최종 문서 높이를 돌려준다."""
    last_height = -1
    stable = 0
    for _ in range(max_steps):
        height = page.evaluate("document.documentElement.scrollHeight")
        y = page.evaluate("window.scrollY + window.innerHeight")
        if y >= height and height == last_height:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0
        last_height = height
        page.evaluate("window.scrollBy(0, Math.max(300, window.innerHeight * 0.8))")
        time.sleep(step_wait)
    page.evaluate("window.scrollTo(0, 0)")
    time.sleep(step_wait)
    return int(last_height)


def wait_images(page: Page, timeout_s: float = 20.0) -> None:
    """모든 <img>가 로드(또는 실패)될 때까지 기다린다."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pending = page.evaluate(
            "Array.from(document.images).filter(i => i.src && !i.complete).length"
        )
        if pending == 0:
            return
        time.sleep(0.3)


CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "보안 확인")
CHALLENGE_TEXTS = ("checking your browser", "verify you are human", "보안 확인", "봇이 아님")
CHALLENGE_FRAME_HINTS = ("challenges.cloudflare.com", "/cdn-cgi/challenge-platform/")


def is_challenge_page(page: Page) -> str | None:
    """봇 차단(Cloudflare 등) 확인 페이지면 이유 문자열, 아니면 None."""
    try:
        title = (page.title() or "").lower()
        for t in CHALLENGE_TITLES:
            if t in title:
                return f"페이지 제목이 '{page.title()}'"
        for f in page.frames:
            if any(h in f.url for h in CHALLENGE_FRAME_HINTS):
                return "보안 확인 프레임(challenge-platform) 존재"
        text = (page.evaluate("(document.body && document.body.innerText || '').slice(0, 3000)") or "").lower()
        hits = [t for t in CHALLENGE_TEXTS if t in text]
        if hits and page.evaluate("document.images.length") < 3:
            return f"본문에 '{hits[0]}' 문구"
    except Exception:  # noqa: BLE001
        return "페이지 전환 중"  # 이동 중이라 판정 불가 → 아직 통과로 보지 않는다
    return None


def wait_for_challenge_clear(page: Page, timeout_s: float = 120.0, poll: float = 0.5) -> bool:
    """사용자가 보안 확인을 직접 통과할 때까지 기다린다. 통과되면 True, 시간 초과면 False.

    이동 직후의 순간적인 판정 오류를 피하려고, 연속 두 번 확인 페이지가 아니어야 통과로 본다.
    """
    deadline = time.time() + timeout_s
    clean = 0
    while time.time() < deadline:
        if is_challenge_page(page) is None:
            clean += 1
            if clean >= 2:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
                except PWTimeout:
                    pass
                return True
        else:
            clean = 0
        time.sleep(poll)
    return False


def _console_wait() -> None:
    print("브라우저에서 로그인을 마친 뒤 이 창으로 돌아와 Enter를 누르세요...")
    input()


def save_login_state(url: str, out_path: Path, wait: Callable[[], None] = _console_wait) -> None:
    """화면 있는 브라우저를 띄워 사용자가 직접 로그인하게 한 뒤, wait()가 끝나면 상태를 저장한다."""
    with sync_playwright() as p:
        browser, real_chrome = _launch_browser(p, headed=True)
        context = browser.new_context(**_context_kwargs(browser, real_chrome, headed=True))
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded")
        wait()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(out_path))
        browser.close()
