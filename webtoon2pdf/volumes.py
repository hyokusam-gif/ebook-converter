"""여러 회차를 권으로 묶는 로직: 표지 판별, 표지 OCR로 권 번호 읽기, 중복·미완성 회차 처리, 이름 짓기."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from PIL import Image

from .models import Chapter

Log = Callable[[str], None]


@dataclass
class VolumeInfo:
    """권 분석 보고의 한 권. status: '완결' | '미확정' | '개수묶기'."""
    number: int
    first_ep: str
    last_ep: str
    chapter_count: int
    status: str


@dataclass
class VolumeReport:
    """모아온(또는 폴더에 쌓인) 회차들을 변환 전에 분석한 결과.

    covers_detected=False면 표지를 못 찾아 권을 자동으로 나눌 수 없는 소스(예: '미리보기').
    이때 volumes는 count_per_volume개씩 임시로 묶은 것이며 권 번호는 추정값이다.
    """
    series: str
    covers_detected: bool
    volumes: list[VolumeInfo] = field(default_factory=list)
    missing_episodes: list[int] = field(default_factory=list)
    incomplete_labels: list[str] = field(default_factory=list)
    total_chapters: int = 0
    count_per_volume: int = 7

_VOL_RE = re.compile(r"(?<!\d)(\d{1,3})\s*권")
# OCR이 '권'을 못 읽어도 '제 23' 꼴은 남는다. '제'는 碍·저·계·째 등으로 깨지기도 한다.
_VOL_RE_LOOSE = re.compile(r"[제碍저계째]\s*(\d{1,3})(?!\d)")


def _normalize_ocr(text: str) -> str:
    """OCR 오독 보정: 숫자 0이 '()'나 'O'로 읽히는 경우('碍1()췋' → '碍10췋', '碍2())d' → '碍20)d')."""
    text = text.replace("()", "0")
    return re.sub(r"(?<=\d)[Oo]|(?<=[제碍저계째])\s*[Oo](?=\d)", "0", text)
_EP_RE = re.compile(r"(\d{1,4})\s*(화|話|회)")


def file_hash(p: Path) -> str:
    return hashlib.md5(p.read_bytes()).hexdigest()


def chapter_hash(ch: Chapter) -> str:
    return hashlib.md5(b"".join(file_hash(p).encode() for p in ch.image_paths)).hexdigest()


# ---------- 표지 OCR ----------

def _ocr_text(img: Image.Image) -> str:
    """윈도우 내장 OCR(한국어)로 글자를 읽는다. 사용 불가 환경이면 빈 문자열."""
    try:
        import winocr  # type: ignore
    except Exception:  # noqa: BLE001
        return ""
    for lang in ("ko", "ja", "en"):
        try:
            r = winocr.recognize_pil_sync(img, lang)
            text = _normalize_ocr(r.get("text", "") if isinstance(r, dict) else getattr(r, "text", ""))
            if text and (_VOL_RE.search(text) or _VOL_RE_LOOSE.search(text)):
                return text
        except Exception:  # noqa: BLE001
            continue
    return ""


def _volume_in_text(text: str) -> int | None:
    for rx in (_VOL_RE, _VOL_RE_LOOSE):
        m = rx.search(text)
        if m and 1 <= int(m.group(1)) <= 300:
            return int(m.group(1))
    return None


def ocr_volume_number(path: Path) -> int | None:
    """표지 이미지에서 'N권'(또는 '제 N')을 읽는다. 원본 → 2배 확대 순으로 시도. 못 읽으면 None."""
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            for scale in (1, 2):
                probe = im if scale == 1 else im.resize((im.width * 2, im.height * 2), Image.LANCZOS)
                n = _volume_in_text(_ocr_text(probe))
                if n is not None:
                    return n
    except Exception:  # noqa: BLE001
        return None
    return None


# ---------- 제목 정리 ----------

def common_suffix(titles: list[str]) -> str:
    """모든 제목에 공통으로 붙은 뒷부분(예: ' - 마나토끼 미리보기'). 구분 문자(- | : ·) 경계에서만 인정."""
    if len(titles) < 2:
        return ""
    suffix = titles[0]
    for t in titles[1:]:
        i = 0
        while i < min(len(suffix), len(t)) and suffix[-1 - i] == t[-1 - i]:
            i += 1
        suffix = suffix[len(suffix) - i:]
    m = re.search(r"\s*[-|:·~]\s*[^-|:·~]*$", suffix)
    return m.group(0) if m else ""


def strip_common_suffix(chapters: list[Chapter], log: Log) -> None:
    suf = common_suffix([c.title for c in chapters])
    if suf and len(suf.strip()) >= 3:
        for c in chapters:
            if c.title.endswith(suf):
                c.title = c.title[: -len(suf)].strip()
        log(f"제목 공통 꼬리표 제거: '{suf.strip()}'")


def series_name(chapters: list[Chapter]) -> str:
    """회차 제목들의 공통 앞부분(예: '나루토 1화', '나루토 2화' → '나루토')."""
    titles = [c.title for c in chapters if c.title]
    if not titles:
        return ""
    prefix = titles[0]
    for t in titles[1:]:
        i = 0
        while i < min(len(prefix), len(t)) and prefix[i] == t[i]:
            i += 1
        prefix = prefix[:i]
    prefix = re.sub(r"[\s\d제_\-~(\[]+$", "", prefix).strip()
    return prefix


def episode_label(title: str) -> str:
    """'나루토 100화 - …' → '100화'. 못 찾으면 제목 앞 20자."""
    m = _EP_RE.search(title)
    return f"{m.group(1)}화" if m else title.strip()[:20]


# ---------- 회차 정리 ----------

def dedupe_chapters(chapters: list[Chapter], log: Log) -> list[Chapter]:
    """같은 제목이면서 이미지 내용까지 같은 회차는 하나만 남긴다(연속 저장 중 두 번 저장된 경우)."""
    seen: dict[tuple[str, str], Chapter] = {}
    out: list[Chapter] = []
    for ch in chapters:
        key = (ch.title, chapter_hash(ch))
        if key in seen:
            log(f"중복 저장된 회차 제외: {ch.title}")
            continue
        seen[key] = ch
        out.append(ch)
    for i, ch in enumerate(out, start=1):
        ch.index = i
    return out


def warn_gaps(chapters: list[Chapter], log: Log) -> list[int]:
    """제목의 회차 번호가 연속되지 않으면(예: 88화 다음 91화) 빠진 번호를 경고한다."""
    nums = sorted({int(m.group(1)) for c in chapters for m in [_EP_RE.search(c.title)] if m})
    if len(nums) < 2:
        return []
    missing = [n for n in range(nums[0], nums[-1] + 1) if n not in set(nums)]
    if missing and len(missing) <= 30:
        shown = ", ".join(f"{n}화" for n in missing[:15]) + (" …" if len(missing) > 15 else "")
        log(f"[yellow]빠진 회차: {shown} — 저장 파일에 없습니다. 크롬에서 다시 저장해 추가하세요.[/yellow]")
    return missing


def find_incomplete(chapters: list[Chapter]) -> list[Chapter]:
    """이미지 수가 중앙값의 40% 미만인 회차(저장이 덜 된 것)."""
    counts = sorted(len(c.images) for c in chapters)
    if len(counts) < 3:
        return []
    median = counts[len(counts) // 2]
    return [c for c in chapters if len(c.images) < median * 0.4]


def problem_label(c: Chapter, incomplete_ids: set[int]) -> str:
    """점검 창·로그용 문구: '(2장뿐)' 또는 '(일부 이미지 3장 빠짐)'."""
    if id(c) in incomplete_ids:
        return f"{c.title} ({len(c.images)}장뿐)"
    return f"{c.title} (일부 이미지 {len(c.missing)}장 빠짐)"


def find_problems(chapters: list[Chapter]) -> tuple[list[Chapter], set[int]]:
    """저장이 덜 된 회차(이미지 수 부족) + 빠진 본문 이미지가 남은 회차. (회차들, 이미지 수 부족 회차 id 집합)"""
    incomplete = find_incomplete(chapters)
    ids = {id(c) for c in incomplete}
    bad = list(incomplete) + [c for c in chapters if c.missing and id(c) not in ids]
    order = {id(c): i for i, c in enumerate(chapters)}
    bad.sort(key=lambda c: order[id(c)])
    return bad, ids


def drop_incomplete(chapters: list[Chapter], log: Log) -> list[Chapter]:
    """저장이 덜 된 회차와 빠진 본문 이미지가 남은 회차를 책에서 뺀다(다시 저장하도록 안내)."""
    bad_list, incomplete_ids = find_problems(chapters)
    bad = {id(c) for c in bad_list}
    kept: list[Chapter] = []
    for c in chapters:
        if id(c) in bad:
            log(f"[yellow]저장이 덜 된 회차 제외: {problem_label(c, incomplete_ids)} — 크롬에서 다시 저장한 뒤 추가하세요.[/yellow]")
        else:
            kept.append(c)
    for i, c in enumerate(kept, start=1):
        c.index = i
    return kept


def repair_missing(chapters: list[Chapter], log: Log, skip_ids: set[int] | None = None,
                   headed: bool = False) -> None:
    """빠진 본문 이미지(missing)를 원본 주소에서 받아 제자리에 끼워 넣는다. 못 받은 것은 missing에 남긴다.
    사이트가 막으면(403·연결 끊김) 그대로 실패로 두며 우회하지 않는다."""
    from . import browser as br
    from .downloader import ext_from_bytes, fetch_image
    from .models import ImageItem, is_banner_shaped

    todo = [c for c in chapters if c.missing and (skip_ids is None or id(c) not in skip_ids)]
    if not todo:
        return
    total = sum(len(c.missing) for c in todo)
    log(f"빠진 본문 이미지 {total}장을 원본 주소에서 받아 봅니다…")
    with br.launch(headed=headed) as (_b, ctx, _page):
        for ch in todo:
            still: list[tuple[int, str]] = []
            got = 0
            for k, (pos, url) in enumerate(sorted(ch.missing)):
                r = fetch_image(ctx, url, referer=ch.page_url)
                ok = False
                if r:
                    data, ctype = r
                    ext = ext_from_bytes(data, ctype) or ".jpg"
                    dest = (ch.dir or Path(".")) / f"r{pos:04d}_{k}{ext}"
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(data)
                    try:
                        with Image.open(dest) as im:
                            w, h = im.size
                        ok = not is_banner_shaped(w, h) and w >= 100
                    except Exception:  # noqa: BLE001
                        ok = False
                    if ok:
                        # pos는 감지 시점의 본문 순서. 앞서 끼워 넣은 만큼 뒤로 밀린다.
                        insert_at = min(pos + got, len(ch.images))
                        ch.images.insert(insert_at, ImageItem(src=url, width=w, height=h, top=0, left=0, selector="", path=dest))
                        got += 1
                    else:
                        dest.unlink(missing_ok=True)
                if not ok:
                    still.append((pos, url))
            ch.missing = still
            if got and not still:
                log(f"  {ch.title}: 빠진 {got}장을 모두 원본 주소에서 받았습니다.")
            elif got:
                log(f"  [yellow]{ch.title}: 빠진 {got + len(still)}장 중 {got}장만 받았습니다 (나머지는 사이트가 막았을 수 있음).[/yellow]")
            else:
                log(f"  [yellow]{ch.title}: 빠진 {len(still)}장을 받지 못했습니다 (사이트 차단 가능).[/yellow]")


def episode_number(title: str) -> int:
    m = _EP_RE.search(title)
    return int(m.group(1)) if m else 10**9


# ---------- 권 묶기 ----------

def _first_hashes(chapters: list[Chapter]) -> list[str | None]:
    return [file_hash(c.image_paths[0]) if c.image_paths else None for c in chapters]


def drop_site_image(chapters: list[Chapter], log: Log) -> bool:
    """모든 회차가 똑같은 이미지로 시작하면 사이트 공통 이미지(로고·안내)로 보고 전부 제거한다."""
    hashes = _first_hashes(chapters)
    if (len(chapters) >= 3 and hashes[0] is not None and all(h == hashes[0] for h in hashes)
            and all(len(c.images) > 1 for c in chapters)):
        for c in chapters:
            c.images = c.images[1:]
        log("모든 회차가 같은 이미지로 시작해 사이트 공통 이미지로 보고 제거했습니다.")
        return True
    return False


MAX_CHAPTERS_PER_VOLUME = 20
LEAD_K = 5  # 표지를 찾을 때 살펴보는 회차 앞쪽 이미지 수 (사이트 로고·광고 뒤에 표지가 오는 경우 대비)


def _leading(ch: Chapter) -> list[tuple[int, str, int, int]]:
    """회차 앞쪽 LEAD_K장의 (위치, 해시, 폭, 높이)."""
    out = []
    for i, p in enumerate(ch.image_paths[:LEAD_K]):
        try:
            with Image.open(p) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001
            continue
        out.append((i, file_hash(p), w, h))
    return out


def find_covers(chapters: list[Chapter]) -> tuple[list[str | None], set[str]]:
    """각 회차의 표지 해시(없으면 None)와, 표지가 아닌 반복 앞장(사이트 로고·광고) 해시 집합.

    표지 = 앞쪽 LEAD_K장 안에 있고, 2회차 이상에 똑같이 나오며, 본문 폭의 70% 이상인 세로형 이미지.
    """
    leads = [_leading(c) for c in chapters]
    widths = sorted(w for lead in leads for _, _, w, _ in lead)
    body_w = widths[len(widths) // 2] if widths else 0
    from collections import Counter
    seen_in = Counter()
    for lead in leads:
        for h in {h for _, h, _, _ in lead}:
            seen_in[h] += 1
    covers: list[str | None] = []
    site: set[str] = set()
    for lead in leads:
        cover = None
        for _, h, w, hgt in lead:
            repeated = seen_in[h] >= 2
            page_like = w >= body_w * 0.7 and hgt > w
            if repeated and page_like and cover is None:
                cover = h
            elif repeated and not page_like:
                site.add(h)
        covers.append(cover)
    return covers, site


def group_by_cover(chapters: list[Chapter]) -> tuple[list[list[Chapter]], list[str | None]] | None:
    """표지가 같은 연속 회차끼리 묶는다. 표지를 못 찾은 회차는 앞 묶음에 붙는다.

    회차 절반 이상에서 표지를 못 찾으면 None(표지 구조 아님). 한 회차짜리 묶음(표지 변형 등)은 앞 묶음에 합치고,
    20회차를 넘는 묶음은 표지 구조가 깨진 것으로 보고 None. (묶음들, 묶음별 표지 해시)를 돌려준다.
    """
    covers, _ = find_covers(chapters)
    if sum(c is not None for c in covers) < max(1, len(chapters) // 2):
        return None
    groups: list[list[Chapter]] = []
    group_cover: list[str | None] = []
    for ch, h in zip(chapters, covers):
        if groups and (h is None or h == group_cover[-1]):
            groups[-1].append(ch)
            if group_cover[-1] is None:
                group_cover[-1] = h
        else:
            groups.append([ch])
            group_cover.append(h)
    # 유난히 작은 중간 묶음(표지 변형·안내 페이지 변형)은 이웃 묶음 중 작은 쪽에 합친다.
    # 기준: 묶음 크기 중앙값의 40% 미만이면서 3회차 미만. 첫·마지막 묶음은 선택 범위가 잘린 것일 수 있어 두고,
    # 한 회차짜리 묶음은 위치와 상관없이 합친다.
    sizes = sorted(len(g) for g in groups)
    median_size = sizes[len(sizes) // 2]
    merged: list[list[Chapter]] = list(groups)
    merged_cover: list[str | None] = list(group_cover)
    changed = True
    while changed and len(merged) > 1:
        changed = False
        for i, g in enumerate(merged):
            interior = 0 < i < len(merged) - 1
            small = len(g) < 3 and len(g) < median_size * 0.4
            if not (len(g) == 1 or (interior and small)):
                continue
            prev_n = len(merged[i - 1]) if i > 0 else None
            next_n = len(merged[i + 1]) if i + 1 < len(merged) else None
            if prev_n is None and next_n is None:
                break
            into = i - 1 if (next_n is None or (prev_n is not None and prev_n <= next_n)) else i + 1
            if into < i:
                merged[into].extend(g)  # 뒤에 붙임: 앞 회차(표지) 그대로 → 대표 표지 유지
            else:
                merged[into][0:0] = g  # 앞에 붙임: 이제 이 묶음의 맨 앞이 g → g의 표지를 대표로
                merged_cover[into] = merged_cover[i] or merged_cover[into]
            del merged[i]
            del merged_cover[i]
            changed = True
            break
    # 같은 표지가 떨어진 묶음에 다시 나오면(중간에 다른 소스로 대체한 회차가 끼어들어 갈린 경우),
    # 첫 등장~마지막 등장 사이의 묶음을 한 권으로 합친다. (24권 앞뒤 표지가 같은데 211·213 일일툰이 끼어 갈리던 문제)
    i = 0
    while i < len(merged):
        h = merged_cover[i]
        if h is not None:
            last = max((j for j in range(i + 1, len(merged)) if merged_cover[j] == h), default=i)
            if last > i:
                for j in range(i + 1, last + 1):
                    merged[i].extend(merged[j])
                del merged[i + 1:last + 1]
                del merged_cover[i + 1:last + 1]
        i += 1

    if sum(1 for g in groups if len(g) == 1) > len(groups) / 2:
        return None  # 회차마다 첫 장이 제각각 → 표지 구조가 아니다
    if len(merged) == 1 and len(chapters) > 12:
        return None
    if any(len(g) > MAX_CHAPTERS_PER_VOLUME for g in merged):
        return None
    return merged, merged_cover


def _strip_leading(ch: Chapter, drop: set[str], keep_cover: str | None) -> int:
    """회차 앞쪽에서 drop 해시(사이트 이미지·중복 표지)를 제거한다. keep_cover는 남긴다. 제거한 수를 돌려준다."""
    removed = 0
    keep = []
    for i, item in enumerate(ch.images):
        if i < LEAD_K and item.path is not None:
            h = file_hash(item.path)
            if h in drop and h != keep_cover and len(ch.images) - removed > 1:
                removed += 1
                continue
        keep.append(item)
    ch.images = keep
    return removed


def group_by_count(chapters: list[Chapter], n: int) -> list[list[Chapter]]:
    n = max(1, n)
    return [chapters[i:i + n] for i in range(0, len(chapters), n)]


def drop_repeated_leading_images(chapters: list[Chapter], log: Log) -> None:
    """개수 묶기(표지 구조를 못 믿을 때)에서 앞장 정리: 여러 회차에 반복되는 앞쪽 이미지 중
    작은 것(로고·광고)은 모두 제거하고, 페이지 크기인 것(표지일 수 있음)은 첫 등장만 남기고 반복만 제거한다."""
    covers, site = find_covers(chapters)
    removed = 0
    seen_pages: set[str] = set()
    for ch, h in zip(chapters, covers):
        drop = set(site)
        if h is not None:
            if h in seen_pages:
                drop.add(h)         # 이미 나온 표지의 반복
            else:
                seen_pages.add(h)   # 첫 등장은 표지로 남긴다
        removed += _strip_leading(ch, drop, None if h in drop else h)
    if removed:
        log(f"앞쪽 반복 이미지(로고·광고·중복 표지) {removed}장 제거 (표지는 첫 등장만 남김)")


def plan_volumes(chapters: list[Chapter], use_cover: bool, count: int, first_volume: int, log: Log
                 ) -> tuple[list[list[Chapter]], list[int]]:
    """회차들을 권으로 나누고 각 권 번호를 정한다.

    표지 기준은 표지 OCR에서 권 번호가 하나라도 읽힐 때만 신뢰한다. 못 읽으면 사이트 안내 페이지를
    걷어낸 뒤 count개씩 나누고, 권 번호는 first_volume부터 차례로 붙인다.
    """
    if use_cover and len(chapters) > 1:
        found = group_by_cover(chapters)
        if found is not None:
            groups, group_cover = found
            covers_all, site = find_covers(chapters)
            page_like_repeats = {c for c in covers_all if c}  # 표지 후보 전부(안내 페이지 변형 포함)

            def cover_holder(g: list[Chapter], h: str | None) -> tuple[int, Path | None]:
                """묶음 안에서 실제로 표지 이미지를 가진 첫 회차와 그 파일."""
                if h is None:
                    return -1, None
                for k, ch in enumerate(g):
                    for p in ch.image_paths[:LEAD_K]:
                        if file_hash(p) == h:
                            return k, p
                return -1, None

            # 표지에서 권 번호 읽기 (표지마다 한 번만 OCR)
            found_nums: list[int | None] = []
            holders: list[int] = []
            for g, h in zip(groups, group_cover):
                k, cover_path = cover_holder(g, h)
                holders.append(k)
                n = ocr_volume_number(cover_path) if cover_path else None
                if n is not None:
                    log(f"  표지에서 권 번호 읽음: {n}권 ({episode_label(g[0].title)}~{episode_label(g[-1].title)})")
                found_nums.append(n)
            read = sum(1 for n in found_nums if n is not None)
            nums = fill_volume_numbers(found_nums, first_volume)

            # 같은 권 번호가 이웃하면 한 권으로 합친다 (표지가 둘로 갈렸지만 번호가 같은 경우:
            # 한 묶음은 OCR로, 옆 묶음은 앞 권+1로 같은 번호가 되어 "24권 두 개"가 생기던 문제).
            # 대표 표지는 **맨 앞 묶음의 표지**(권 맨 앞의 진짜 표지)를 쓴다. OCR로 번호가 읽힌 게
            # 뒤쪽 표지여도, 화면에 남길 표지는 권 첫머리의 앞표지여야 한다(권 번호는 nums가 이미 정함).
            merged_g: list[list[Chapter]] = []
            merged_n: list[int] = []
            merged_cover: list[str | None] = []  # 대표 표지 = 권 맨 앞 묶음의 표지
            for g, num, h in zip(groups, nums, group_cover):
                if merged_n and merged_n[-1] == num:
                    merged_g[-1].extend(g)
                    if merged_cover[-1] is None:
                        merged_cover[-1] = h  # 앞 묶음에 표지가 없을 때만 뒤 표지로 채움
                else:
                    merged_g.append(list(g))
                    merged_n.append(num)
                    merged_cover.append(h)
            if len(merged_g) < len(groups):
                log(f"  같은 권 번호로 갈린 묶음을 합쳤습니다 ({len(groups)} → {len(merged_g)}권)")

            # 정리: 로고·광고·안내 페이지 변형·반복 표지 제거, 대표 표지는 그 권에서 처음 나오는 곳에만 남김
            removed = 0
            for g, keep_hash in zip(merged_g, merged_cover):
                kept = False
                for ch in g:
                    has_cover = keep_hash is not None and any(
                        file_hash(p) == keep_hash for p in ch.image_paths[:LEAD_K])
                    keep = keep_hash if (has_cover and not kept) else None
                    removed += _strip_leading(ch, set(site) | page_like_repeats, keep)
                    if keep is not None:
                        kept = True
            log(f"표지 이미지 기준으로 {len(merged_g)}권으로 나눔 (권 번호 읽음 {read}/{len(groups)})"
                + (f", 사이트 이미지·반복 표지 {removed}장 제거" if removed else ""))
            return merged_g, merged_n
        log(f"[yellow]표지 구조를 확인하지 못해 {count}개씩 묶습니다. (권 번호는 '첫 권 번호' {first_volume}부터)[/yellow]")
        drop_repeated_leading_images(chapters, log)
    groups = group_by_count(chapters, count)
    return groups, [first_volume + i for i in range(len(groups))]


def discard_inconsistent(found: list[int | None]) -> list[int | None]:
    """읽힌 권 번호 중 다른 읽힌 번호들과 간격이 맞지 않는 것(10, 11 다음에 1)은 오독으로 보고 버린다.
    읽힌 번호가 3개 이상일 때만 판단한다."""
    idx = [i for i, n in enumerate(found) if n is not None]
    if len(idx) < 3:
        return found
    out = list(found)
    for i in idx:
        support = sum(1 for j in idx if j != i and found[j] - found[i] == j - i)
        if support == 0:
            out[i] = None
    return out


def fill_volume_numbers(found: list[int | None], first_volume: int) -> list[int]:
    """OCR로 못 읽은 권 번호를 채운다. 뒤에 읽힌 번호가 있으면 거기서 거꾸로(23권 앞 → 22권),
    없으면 앞 권 + 1, 아무것도 못 읽었으면 first_volume부터 차례로."""
    found = discard_inconsistent(found)
    nums: list[int] = []
    for i, n in enumerate(found):
        if n is not None:
            nums.append(n)
            continue
        nxt = next(((j, m) for j, m in enumerate(found[i + 1:], start=i + 1) if m is not None), None)
        if nums:
            nums.append(nums[-1] + 1)
        elif nxt is not None:
            nums.append(nxt[1] - (nxt[0] - i))
        else:
            nums.append(first_volume)
    return nums


def volume_name(series: str, vol: int, g: list[Chapter]) -> str:
    first, last = episode_label(g[0].title), episode_label(g[-1].title)
    span = first if first == last else f"{first}~{last}"
    return f"{series} {vol}권 ({span})".strip()
