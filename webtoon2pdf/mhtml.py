"""크롬 '웹페이지, 단일 파일(.mhtml)' 저장본에서 이미지를 꺼낸다. 브라우저 없이 동작."""
from __future__ import annotations

import email
import email.policy
import html
import re
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path
from typing import Callable

from PIL import Image

from .downloader import ext_from_bytes
from .models import Chapter, ImageItem, is_banner_shaped

_SRC_RE = re.compile(r"""<img\b[^>]*?\ssrc\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def _decode_text(part: Message) -> str:
    raw = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _title(msg: Message, html_parts: list[str]) -> str:
    subj = msg.get("Subject")
    if subj:
        try:
            return str(make_header(decode_header(subj)))
        except Exception:  # noqa: BLE001
            pass
    for h in html_parts:
        m = re.search(r"<title[^>]*>(.*?)</title>", h, re.IGNORECASE | re.DOTALL)
        if m:
            return html.unescape(m.group(1)).strip()
    return "저장한 페이지"


def _keys(part: Message) -> list[str]:
    keys = []
    loc = part.get("Content-Location")
    if loc:
        keys.append(loc.strip())
    cid = part.get("Content-ID")
    if cid:
        keys.append("cid:" + cid.strip().strip("<>"))
    return keys


def chapter_from_mhtml(path: Path, out_dir: Path, min_width: int = 300,
                       log: Callable[[str], None] = print) -> Chapter:
    """HTML 파트에 <img>가 나오는 순서대로 이미지 파트를 저장해 Chapter로 돌려준다."""
    raw = path.read_bytes().replace(b"\r\r\n", b"\r\n")  # 텍스트 모드로 재저장된 파일의 줄바꿈 보정
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    if not msg.is_multipart():
        raise RuntimeError("MHTML 형식을 읽지 못했습니다. 크롬에서 '웹페이지, 단일 파일'로 저장한 파일인지 확인해 주세요.")
    parts = [p for p in msg.walk() if not p.is_multipart()]
    images: dict[str, Message] = {}
    html_parts: list[str] = []
    for p in parts:
        ctype = (p.get_content_type() or "").lower()
        if ctype == "text/html":
            html_parts.append(_decode_text(p))
        elif ctype.startswith("image/"):
            for k in _keys(p):
                images.setdefault(k, p)

    out_dir.mkdir(parents=True, exist_ok=True)
    page_url = (msg.get("Snapshot-Content-Location") or "").strip()
    if not page_url:
        for p in parts:
            if (p.get_content_type() or "").lower() == "text/html" and p.get("Content-Location"):
                page_url = p.get("Content-Location").strip()
                break
    ch = Chapter(index=1, url=path.resolve().as_uri(), title=_title(msg, html_parts), dir=out_dir, page_url=page_url)
    seen: set[str] = set()
    absent: list[tuple[int, str]] = []  # (그 시점까지의 본문 이미지 수 = 끼워 넣을 위치, src)
    n = 0
    for h in html_parts:
        for m in _SRC_RE.finditer(h):
            src = html.unescape(m.group(1)).strip()
            if src in seen:
                continue
            seen.add(src)
            part = images.get(src)
            if part is None:
                if src.lower().startswith(("http://", "https://")):
                    absent.append((len(ch.images), src))
                continue
            data = part.get_payload(decode=True) or b""
            ext = ext_from_bytes(data, part.get_content_type())
            if not ext:
                continue
            n += 1
            f = out_dir / f"{n:04d}{ext}"
            f.write_bytes(data)
            try:
                with Image.open(f) as im:
                    w, h = im.size
            except Exception:  # noqa: BLE001
                f.unlink(missing_ok=True)
                continue
            if w * 2 < min_width or is_banner_shaped(w, h):
                f.unlink(missing_ok=True)
                continue
            ch.images.append(ImageItem(src=src, width=w, height=0, top=n, left=0, selector="", path=f))

    # 파일에 없는 참조 중 '본문 이미지 폴더'와 같은 폴더의 주소만 빠진 본문으로 본다 (광고·아이콘 제외)
    if absent and ch.images:
        from collections import Counter
        from .extractor import _src_dir
        dominant = Counter(_src_dir(i.src) for i in ch.images).most_common(1)[0][0]
        ch.missing = [(pos, src) for pos, src in absent if _src_dir(src) == dominant]
    log(f"  MHTML 안 이미지 {len(images)}개 중 본문 {len(ch.images)}장 선택"
        + (f", 빠진 본문 이미지 {len(ch.missing)}장 (원본 주소는 남아 있음)" if ch.missing else ""))
    return ch
