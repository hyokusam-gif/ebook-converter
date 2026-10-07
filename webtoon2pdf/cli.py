"""webtoon2pdf 명령줄 진입점."""
from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
import re
import shutil
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from urllib.request import url2pathname

import typer
from PIL import Image
from rich.console import Console

from . import browser as br
from .downloader import download_images
from .extractor import extract_images
from .mhtml import chapter_from_mhtml
from .models import Chapter, ImageItem, JobConfig, is_banner_shaped
from .navigator import find_next_url
from .epub_builder import build_epub
from .pdf_builder import build_pdf
from .image_builder import build_images
from .stitch import stitch_canvas_strips, merge_slices_to_pages
from . import volumes as vol
from . import naver

app = typer.Typer(add_completion=False, help="웹페이지의 이미지를 순서대로 모아 PDF 전자책으로 만듭니다. (개인 소장 용도)")
console = Console()

Log = Callable[[str], None]


def _log(msg: str) -> None:
    console.print(msg, highlight=False)


def _safe_name(s: str, limit: int = 60) -> str:
    s = s.replace(":", "：")  # 콜론은 전각(：)으로 보존 — 파일명에 ':'는 못 쓰지만 '：'는 됨
    s = re.sub(r"[\\/*?\"<>|\r\n\t]+", " ", s).strip()
    s = re.sub(r"\s+", " ", s)
    return s[:limit] or "untitled"


def _job_dir(cfg: JobConfig) -> Path:
    host = (urlparse(cfg.start_url).hostname or "local").replace(":", "_")
    digest = hashlib.sha1(cfg.start_url.encode()).hexdigest()[:8]
    return cfg.work_dir / f"{host}_{digest}"


IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


def resolve_input(s: str) -> tuple[str, str]:
    """입력을 ("url", url) / ("html", file_url) / ("dir", path)로 판별한다."""
    s = s.strip().strip('"')
    if s.lower().startswith(("http://", "https://")):
        return "url", s
    if s.lower().startswith("file://"):
        return "html", s
    p = Path(s).expanduser()
    if p.is_dir():
        return "dir", str(p.resolve())
    if p.is_file() and p.suffix.lower() in (".mhtml", ".mht"):
        return "mhtml", str(p.resolve())
    if p.is_file() and p.suffix.lower() in (".html", ".htm"):
        return "html", p.resolve().as_uri()
    raise RuntimeError("주소나 파일/폴더 경로가 올바르지 않습니다. http(s):// 주소, 저장한 .html 파일, 또는 이미지 폴더를 넣어 주세요.")


# 크롬 확장이 저장하는 파일명: 연속 저장 B<YYYYMMDD-HHMM>_<NNN>_<제목>, 낱개 저장 <제목>_<YYYYMMDD-HHMM>
BATCH_PREFIX_RE = re.compile(r"^B(\d{8}-\d{4})_(\d{3})_")
_STAMP_SUFFIX_RE = re.compile(r"_\d{8}-\d{4}$")


def saved_page_title(stem: str) -> str:
    """파일명(확장자 제외)에서 배치 접두(B…_001_)와 저장 시각(_20260908-1515)을 뗀 제목."""
    return _STAMP_SUFFIX_RE.sub("", BATCH_PREFIX_RE.sub("", stem))


SAVED_PAGE_EXTS = {".mhtml", ".mht", ".html", ".htm"}


def saved_pages_in(folder: Path) -> list[Path]:
    """폴더 바로 아래의 저장한 웹페이지 파일들(이름의 숫자 순). '웹페이지, 전체' 저장의 _files 폴더는 무시."""
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in SAVED_PAGE_EXTS]
    return sorted(files, key=_natural_key)


def _clear_prior_books(out_dir: Path, log: Log) -> None:
    """권 나눠 저장하는 폴더 안의 이전 결과물(.epub/.pdf)을 지운다.
    이 폴더는 이번 변환 전용이라, 지난 변환에서 이름이 달라 남은 파일이 쌓여 '같은 권이 두 개'로 보이던 문제를 막는다."""
    removed = 0
    for f in out_dir.glob("*"):
        if f.is_file() and f.suffix.lower() in (".epub", ".pdf"):
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        log(f"이전 결과물 {removed}개를 지우고 새로 만듭니다.")


def _trim_after_episode(title: str) -> str:
    """'나루토 155화 - 일일툰 일본만화…' → '나루토 155화'. 회차 표시 뒤 사이트 꼬리표를 잘라 목차를 깔끔히 한다."""
    m = re.search(r"^(.*?\d{1,4}\s*화)\b", title)
    return m.group(1).strip() if m else title


def _nice_title(raw: str) -> str:
    """웹툰 페이지의 지저분한 탭 제목에서 '작품명 N화'만 깔끔히 뽑는다.
    예) '제목 : 라그나로크 0001 - 제목 ： 라그나로크 1화 - 뉴토끼 미리보기' → '제목 : 라그나로크 1화'.
    뉴토끼·마나토끼류의 'NNNN - 작품명 N화 - 사이트' 패턴일 때만 정리하고, 그 외 제목은 원본 그대로 둔다
    (일반 꼬리표는 뒤에서 strip_common_suffix가 처리)."""
    if not raw:
        return ""
    s = raw.replace("：", ":").strip()
    m = re.search(r"\d{3,4}\s*-\s*(.+?\d{1,4}\s*화)(?=\s*-\s|\s*$)", s)  # NNNN - <작품명 N화> - 사이트
    return m.group(1).strip() if m else raw


def _natural_key(p: Path) -> list:
    """이름의 숫자 순 정렬 키. 배치 접두와 저장 시각은 무시해서 연속 저장 파일과 낱개 저장 파일이
    섞여 있어도 회차 번호 순('나루토 1화' < '나루토 12화')이 되게 한다. 제목이 같으면 배치·번호 순."""
    m = BATCH_PREFIX_RE.match(p.stem)
    title = saved_page_title(p.stem)
    key = [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", title)]
    return [key, m.group(1) if m else "", int(m.group(2)) if m else 0]


def _chapter_from_folder(folder: Path, min_width: int, log: Log) -> Chapter:
    """폴더의 이미지 파일을 이름 순으로. 너무 작거나 배너 모양(광고·구분선)은 제외."""
    files = sorted((f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in IMAGE_EXTS), key=_natural_key)
    ch = Chapter(index=1, url=str(folder), title=_safe_name(folder.name), dir=folder)
    skipped = 0
    for f in files:
        try:
            with Image.open(f) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        if w * 2 < min_width or is_banner_shaped(w, h):
            skipped += 1
            continue
        ch.images.append(ImageItem(src=str(f), width=w, height=h, top=0, left=0, selector="", path=f))
    if skipped:
        log(f"  작은 이미지·배너 모양 {skipped}개 제외")
    return ch


def _load_manifest(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"chapters": [], "next_url": None}


def _save_manifest(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _chapters_from_manifest(m: dict) -> list[Chapter]:
    chapters: list[Chapter] = []
    for c in m["chapters"]:
        ch = Chapter(index=c["index"], url=c["url"], title=c["title"], dir=Path(c["dir"]))
        for f in c["files"]:
            p = Path(f)
            if p.exists():
                ch.images.append(ImageItem(src="", width=0, height=0, top=0, left=0, selector="", path=p))
        chapters.append(ch)
    return chapters


CHALLENGE_HINT = (
    "이 사이트는 봇 차단(Cloudflare 등) 보안 확인을 요구합니다. "
    "'브라우저 창 보기'를 켜고 다시 실행한 뒤, 열린 창에서 확인을 직접 완료해 주세요."
)


class ChallengeRequired(RuntimeError):
    """헤드리스 상태에서 보안 확인 페이지를 만났을 때."""


def _pass_challenge(page, cfg: JobConfig, url: str, log: Log) -> None:
    reason = br.is_challenge_page(page)
    if reason is None:
        return
    if not cfg.headed:
        raise ChallengeRequired(f"{CHALLENGE_HINT} ({reason})")
    log("  [yellow]사이트가 보안 확인을 요구합니다. 브라우저 창에서 확인을 완료하면 자동으로 계속됩니다. (최대 120초)[/yellow]")
    if not br.wait_for_challenge_clear(page, timeout_s=120):
        raise RuntimeError("보안 확인이 120초 안에 완료되지 않았습니다. 다시 시도해 주세요.")
    log("  보안 확인 통과, 수집을 계속합니다.")
    if page.url.split("#", 1)[0] != url.split("#", 1)[0]:
        br.load(page, url, timeout_ms=cfg.timeout_ms)


def _friendly_error(e: Exception) -> str:
    s = str(e)
    if "ERR_CONNECTION_RESET" in s or "ERR_CONNECTION_CLOSED" in s or "ERR_HTTP2_PROTOCOL_ERROR" in s:
        return ("사이트가 연결을 끊었습니다. 봇 차단이 자동화 브라우저의 접속 자체를 막고 있을 수 있습니다. "
                "잠시 뒤 '브라우저 창 보기'를 켜고 다시 시도해 보세요.")
    if "ERR_NAME_NOT_RESOLVED" in s or "ERR_CONNECTION_REFUSED" in s:
        return "사이트에 접속할 수 없습니다. 주소를 확인해 주세요."
    if "Timeout" in s and "goto" in s:
        return "페이지 로드 시간이 초과되었습니다. 인터넷 연결을 확인하거나 시간 제한을 늘려 보세요."
    return s.splitlines()[0]


def _grab_chapter(page, cfg: JobConfig, index: int, url: str, out_dir: Path, log: Log) -> Chapter:
    log(f"[bold]{index}번째 페이지[/bold] {url}")
    br.load(page, url, timeout_ms=cfg.timeout_ms)
    _pass_challenge(page, cfg, url, log)
    br.scroll_to_bottom(page, step_wait=cfg.scroll_wait)
    br.wait_images(page)
    title = _safe_name(_nice_title(page.title()) or f"{index}화")

    items = extract_images(page, min_width=cfg.min_width, mode=cfg.mode)
    if not items:
        log(f"  이미지가 없어 최소 폭을 {cfg.min_width // 2}px로 낮춰 재시도")
        items = extract_images(page, min_width=max(50, cfg.min_width // 2), mode=cfg.mode)
    log(f"  이미지 {len(items)}장 감지")

    ch = Chapter(index=index, url=url, title=title, dir=out_dir)
    if url.lower().startswith("file://"):
        # 크롬 '웹페이지, 전체' 저장본 첫 줄: <!-- saved from url=(0043)https://... -->
        m = re.search(r"saved from url=\(\d+\)(\S+?)\s*-->", page.content()[:3000])
        ch.page_url = m.group(1) if m else ""
    if items:
        ch.images = download_images(page, items, out_dir, delay=cfg.delay, min_width=cfg.min_width, log=log,
                                    missing=ch.missing)
        stitch_canvas_strips(ch, log)  # 캔버스 뷰어가 페이지를 조각으로 나눠 그린 경우 이어 붙임
    if not ch.images:
        log("  [yellow]이미지를 하나도 저장하지 못해 페이지 전체 스크린샷으로 대체합니다.[/yellow]")
        out_dir.mkdir(parents=True, exist_ok=True)
        shot = out_dir / "0001.png"
        page.screenshot(path=str(shot), full_page=True)
        ch.images = [ImageItem(src=url, width=0, height=0, top=0, left=0, selector="body", path=shot)]
    log(f"  저장 {len(ch.images)}장")
    return ch


FORMATS = ("pdf", "epub", "both", "jpg", "webp")


def _write_outputs(chapters: list[Chapter], cfg: JobConfig, base: Path, tmp: Path, log: Log) -> Path:
    """cfg.format에 따라 PDF/EPUB을 쓰거나(jpg/webp면) 낱장 이미지 폴더로 내보낸다.
    base의 책 확장자는 무시하고 형식별로 붙인다. 대표 결과 경로(파일 또는 폴더)를 돌려준다."""
    if cfg.format not in FORMATS:
        raise RuntimeError(f"형식은 {FORMATS} 중 하나여야 합니다: {cfg.format}")
    if cfg.format in ("jpg", "webp"):
        out_dir = base.with_suffix("")
        n = build_images(chapters, out_dir, cfg.format, log=log)
        log(f"[bold green]완료:[/bold green] {out_dir} ({n}장 {cfg.format.upper()} 이미지, 폴더)")
        return out_dir
    stem = base.with_suffix("") if base.suffix.lower() in (".pdf", ".epub") else base
    title = cfg.title or (chapters[0].title if len(chapters) == 1 else stem.name)
    result: Path | None = None
    if cfg.format in ("pdf", "both"):
        p = stem.with_suffix(".pdf")
        n = build_pdf(chapters, p, fit_width=cfg.fit_width, tmp_dir=tmp, title=title, author=cfg.author, log=log)
        log(f"[bold green]완료:[/bold green] {p} ({n}쪽, {len(chapters)}개 북마크)")
        result = p
    if cfg.format in ("epub", "both"):
        e = stem.with_suffix(".epub")
        n = build_epub(chapters, e, tmp_dir=tmp / "epub", title=title, author=cfg.author, rtl=cfg.rtl, log=log)
        log(f"[bold green]완료:[/bold green] {e} ({n}쪽, 아이폰·아이패드 '도서(Books)' 앱에서 열기)")
        result = e if cfg.format == "epub" else result
    return result  # type: ignore[return-value]


INPUT_SEP = "|"


def resolve_inputs(s: str) -> list[tuple[str, str]]:
    """'|' 또는 줄바꿈으로 구분된 여러 입력을 판별한다. 로컬 파일들은 이름의 숫자 순(1화, 2화, … 10화)으로 정렬."""
    parts = [p.strip().strip('"') for p in re.split(r"[|\r\n]+", s) if p.strip().strip('"')]
    if not parts:
        raise RuntimeError("입력이 비어 있습니다.")
    resolved: list[tuple[str, str]] = []
    for p in parts:
        kind, target = resolve_input(p)
        if kind == "dir":
            pages = saved_pages_in(Path(target))
            if pages:  # 저장한 페이지가 든 폴더 → 그 파일들을 회차로
                resolved.extend(resolve_input(str(f)) for f in pages)
                continue
        resolved.append((kind, target))
    if len(resolved) > 1:
        if any(k == "url" for k, _ in resolved):
            raise RuntimeError("여러 개를 한 번에 넣을 때는 저장한 파일이나 폴더만 가능합니다. 웹 주소는 하나씩 넣어 주세요.")
        resolved.sort(key=lambda kt: _natural_key(Path(kt[1])))
    return resolved


def _local_chapter(page, cfg: JobConfig, index: int, kind: str, target: str, tmp_root: Path, log: Log) -> Chapter:
    if kind == "dir":
        ch = _chapter_from_folder(Path(target), cfg.min_width, log)
        log(f"[bold]{index}. 폴더[/bold] {Path(target).name} — 이미지 {len(ch.images)}장 (파일명 순)")
    elif kind == "mhtml":
        log(f"[bold]{index}. 저장한 페이지(MHTML)[/bold] {Path(target).name}")
        ch = chapter_from_mhtml(Path(target), tmp_root / f"mhtml{index:03d}", min_width=cfg.min_width, log=log)
    else:  # html → 브라우저로 렌더링
        ch = _grab_chapter(page, cfg, index, target, tmp_root / f"html{index:03d}", log)
    ch.index = index
    if not ch.images:
        raise RuntimeError(f"{Path(target).name} 안에서 이미지를 찾지 못했습니다. 저장할 때 이미지가 다 로드된 상태였는지 확인해 주세요.")
    return ch


def _series_name(chapters: list[Chapter]) -> str:
    name = vol.series_name(chapters)
    return _safe_name(name, 40) if name else ""


def parse_range(s: str) -> tuple[int, int]:
    """'1-10', '11~20', '11 - 20', '7' → (lo, hi). 잘못되면 RuntimeError."""
    m = re.fullmatch(r"\s*(\d{1,4})\s*(?:[-~]\s*(\d{1,4}))?\s*", s or "")
    if not m:
        raise RuntimeError(f"범위는 1-10 처럼 넣어 주세요: '{s}'")
    lo = int(m.group(1))
    hi = int(m.group(2)) if m.group(2) else lo
    if lo < 1 or hi < lo:
        raise RuntimeError(f"범위가 올바르지 않습니다: '{s}' (작은 수-큰 수)")
    return lo, hi


def _apply_webtoon_merge(chapters: list[Chapter], cfg: JobConfig, log: Log) -> None:
    """웹툰: 각 회차의 작게 잘린 조각들을 '긴 한 쪽'으로 이어 붙여 끊김을 줄인다(cfg.webtoon_merge일 때)."""
    if not cfg.webtoon_merge:
        return
    for ch in chapters:
        merge_slices_to_pages(ch, cfg.webtoon_page_px, log)


def _make_groups(chapters: list[Chapter], cfg: JobConfig, log: Log) -> tuple[list[list[Chapter]], list[str]] | None:
    """여러 회차를 권으로 나눈다. (묶음들, 이름들) 또는 나누지 않으면 None.
    cfg.volume_range가 있으면 그 범위의 권만 남긴다."""
    if cfg.group_by_cover and len(chapters) > 1:
        groups, nums = vol.plan_volumes(chapters, True, cfg.group or 7, cfg.first_volume, log)
    elif cfg.group and len(chapters) > cfg.group:
        groups, nums = vol.plan_volumes(chapters, False, cfg.group, cfg.first_volume, log)
    else:
        return None
    if cfg.volume_range:
        lo, hi = cfg.volume_range
        keep = [(g, n) for g, n in zip(groups, nums) if lo <= n <= hi]
        if not keep:
            raise RuntimeError(f"지정한 권({lo}~{hi})이 없습니다. 읽힌 권: {min(nums)}~{max(nums)}")
        log(f"권 범위 {lo}~{hi}: {len(groups)}권 중 {len(keep)}권만 만듭니다 (나머지는 건너뜀)")
        groups, nums = [g for g, _ in keep], [n for _, n in keep]
    series = _series_name(chapters) or _safe_name(cfg.output.stem, 40)
    names = [_safe_name(vol.volume_name(series, n, g), 80) for n, g in zip(nums, groups)]
    return groups, names


def _filter_episode_range(inputs: list[tuple[str, str]], rng: tuple[int, int], log: Log) -> list[tuple[str, str]]:
    """파일명의 회차 번호로 읽을 파일을 미리 거른다(읽는 시간 절약). 번호를 알 수 없는 파일은 남긴다."""
    lo, hi = rng
    kept = []
    for kind, target in inputs:
        if kind == "url":
            n = 10**9
        else:
            local = Path(url2pathname(urlparse(target).path)) if target.lower().startswith("file://") else Path(target)
            n = vol.episode_number(saved_page_title(local.stem))
        if n == 10**9 or lo <= n <= hi:
            kept.append((kind, target))
    if not kept:
        raise RuntimeError(f"화 범위 {lo}~{hi}에 해당하는 파일이 없습니다.")
    if len(kept) < len(inputs):
        log(f"화 범위 {lo}~{hi}: 파일 {len(inputs)}개 중 {len(kept)}개만 읽습니다")
    return kept


Progress = Callable[[int, int], None]  # (처리한 수, 전체 수)
# 점검 결과 콜백: (저장이 덜 된 회차들의 (제목, 원본 파일), 빠진 회차 번호들) → 추가할 파일 경로들. None이면 취소.
AskReplacements = Callable[[list[tuple[str, Path | None]], list[int]], "list[str] | None"]


def source_path(ch: Chapter) -> Path | None:
    """회차가 어느 저장 파일에서 왔는지(file:// URL 또는 경로)."""
    u = ch.url or ""
    if u.lower().startswith("file://"):
        return Path(url2pathname(urlparse(u).path))
    p = Path(u)
    return p if p.exists() else None


def _load_chapters(cfg: JobConfig, inputs: list[tuple[str, str]], tmp_root: Path,
                   log: Log, progress: Progress | None = None) -> list[Chapter]:
    """저장한 파일·폴더 입력들을 각각 한 회차로 읽어들인다(변환·분석 공통).
    html 입력이 섞이면 브라우저로 렌더링한다. cfg.episode_range가 있으면 파일명 번호로 미리 거른다."""
    if cfg.episode_range and len(inputs) > 1:
        inputs = _filter_episode_range(inputs, cfg.episode_range, log)
    total = len(inputs)

    def tick(done: int) -> None:
        if progress:
            progress(done, total)

    if len(inputs) > 1:
        log(f"파일 {len(inputs)}개를 이름 순서대로 읽습니다.")
    elif inputs[0][0] == "html" and cfg.max_chapters != 1:
        log("저장한 페이지 파일은 이 페이지만 변환합니다. ('다음' 링크는 따라가지 않음)")

    chapters: list[Chapter] = []
    needs_browser = any(k == "html" for k, _ in inputs)
    tick(0)
    if needs_browser:
        with br.launch(headed=cfg.headed, cookies=cfg.cookies) as (_b, _ctx, page):
            for i, (kind, target) in enumerate(inputs, start=1):
                chapters.append(_local_chapter(page, cfg, i, kind, target, tmp_root, log))
                tick(i)
    else:
        for i, (kind, target) in enumerate(inputs, start=1):
            chapters.append(_local_chapter(None, cfg, i, kind, target, tmp_root, log))
            tick(i)
    return chapters


def _run_local(cfg: JobConfig, inputs: list[tuple[str, str]], log: Log, progress: Progress | None = None,
               ask_replacements: AskReplacements | None = None) -> Path:
    """저장한 파일·폴더 입력(여러 개 가능). 각 입력이 한 회차가 되고, cfg.group>0이면 그 개수씩 나눠 여러 권으로.

    ask_replacements가 있으면 책을 만들기 전에 점검 결과(저장이 덜 된 회차·빠진 회차)를 알리고,
    돌려받은 파일들을 회차로 추가한 뒤 다시 점검한다(최대 3번).
    """
    tmp_root = cfg.work_dir / ".tmp_local"
    shutil.rmtree(tmp_root, ignore_errors=True)
    try:
        chapters = _load_chapters(cfg, inputs, tmp_root, log, progress)
        if len(chapters) > 1:
            log(f"파일 {len(chapters)}개 읽기 완료. 중복·누락 검사와 표지 확인 중… (파일이 많으면 1~2분)")
            repaired: set[int] = set()
            for round_no in range(4):
                vol.strip_common_suffix(chapters, log)
                chapters = vol.dedupe_chapters(chapters, log)
                vol.repair_missing(chapters, log, skip_ids=repaired, headed=cfg.headed)  # 빠진 장은 먼저 원본에서 받아 본다
                repaired |= {id(c) for c in chapters}
                bad, incomplete_ids = vol.find_problems(chapters)
                missing = vol.warn_gaps(chapters, log)
                if ask_replacements is None or round_no == 3 or not (bad or missing):
                    chapters = vol.drop_incomplete(chapters, log)
                    break
                # 점검 결과를 알리고 다시 저장한 파일을 받는다
                bad_sorted = sorted(bad, key=lambda c: vol.episode_number(c.title))
                for c in bad_sorted:
                    log(f"[yellow]저장이 덜 된 회차: {vol.problem_label(c, incomplete_ids)}[/yellow]")
                added = ask_replacements([(vol.problem_label(c, incomplete_ids), source_path(c)) for c in bad_sorted], missing)
                if added is None:
                    raise RuntimeError("변환을 취소했습니다.")
                bad_ids = {id(c) for c in bad}
                chapters = [c for c in chapters if id(c) not in bad_ids]
                if not added:
                    log("추가한 파일 없이 계속합니다." if bad or missing else "")
                    break
                log(f"다시 저장한 파일·주소 {len(added)}개를 추가합니다.")
                # 파일(.mhtml/.html/폴더)과 웹 주소(캔버스 뷰어 사이트 등)를 섞어 넣을 수 있다
                new_inputs = [resolve_input(p) for p in added]
                need_browser = any(k in ("html", "url") for k, _ in new_inputs)
                with (br.launch(headed=cfg.headed, cookies=cfg.cookies) if need_browser else nullcontext((None, None, None))) as (_b, _ctx, page):
                    for k, t in new_inputs:
                        n = len(chapters) + 1
                        if k == "url":
                            log(f"[bold]추가 회차 주소에서 받기[/bold] {t}")
                            new_ch = _grab_chapter(page, cfg, n, t, tmp_root / f"url{n:03d}", log)
                        else:
                            new_ch = _local_chapter(page, cfg, n, k, t, tmp_root, log)
                        new_ch.title = _trim_after_episode(new_ch.title)  # 다른 사이트 꼬리표 정리
                        ep = vol.episode_number(new_ch.title)
                        log(f"  → {new_ch.title} ({'회차 번호 ' + str(ep) + '자리에 넣음' if ep < 10**9 else '회차 번호 못 읽음, 맨 뒤'})")
                        chapters.append(new_ch)
                chapters.sort(key=lambda c: (vol.episode_number(c.title), _natural_key(source_path(c) or Path(c.title))))
                for i, c in enumerate(chapters, start=1):
                    c.index = i
            if not chapters:
                raise RuntimeError("남은 회차가 없습니다. 저장이 덜 된 파일뿐입니다.")
        _apply_webtoon_merge(chapters, cfg, log)
        made = _make_groups(chapters, cfg, log)
        if made is not None:
            groups, names = made
            out_dir = cfg.output.with_suffix("") if cfg.output.suffix.lower() in (".pdf", ".epub", ".jpg", ".webp") else cfg.output
            out_dir.mkdir(parents=True, exist_ok=True)
            _clear_prior_books(out_dir, log)  # 이전 변환에서 남은 다른 이름의 책 파일 정리(권 두 개로 보이는 문제 방지)
            saved_title = cfg.title
            for g, name in zip(groups, names):
                cfg.title = name
                _write_outputs(g, cfg, out_dir / name, tmp_root / "norm", log)
                log(f"  {name}: {len(g)}회차")
            cfg.title = saved_title
            # 여러 권을 아이폰으로 한 번에 옮기기 쉽게 zip도 같이 만든다 (파일 앱에서 풀어 전체 선택 → 도서)
            zip_path = Path(shutil.make_archive(str(out_dir), "zip", root_dir=out_dir))
            log(f"[bold green]완료:[/bold green] {out_dir} ({len(groups)}권) + 한 번에 보내기용 {zip_path.name}")
            return out_dir
        if len(chapters) > 1 and not cfg.title:
            cfg.title = _safe_name(chapters[0].title, 40) + " ~ " + _safe_name(chapters[-1].title, 40)
        return _write_outputs(chapters, cfg, cfg.output, tmp_root / "norm", log)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def analyze_volumes(cfg: JobConfig, inputs: list[tuple[str, str]], log: Log,
                    progress: Progress | None = None) -> vol.VolumeReport:
    """저장한 파일·폴더를 '변환하지 않고' 권 구조만 분석한다(크롬에서 모아온 뒤·폴더 점검용).

    표지가 있으면 표지로 권을 나누고 번호를 읽는다(마지막 권은 다음 권 표지가 아직 없어 '미확정').
    표지가 없으면 covers_detected=False — 권을 자동으로 못 나누므로 N개씩 임시 묶음과 추정 번호만 돌려준다.
    변환 파이프라인(run)과 같은 읽기·중복제거·점검 함수를 재사용한다.
    """
    tmp_root = cfg.work_dir / ".tmp_analyze"
    shutil.rmtree(tmp_root, ignore_errors=True)
    try:
        chapters = _load_chapters(cfg, inputs, tmp_root, log, progress)
        if not chapters:
            raise RuntimeError("읽을 회차가 없습니다.")
        vol.strip_common_suffix(chapters, log)
        chapters = vol.dedupe_chapters(chapters, log)
        series = _series_name(chapters)
        bad, incomplete_ids = vol.find_problems(chapters)
        incomplete_labels = [vol.problem_label(c, incomplete_ids)
                             for c in sorted(bad, key=lambda c: vol.episode_number(c.title))]
        missing = vol.warn_gaps(chapters, log)
        covers_detected = len(chapters) > 1 and vol.group_by_cover(chapters) is not None
        count = cfg.group or 7
        groups, nums = vol.plan_volumes(chapters, covers_detected, count, cfg.first_volume, log)
        infos: list[vol.VolumeInfo] = []
        for i, (g, n) in enumerate(zip(groups, nums)):
            if covers_detected:
                status = "미확정" if i == len(groups) - 1 else "완결"
            else:
                status = "개수묶기"
            infos.append(vol.VolumeInfo(
                number=n, first_ep=vol.episode_label(g[0].title), last_ep=vol.episode_label(g[-1].title),
                chapter_count=len(g), status=status))
        return vol.VolumeReport(
            series=series, covers_detected=covers_detected, volumes=infos,
            missing_episodes=missing, incomplete_labels=incomplete_labels,
            total_chapters=len(chapters), count_per_volume=count)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


NAVER_BLOG_HOSTS = {"blog.naver.com", "m.blog.naver.com"}


def is_naver_category(url: str) -> bool:
    """네이버 블로그 카테고리 주소인가? (호스트가 네이버 블로그이고 categoryNo가 들어 있음)"""
    try:
        u = urlparse(url.strip().strip('"'))
    except Exception:  # noqa: BLE001
        return False
    return (u.hostname in NAVER_BLOG_HOSTS) and ("categoryNo=" in (u.query or ""))


def _run_naver_category(cfg: JobConfig, log: Log, progress: Progress | None = None) -> Path:
    """네이버 블로그 카테고리 주소 → 제목의 'N권'으로 묶어 권마다 PDF/EPUB을 만든다."""
    blog_id, category_no, cat_name = naver.resolve_category(cfg.start_url, log)
    log(f"네이버 블로그 카테고리 '{cat_name or category_no}' (blogId={blog_id}) 목록을 가져옵니다…")
    order = naver.build_reading_order(naver.list_posts(blog_id, category_no, log), log)
    if not order:
        raise RuntimeError("제목에 'N권'이 있는 만화 글을 찾지 못했습니다. 카테고리 주소가 맞는지 확인해 주세요.")
    vols = sorted(order)
    if cfg.volume_range:
        lo, hi = cfg.volume_range
        vols = [v for v in vols if lo <= v <= hi]
        if not vols:
            raise RuntimeError(f"지정한 권({lo}~{hi})이 이 카테고리에 없습니다. 있는 권: {sorted(order)}")
        log(f"권 범위 {lo}~{hi}: {len(vols)}권만 만듭니다.")
    series = cfg.title or naver.series_from_titles([p.title for g in order.values() for p in g]) or (cat_name or "만화")

    out_dir = cfg.output.with_suffix("") if cfg.output.suffix.lower() in (".pdf", ".epub", ".jpg", ".webp") else cfg.output
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_root = cfg.work_dir / ".tmp_naver"
    shutil.rmtree(tmp_root, ignore_errors=True)
    total = sum(len(order[v]) for v in vols)
    done = 0
    saved_title = cfg.title
    names: list[str] = []
    try:
        if progress:
            progress(0, total)
        for v in vols:
            posts = order[v]
            chapters: list[Chapter] = []
            for i, post in enumerate(posts, start=1):
                log(f"[bold]{v}권 {i}/{len(posts)}[/bold] {post.title[:42]}")
                try:
                    ch = naver.download_post(blog_id, post, tmp_root / f"v{v:03d}_{i:03d}",
                                             min_width=cfg.min_width, delay=cfg.delay, log=log)
                except naver.NoImages:
                    log("  [yellow]직접 받기로 이미지를 못 찾아 브라우저로 재시도합니다.[/yellow]")
                    with br.launch(headed=cfg.headed, cookies=cfg.cookies) as (_b, _ctx, page):
                        ch = _grab_chapter(page, cfg, i, post.view_url, tmp_root / f"v{v:03d}_{i:03d}b", log)
                ch.index = i
                ch.title = post.title
                chapters.append(ch)
                done += 1
                if progress:
                    progress(done, total)
            naver.verify_page_numbers(chapters, log)
            name = _safe_name(f"{series} {v}권", 80)
            cfg.title = name
            _write_outputs(chapters, cfg, out_dir / name, tmp_root / "norm", log)
            imgs = sum(len(c.images) for c in chapters)
            log(f"  [green]{name}[/green]: {len(chapters)}회차, {imgs}장")
            names.append(name)
        cfg.title = saved_title
        if len(names) > 1:
            zip_path = Path(shutil.make_archive(str(out_dir), "zip", root_dir=out_dir))
            log(f"[bold green]완료:[/bold green] {out_dir} ({len(names)}권) + 한 번에 보내기용 {zip_path.name}")
        else:
            log(f"[bold green]완료:[/bold green] {out_dir} ({len(names)}권)")
        return out_dir
    finally:
        cfg.title = saved_title
        if not cfg.keep_images:
            shutil.rmtree(tmp_root, ignore_errors=True)


_GENERIC_OUTPUT_NAMES = {"이미지북", "output", "이미지pdf", ""}


def _auto_output(cfg: JobConfig, chapters: list[Chapter]) -> Path:
    """출력 파일명이 기본값('이미지북' 등)이면 수집한 제목으로 바꾼다(폴더 위치는 그대로).
    사용자가 저장 위치에서 이름을 직접 정했으면 그대로 둔다. 주소(URL) 수집은 변환 전 제목을 몰라 기본값이 쓰임."""
    out = cfg.output
    if not chapters or out.with_suffix("").name.strip().lower() not in {n.lower() for n in _GENERIC_OUTPUT_NAMES}:
        return out
    series = vol.series_name(chapters)
    if len(chapters) == 1:
        name = _trim_after_episode(chapters[0].title) or series or chapters[0].title
    elif series:
        name = f"{series} {vol.episode_label(chapters[0].title)}~{vol.episode_label(chapters[-1].title)}"
    else:
        name = f"{chapters[0].title} ~ {chapters[-1].title}"
    name = _safe_name(name, 70) or "이미지북"
    return out.with_name(name + out.suffix)


def run(cfg: JobConfig, log: Log = _log, progress: Progress | None = None,
        ask_replacements: AskReplacements | None = None) -> Path:
    """수집 → PDF 생성 전체 파이프라인. 결과 PDF(또는 --split이면 폴더) 경로를 돌려준다.
    progress(처리한 수, 전체 수)는 저장 파일 여러 개를 처리할 때 파일마다 호출된다.
    ask_replacements는 점검 결과를 알리고 다시 저장한 파일을 받아오는 콜백(창 프로그램용)."""
    if is_naver_category(cfg.start_url):
        return _run_naver_category(cfg, log, progress)
    inputs = resolve_inputs(cfg.start_url)
    if len(inputs) > 1 or inputs[0][0] != "url":
        return _run_local(cfg, inputs, log, progress, ask_replacements)
    job_dir = _job_dir(cfg)
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = job_dir / "manifest.json"
    manifest = _load_manifest(manifest_path)
    chapters = _chapters_from_manifest(manifest)
    visited = {c.url for c in chapters}

    if chapters:
        log(f"[cyan]이전 작업 {len(chapters)}화를 이어서 진행합니다.[/cyan]")
        url = manifest.get("next_url")
    else:
        url = cfg.start_url

    stop_reason = "완료"
    with br.launch(headed=cfg.headed, cookies=cfg.cookies, log=log) as (_b, _ctx, page):
        while url and len(chapters) < cfg.max_chapters:
            if url in visited:
                stop_reason = "이미 방문한 URL이 다시 나와 중단"
                break
            index = len(chapters) + 1
            try:
                ch = _grab_chapter(page, cfg, index, url, job_dir / f"{index:03d}", log)
            except ChallengeRequired:
                if not chapters:
                    raise  # 첫 페이지부터 막히면 사용자에게 바로 알린다
                log(f"  [red]{index}화에서 보안 확인 요구로 중단[/red]")
                stop_reason = f"{index}화에서 보안 확인 요구로 중단"
                break
            except Exception as e:  # noqa: BLE001
                msg = _friendly_error(e)
                if not chapters:
                    raise RuntimeError(msg) from e
                log(f"  [red]{index}화 수집 실패: {msg}[/red]")
                stop_reason = f"{index}화 수집 실패로 중단"
                break
            chapters.append(ch)
            visited.add(url)

            next_url = find_next_url(page, url, selector=cfg.next_selector)
            manifest["chapters"].append({
                "index": ch.index, "url": ch.url, "title": ch.title, "dir": str(ch.dir),
                "files": [str(p) for p in ch.image_paths],
            })
            manifest["next_url"] = next_url
            _save_manifest(manifest_path, manifest)

            if not next_url:
                stop_reason = f"다음 링크를 찾지 못해 {index}화에서 종료"
            url = next_url
        else:
            if url and len(chapters) >= cfg.max_chapters:
                stop_reason = f"최대 회차 수({cfg.max_chapters})에 도달"

    if not chapters:
        raise RuntimeError("수집한 회차가 없습니다.")
    log(f"[green]{stop_reason}[/green] — 총 {len(chapters)}화")
    _apply_webtoon_merge(chapters, cfg, log)

    out_path = _auto_output(cfg, chapters)  # 기본 이름('이미지북')이면 수집한 제목으로
    tmp = job_dir / ".tmp"
    if cfg.split:
        out_dir = out_path.with_suffix("") if out_path.suffix.lower() in (".pdf", ".epub", ".jpg", ".webp") else out_path
        out_dir.mkdir(parents=True, exist_ok=True)
        for ch in chapters:
            _write_outputs([ch], cfg, out_dir / f"{ch.index:03d}_{_safe_name(ch.title)}", tmp, log)
        log(f"[bold green]완료:[/bold green] {out_dir} ({len(chapters)}개 회차)")
        result = out_dir
    else:
        result = _write_outputs(chapters, cfg, out_path, tmp, log)

    if not cfg.keep_images:
        shutil.rmtree(job_dir, ignore_errors=True)
    else:
        shutil.rmtree(tmp, ignore_errors=True)
        log(f"원본 이미지 보관: {job_dir}")
    return result


@app.command()
def grab(
    url: str = typer.Argument(..., help="페이지 URL, 저장한 .html/.mhtml 파일, 또는 이미지 폴더. 파일 여러 개는 '|'로 구분"),
    output: Path = typer.Option(Path("output.pdf"), "-o", "--output", help="결과 파일 경로 (--split이면 폴더). 확장자는 --format에 맞게 바뀜"),
    fmt: str = typer.Option("pdf", "--format", help="pdf | epub(아이폰·아이패드 도서(Books) 앱) | both | jpg | webp(낱장 이미지 폴더)"),
    group: int = typer.Option(0, "--group", min=0, help="저장 파일 여러 개를 넣을 때 N개씩 나눠 여러 권으로 (0 = 한 권)"),
    group_by_cover: bool = typer.Option(False, "--group-by-cover", help="회차 첫 장(표지)이 바뀔 때마다 새 권으로 나눔. 표지의 'N권' 글자를 읽어 권 번호로 씀"),
    first_volume: int = typer.Option(1, "--first-volume", min=1, help="권 번호를 표지에서 못 읽을 때 첫 묶음에 붙일 권 번호"),
    volumes: Optional[str] = typer.Option(None, "--volumes", help="만들 권 범위, 예: 11-20 (비우면 전부)"),
    episodes: Optional[str] = typer.Option(None, "--episodes", help="읽을 회차 범위, 예: 100-190 (파일명의 N화 기준)"),
    title: Optional[str] = typer.Option(None, "--title", help="책 제목 (기본: 파일 이름 또는 페이지 제목)"),
    author: Optional[str] = typer.Option(None, "--author", help="저자 (도서 앱 등에 표시)"),
    rtl: bool = typer.Option(True, "--rtl/--ltr", help="EPUB 읽는 방향: --rtl 오른쪽→왼쪽(일본 만화, 기본) / --ltr 왼쪽→오른쪽(웹툰)"),
    max_chapters: int = typer.Option(50, "--max-chapters", min=1, help="최대 회차(페이지) 수"),
    min_width: int = typer.Option(300, "--min-width", help="포함할 최소 이미지 폭(px, 표시 폭과 원본 폭 중 큰 값)"),
    mode: str = typer.Option("auto", "--mode", help="auto | all(큰 이미지 전부) | webtoon(본문 구간만)"),
    delay: float = typer.Option(0.3, "--delay", help="이미지 요청 간격(초)"),
    headed: bool = typer.Option(False, "--headed", help="브라우저 창을 띄워서 진행"),
    cookies: Optional[Path] = typer.Option(None, "--cookies", help="login 명령으로 저장한 상태 파일"),
    next_selector: Optional[str] = typer.Option(None, "--next-selector", help="다음 화 링크 CSS selector"),
    split: bool = typer.Option(False, "--split", help="회차별로 PDF를 따로 저장"),
    webtoon_merge: bool = typer.Option(False, "--webtoon-merge", help="웹툰: 작게 잘린 조각을 긴 한 쪽으로 이어 붙여 끊김 줄임"),
    webtoon_page_px: int = typer.Option(0, "--webtoon-page-px", help="웹툰 이어붙이기 한 쪽 최대 높이(px). 0=자동(최대)"),
    keep_images: bool = typer.Option(False, "--keep-images", help="원본 이미지를 삭제하지 않음"),
    fit_width: bool = typer.Option(False, "--fit-width", help="가장 흔한 폭에 맞춰 리사이즈"),
    work_dir: Path = typer.Option(Path("work"), "--work-dir", help="중간 파일 폴더"),
    scroll_wait: float = typer.Option(0.4, "--scroll-wait", help="스크롤 스텝 간 대기(초)"),
    timeout: int = typer.Option(30, "--timeout", help="페이지 로드 제한(초)"),
) -> None:
    """URL에서 시작해 '다음 화'를 따라가며 이미지를 모아 PDF로 만듭니다."""
    cfg = JobConfig(
        start_url=url, output=output, max_chapters=max_chapters, min_width=min_width, mode=mode, delay=delay,
        headed=headed, cookies=cookies, next_selector=next_selector, split=split,
        webtoon_merge=webtoon_merge, webtoon_page_px=webtoon_page_px,
        keep_images=keep_images, fit_width=fit_width, work_dir=work_dir, scroll_wait=scroll_wait,
        timeout_ms=timeout * 1000, format=fmt.lower(), title=title, author=author, group=group,
        group_by_cover=group_by_cover, rtl=rtl, first_volume=first_volume,
        volume_range=parse_range(volumes) if volumes else None,
        episode_range=parse_range(episodes) if episodes else None,
    )
    try:
        run(cfg)
    except RuntimeError as e:
        _log(f"[red]{e}[/red]")
        raise typer.Exit(code=1)


@app.command()
def login(
    url: str = typer.Argument(..., help="로그인 페이지 또는 사이트 URL"),
    output: Path = typer.Option(Path("storage_state.json"), "-o", "--output", help="저장할 상태 파일"),
) -> None:
    """브라우저를 띄워 직접 로그인한 뒤 세션을 파일로 저장합니다. (grab --cookies 로 사용)"""
    br.save_login_state(url, output)
    _log(f"[green]저장됨:[/green] {output}")


if __name__ == "__main__":
    app()
