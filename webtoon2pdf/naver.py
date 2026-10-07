"""네이버 블로그 카테고리에서 만화 글을 순서대로 모아 권별로 묶는다.

브라우저 없이 네이버가 공개로 제공하는 목록·본문 요청만 사용한다(로그인 불필요).
- 카테고리 글 목록: PostTitleListAsync.naver  (logNo, 제목, 날짜)
- 본문 이미지: PostView.naver 안의 data-lazy-src (postfiles.pstatic.net)

순서 규칙: 이 블로그류는 마지막 권/마지막 회차부터 역순으로 올린다.
즉 logNo(글 번호)가 클수록 이야기 순서상 앞이다. → 권 안에서는 logNo 내림차순이 읽는 순서.
권끼리는 제목의 'N권' 숫자 오름차순.
"""
from __future__ import annotations

import gzip
import html
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from PIL import Image

from .models import Chapter, ImageItem, is_banner_shaped

Log = Callable[[str], None]

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_REFERER = "https://blog.naver.com/"
_VOL_RE = re.compile(r"(?<!\d)(\d{1,3})\s*권")
# 본문 이미지: data-lazy-src(우선) 또는 src 에 실린 postfiles 원본
_IMG_RE = re.compile(r'data-lazy-src="(https://postfiles\.pstatic\.net/[^"]+)"')
# 제목 안에 흔히 들어가는 작품명(따옴표·괄호로 감싼 부분) 추출용
_QUOTED_RE = re.compile(r"[\"'「」『』<>〈〉【】\[\]]([^\"'「」『』<>〈〉【】\[\]]{4,40})[\"'「」『』<>〈〉【】\[\]]")


class NoImages(RuntimeError):
    """본문에서 이미지를 하나도 찾지 못했을 때(브라우저 폴백 신호)."""


@dataclass
class PostRef:
    log_no: str
    title: str
    add_date: str = ""

    @property
    def view_url(self) -> str:
        return f"https://blog.naver.com/PostView.naver?blogId={self._blog_id}&logNo={self.log_no}"

    _blog_id: str = field(default="", repr=False)


# ---------- HTTP ----------

def _get(url: str, referer: str = _REFERER, binary: bool = False, retries: int = 3, timeout: int = 20) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": _UA,
                "Referer": referer,
                "Accept": ("image/avif,image/webp,image/*,*/*;q=0.8" if binary
                           else "text/html,application/json,*/*;q=0.8"),
                "Accept-Language": "ko,en;q=0.8",
            })
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    data = gzip.decompress(data)
                return data
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"요청 실패: {url} ({last})")


def _get_text(url: str, referer: str = _REFERER) -> str:
    return _get(url, referer=referer).decode("utf-8", "replace")


# ---------- 카테고리 / 목록 ----------

def _parse_category_url(url_or_id: str) -> tuple[str, str, str]:
    """카테고리 주소에서 (blogId, categoryNo, "")를 순수하게 파싱한다(네트워크 없음).

    지원 형태:
      https://blog.naver.com/<id>?categoryNo=83
      https://blog.naver.com/PostList.naver?blogId=<id>&categoryNo=83
      https://m.blog.naver.com/<id>?categoryNo=83
    """
    u = urlparse(url_or_id.strip().strip('"'))
    q = parse_qs(u.query)
    blog_id = (q.get("blogId", [""])[0] or "").strip()
    if not blog_id:
        seg = [s for s in u.path.split("/") if s]
        # 첫 세그먼트가 blogId (PostList.naver 같은 파일명은 제외)
        if seg and not seg[0].endswith(".naver"):
            blog_id = seg[0]
    category_no = (q.get("categoryNo", [""])[0] or "").strip()
    if not blog_id:
        raise RuntimeError("블로그 주소에서 블로그 아이디를 찾지 못했습니다. 카테고리 주소를 확인해 주세요.")
    if not category_no:
        raise RuntimeError("카테고리 번호(categoryNo)가 주소에 없습니다. 블로그에서 원하는 카테고리를 연 주소를 넣어 주세요.")
    return blog_id, category_no, ""


def resolve_category(url_or_id: str, log: Log = print) -> tuple[str, str, str]:
    """카테고리 주소에서 (blogId, categoryNo, 카테고리이름)을 얻는다."""
    blog_id, category_no, _ = _parse_category_url(url_or_id)
    name = _category_name(blog_id, category_no)
    return blog_id, category_no, name


def _category_name(blog_id: str, category_no: str) -> str:
    """모바일 카테고리 API로 카테고리 이름을 찾는다(실패해도 빈 문자열)."""
    try:
        raw = _get_text(f"https://m.blog.naver.com/api/blogs/{blog_id}/category-list",
                        referer=f"https://m.blog.naver.com/{blog_id}")
        d = json.loads(raw)
        for c in d.get("result", {}).get("mylogCategoryList", []):
            if str(c.get("categoryNo")) == str(category_no):
                return (c.get("categoryName") or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


_LIST_ITEM_RE = re.compile(
    r'"logNo":"(\d+)".*?"title":"(.*?)","categoryNo".*?"addDate":"(.*?)"', re.S)


def list_posts(blog_id: str, category_no: str, log: Log = print, max_pages: int = 60) -> list[PostRef]:
    """카테고리의 모든 글을 (중복 제거) 가져온다. 페이지당 몇 개든 빈 페이지가 나올 때까지 넘긴다."""
    posts: list[PostRef] = []
    seen: set[str] = set()
    for page in range(1, max_pages + 1):
        url = (f"https://blog.naver.com/PostTitleListAsync.naver?blogId={blog_id}"
               f"&currentPage={page}&categoryNo={category_no}&countPerPage=30")
        raw = _get_text(url, referer=f"https://blog.naver.com/{blog_id}")
        found = 0
        for log_no, enc_title, add_date in _LIST_ITEM_RE.findall(raw):
            if log_no in seen:
                continue
            seen.add(log_no)
            title = html.unescape(unquote(enc_title.replace("+", " "))).strip()
            posts.append(PostRef(log_no=log_no, title=title, add_date=add_date, _blog_id=blog_id))
            found += 1
        if found == 0:
            break
    log(f"카테고리 글 {len(posts)}개를 찾았습니다.")
    return posts


def parse_volume(title: str) -> int | None:
    """제목에서 'N권'의 N. 없으면 None(소개·공지글 등)."""
    m = _VOL_RE.search(title)
    if m and 1 <= int(m.group(1)) <= 300:
        return int(m.group(1))
    return None


def build_reading_order(posts: list[PostRef], log: Log = print) -> dict[int, list[PostRef]]:
    """권별로 묶고 각 권 안에서 logNo 내림차순(=이야기 순서)으로 정렬. 권 없는 글은 제외."""
    by_vol: dict[int, list[PostRef]] = {}
    skipped: list[PostRef] = []
    for p in posts:
        v = parse_volume(p.title)
        if v is None:
            skipped.append(p)
            continue
        by_vol.setdefault(v, []).append(p)
    for v in by_vol:
        by_vol[v].sort(key=lambda p: int(p.log_no), reverse=True)
    if skipped:
        log(f"'권' 표시가 없는 글 {len(skipped)}개는 제외합니다 (소개·공지글):")
        for p in skipped:
            log(f"   - {p.add_date} {p.title[:45]}")
    ordered = {v: by_vol[v] for v in sorted(by_vol)}
    log("권별 회차 수: " + ", ".join(f"{v}권 {len(g)}화" for v, g in ordered.items()))
    return ordered


def series_from_titles(titles: list[str]) -> str:
    """제목들에 가장 자주 등장하는 따옴표·괄호로 감싼 작품명(예: 만화로 보는 그리스 로마 신화)."""
    cnt: Counter[str] = Counter()
    for t in titles:
        for m in _QUOTED_RE.findall(t):
            key = re.sub(r"\s+", " ", m).strip()
            if len(key) >= 4 and "권" not in key:
                cnt[key] += 1
    if not cnt:
        return ""
    return cnt.most_common(1)[0][0]


# ---------- 본문 이미지 ----------

def post_image_urls(blog_id: str, log_no: str) -> list[str]:
    """PostView 본문의 만화 이미지 URL들을 문서 순서대로(중복 제거)."""
    url = f"https://blog.naver.com/PostView.naver?blogId={blog_id}&logNo={log_no}"
    text = _get_text(url, referer=f"https://blog.naver.com/{blog_id}")
    urls: list[str] = []
    seen: set[str] = set()
    for img in _IMG_RE.findall(text):
        if img not in seen:
            seen.add(img)
            urls.append(img)
    return urls


def _ext_from(data: bytes, url: str) -> str:
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)", url.lower())
    return "." + (m.group(1).replace("jpeg", "jpg") if m else "jpg")


def download_post(blog_id: str, post: PostRef, out_dir: Path, min_width: int = 300,
                  delay: float = 0.2, log: Log = print) -> Chapter:
    """글 하나의 이미지를 순서대로 내려받아 Chapter로. 이미지가 없으면 NoImages."""
    img_urls = post_image_urls(blog_id, post.log_no)
    if not img_urls:
        raise NoImages(post.log_no)
    out_dir.mkdir(parents=True, exist_ok=True)
    ch = Chapter(index=1, url=post.view_url, title=post.title, dir=out_dir, page_url=post.view_url)
    saved = 0
    skipped = 0
    for i, src in enumerate(img_urls, start=1):
        try:
            data = _get(src, referer=post.view_url, binary=True)
        except Exception as e:  # noqa: BLE001
            log(f"    이미지 {i} 실패: {e}")
            skipped += 1
            continue
        path = out_dir / f"{i:04d}{_ext_from(data, src)}"
        path.write_bytes(data)
        try:
            with Image.open(path) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001
            path.unlink(missing_ok=True)
            skipped += 1
            continue
        if w * 2 < min_width or is_banner_shaped(w, h):  # 작은 아이콘·구분선 제외(표시 폭 기준)
            path.unlink(missing_ok=True)
            skipped += 1
            continue
        ch.images.append(ImageItem(src=src, width=w, height=h, top=i, left=0, selector="", path=path))
        saved += 1
        if delay:
            time.sleep(delay)
    if not ch.images:
        raise NoImages(post.log_no)
    log(f"    이미지 {saved}장 저장" + (f" ({skipped}개 제외)" if skipped else ""))
    return ch


# ---------- 쪽번호 검증(선택, best-effort) ----------

_PAGE_NUM_RE = re.compile(r"(?<!\d)(\d{1,3})(?!\d)")


def verify_page_numbers(chapters: list[Chapter], log: Log = print) -> None:
    """만화에 인쇄된 쪽번호를 읽어 순서·누락을 교차검증한다(경고만; 자동 재정렬 안 함).

    윈도우 OCR(winocr)이 없으면 조용히 건너뛴다. 각 이미지 하단 띠만 읽어 속도를 아낀다.
    """
    try:
        import winocr  # type: ignore  # noqa: F401
    except Exception:  # noqa: BLE001
        return
    total = sum(len(ch.image_paths) for ch in chapters)
    nums: list[int] = []  # 이미지 순서대로 읽힌 쪽번호(읽힌 것만)
    for ch in chapters:
        for p in ch.image_paths:
            n = _ocr_page_number(p)
            if n is not None:
                nums.append(n)
    read = len(nums)
    if read < 8:
        log("쪽번호를 또렷하게 읽지 못해 순서 검증은 생략합니다(글 순서 그대로 사용).")
        return
    # 신뢰도 확인: 진짜 쪽번호라면 (1) 이미지 수를 크게 넘지 않고 (2) 대체로 커지는 방향이어야 한다.
    up = sum(1 for a, b in zip(nums, nums[1:]) if b >= a)
    frac_up = up / (read - 1)
    if max(nums) > total * 1.5 or frac_up < 0.6:
        log("쪽번호가 또렷하지 않아(스캔본에 인쇄된 번호가 없거나 흐림) 순서 검증은 생략합니다. "
            "순서는 글 올린 순서(역순 규칙)로 이미 맞춰져 있습니다.")
        return
    # 신뢰할 만할 때만 누락 의심 구간을 알린다
    gaps = [(a, b) for a, b in zip(nums, nums[1:]) if 1 < (b - a) <= 10]
    span = f"{min(nums)}~{max(nums)}쪽"
    if gaps:
        log(f"[yellow]쪽번호 검증: 읽은 쪽 {span}. 사이가 비는 곳 {len(gaps)}군데 — 빠진 페이지가 있을 수 있어요:[/yellow]")
        for a, b in gaps[:8]:
            log(f"   {a}쪽 다음이 {b}쪽")
    else:
        log(f"쪽번호 검증: 읽은 쪽 {span}, 큰 누락 없음.")


def _ocr_page_number(path: Path) -> int | None:
    try:
        import winocr  # type: ignore
        with Image.open(path) as im:
            im = im.convert("RGB")
            strip = im.crop((0, int(im.height * 0.85), im.width, im.height))  # 하단 15%
            if strip.width < 200:
                strip = strip.resize((strip.width * 2, strip.height * 2), Image.LANCZOS)
            r = winocr.recognize_pil_sync(strip, "en")
            text = r.get("text", "") if isinstance(r, dict) else getattr(r, "text", "")
        m = _PAGE_NUM_RE.search(text or "")
        if m and 1 <= int(m.group(1)) <= 999:
            return int(m.group(1))
    except Exception:  # noqa: BLE001
        return None
    return None
