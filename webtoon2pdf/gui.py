"""webtoon2pdf 창 프로그램 (Tkinter). 주소 붙여넣기 → 변환 → PDF 열기."""
from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
import webbrowser
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from tkinter.scrolledtext import ScrolledText

from rich.text import Text

from . import browser as br
from .cli import (BATCH_PREFIX_RE, ChallengeRequired, _natural_key, analyze_volumes, parse_range,
                  resolve_inputs, run, saved_page_title, saved_pages_in)
from .models import JobConfig

DESKTOP = Path.home() / "Desktop"
if not DESKTOP.exists():
    DESKTOP = Path.home()
DOWNLOADS = Path.home() / "Downloads"
# 중간 파일(work/·로그·세션)을 쓰는 기준 폴더. .exe로 실행하면 cwd가 불안정하므로 실행 파일 옆으로 둔다.
APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
# 크롬 확장(Alt+Shift+S)이 .mhtml을 저장하는 폴더. 확장은 다운로드 폴더 아래에만 저장할 수 있다.
SAVED_DIR = DOWNLOADS / "webtoon2pdf"
SAVED_PAGE_SUFFIXES = (".mhtml", ".mht", ".html", ".htm")
HOTKEY = "Alt+Shift+S"

# 사용자의 실제 크롬(확장이 깔린 프로필)을 찾을 후보 경로
_CHROME_CANDIDATES = [
    Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "Google/Chrome/Application/chrome.exe",
]


def _chrome_exe() -> Path | None:
    for p in _CHROME_CANDIDATES:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def _open_in_chrome(url: str) -> bool:
    """사용자의 크롬에서 url(또는 chrome://…)을 연다. 크롬을 찾아 열었으면 True, 못 찾아 기본 브라우저로 열었으면 False."""
    exe = _chrome_exe()
    if exe is not None:
        try:
            subprocess.Popen([str(exe), url])
            return True
        except OSError:
            pass
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    return False


def _ext_dir() -> Path | None:
    """번들된(또는 개발 중 레포의) chrome-extension 폴더."""
    for cand in (APP_DIR / "chrome-extension", Path(__file__).resolve().parent.parent / "chrome-extension"):
        try:
            if cand.is_dir():
                return cand
        except OSError:
            continue
    return None


def new_saved_files(started_at: float) -> list[Path]:
    """started_at(초) 이후에 SAVED_DIR에 새로 생긴 저장 페이지 파일들(회차 순). '이번에 모은 것'만 고르는 용도."""
    out: list[Path] = []
    try:
        for f in SAVED_DIR.iterdir():
            if (f.is_file() and f.suffix.lower() in SAVED_PAGE_SUFFIXES
                    and f.stat().st_mtime >= started_at - 1):
                out.append(f)
    except OSError:
        pass
    return sorted(out, key=_natural_key)


def find_latest_saved_page(dirs: list[Path]) -> Path | None:
    """주어진 폴더들(재귀 없음)에서 저장된 웹페이지 파일 중 수정 시각이 가장 최근인 것."""
    best: Path | None = None
    best_mtime = -1.0
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if not f.is_file() or f.suffix.lower() not in SAVED_PAGE_SUFFIXES:
                    continue
                m = f.stat().st_mtime
                if m > best_mtime:
                    best, best_mtime = f, m
        except OSError:
            continue
    return best


def latest_saved_batch(dirs: list[Path]) -> list[Path]:
    """가장 최근 저장 파일이 연속 저장 배치(B<시각>_<번호>_<제목>)의 일부면 그 배치 전체(회차 순),
    아니면 그 파일 하나. 없으면 []."""
    latest = find_latest_saved_page(dirs)
    if latest is None:
        return []
    m = BATCH_PREFIX_RE.match(latest.stem)
    if not m:
        return [latest]
    prefix = "B" + m.group(1) + "_"
    try:
        files = [f for f in latest.parent.iterdir()
                 if f.is_file() and f.name.startswith(prefix) and f.suffix.lower() in SAVED_PAGE_SUFFIXES]
    except OSError:
        return [latest]
    return sorted(files, key=_natural_key) or [latest]


def _display_stem(p: Path) -> str:
    """파일명에서 배치 접두(B…_NNN_)와 저장 시각을 뗀 제목."""
    return saved_page_title(p.stem)


def _episode_of(line: str) -> tuple[int, str]:
    """알림 항목을 회차 번호 오름차순으로 정렬하기 위한 키. 번호가 없으면 맨 뒤."""
    m = re.search(r"(\d{1,4})\s*화", line)
    return (int(m.group(1)) if m else 10**9, line)


def _plain(msg: str) -> str:
    try:
        return Text.from_markup(msg).plain
    except Exception:  # noqa: BLE001
        return msg


def _open_path(p: Path) -> None:
    try:
        os.startfile(str(p))  # type: ignore[attr-defined]
    except AttributeError:
        subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", str(p)])


def _version_suffix() -> str:
    """제목에 붙일 현재 버전(예: '  v202610071530'). VERSION 파일이 없으면 빈 문자열."""
    try:
        v = (APP_DIR / "VERSION").read_text(encoding="utf-8").strip()
        return f"  v{v}" if v else ""
    except OSError:
        return ""


class _Tip:
    """위젯에 마우스를 올리면 짧은 설명을 작은 말풍선으로 보여준다."""
    def __init__(self, widget: tk.Widget, text: str) -> None:
        self.widget, self.text, self.tip = widget, text, None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, _e: object = None) -> None:
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tk.Label(tw, text=self.text, justify="left", bg="#fffbe6", relief="solid", borderwidth=1,
                 font=("맑은 고딕", 9), padx=6, pady=3, wraplength=380).pack()

    def _hide(self, _e: object = None) -> None:
        if self.tip:
            self.tip.destroy()
            self.tip = None


def _tip(widget: tk.Widget, text: str) -> None:
    _Tip(widget, text)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"웹페이지 이미지 → PDF 변환기{_version_suffix()}")
        self.minsize(720, 600)
        self.geometry("780x680")
        self.q: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.result: Path | None = None
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)

    # ---------- UI ----------
    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}
        self.option_add("*Font", ("맑은 고딕", 10))
        style = ttk.Style(self)
        style.configure("Section.TLabelframe.Label", font=("맑은 고딕", 11, "bold"))
        style.configure("Hint.TLabel", foreground="#888")
        style.configure("Preset.TButton", padding=(10, 6))

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        # ── 빠른 설정(프리셋) ──
        pre = ttk.Frame(root)
        pre.pack(fill="x", pady=(0, 8))
        ttk.Label(pre, text="빠른 설정", font=("맑은 고딕", 10, "bold")).pack(side="left", padx=(0, 8))
        b_wt = ttk.Button(pre, text="웹툰용", style="Preset.TButton", command=self._preset_webtoon)
        b_wt.pack(side="left")
        b_mg = ttk.Button(pre, text="일본 만화책용", style="Preset.TButton", command=self._preset_manga)
        b_mg.pack(side="left", padx=(6, 0))
        ttk.Label(pre, text="  자주 쓰는 설정을 한 번에 맞춰줍니다", style="Hint.TLabel").pack(side="left")
        _tip(b_wt, "형식 PDF · 읽는 방향 왼→오 · 이미지 자동 · '웹툰처럼 길게 이어붙이기' 켜기")
        _tip(b_mg, "형식 EPUB · 읽는 방향 오른→왼 · 파일 묶기 '표지가 바뀔 때마다'")

        # ── ① 변환할 것 넣기 ──
        sec1 = ttk.LabelFrame(root, text=" ① 변환할 것 넣기 ", style="Section.TLabelframe", padding=8)
        sec1.pack(fill="x", pady=(0, 6))
        sec1.columnconfigure(0, weight=1)
        self.url = tk.StringVar()
        ttk.Entry(sec1, textvariable=self.url).grid(row=0, column=0, sticky="ew", pady=(0, 6))
        r1 = ttk.Frame(sec1); r1.grid(row=1, column=0, sticky="w")
        ttk.Button(r1, text="붙여넣기", command=self._paste).pack(side="left")
        ttk.Button(r1, text="최근 저장 파일", command=self._use_latest_saved).pack(side="left", padx=(4, 0))
        ttk.Button(r1, text="파일 열기", command=self._browse_html).pack(side="left", padx=(4, 0))
        ttk.Button(r1, text="폴더 열기", command=self._browse_folder).pack(side="left", padx=(4, 0))
        r2 = ttk.Frame(sec1); r2.grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Label(r2, text="크롬 연동:", style="Hint.TLabel").pack(side="left", padx=(0, 4))
        ttk.Button(r2, text="크롬에서 모으기", command=self._collect_in_chrome).pack(side="left")
        ttk.Button(r2, text="권 분석", command=self._analyze_clicked).pack(side="left", padx=(4, 0))
        ttk.Button(r2, text="크롬 확장 설치", command=self._install_extension).pack(side="left", padx=(4, 0))

        # ── ② 어떻게 만들까 ──
        sec2 = ttk.LabelFrame(root, text=" ② 어떻게 만들까 ", style="Section.TLabelframe", padding=8)
        sec2.pack(fill="x", pady=(0, 6))
        sec2.columnconfigure(1, weight=1)
        fr = ttk.Frame(sec2); fr.grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))
        flbl = ttk.Label(fr, text="형식", width=9); flbl.pack(side="left")
        self.format = tk.StringVar(value="epub")
        for text, val in (("EPUB (아이폰 도서앱)", "epub"), ("PDF", "pdf"), ("둘 다", "both"),
                          ("JPG 이미지", "jpg"), ("WEBP 이미지", "webp")):
            ttk.Radiobutton(fr, text=text, value=val, variable=self.format,
                            command=self._sync_output_ext).pack(side="left", padx=(6, 0))
        _tip(flbl, "EPUB=아이폰 '도서' 앱 / PDF=스크롤 보기 좋음(웹툰) / JPG·WEBP=낱장 이미지 폴더")
        dr = ttk.Frame(sec2); dr.grid(row=1, column=0, columnspan=3, sticky="w", pady=(0, 2))
        dlbl = ttk.Label(dr, text="읽는 방향", width=9); dlbl.pack(side="left")
        self.direction = tk.StringVar(value="rtl")
        for text, val in (("오른쪽→왼쪽 (일본 만화)", "rtl"), ("왼쪽→오른쪽 (웹툰)", "ltr")):
            ttk.Radiobutton(dr, text=text, value=val, variable=self.direction).pack(side="left", padx=(6, 0))
        ttk.Label(dr, text="  (EPUB에서만 적용)", style="Hint.TLabel").pack(side="left")
        _tip(dlbl, "EPUB에서만 적용. 웹툰=왼→오, 일본 만화책=오른→왼. PDF·JPG·WEBP는 영향 없음")
        mr = ttk.Frame(sec2); mr.grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 4))
        mlbl = ttk.Label(mr, text="이미지 선택", width=9); mlbl.pack(side="left")
        self.mode = tk.StringVar(value="auto")
        for text, val in (("자동", "auto"), ("큰 이미지 전부", "all"), ("웹툰 본문만", "webtoon")):
            ttk.Radiobutton(mr, text=text, value=val, variable=self.mode).pack(side="left", padx=(6, 0))
        _tip(mlbl, "자동=알아서(보통 이거). 큰 이미지 전부=본문이 빠질 때. 웹툰 본문만=광고가 섞일 때")
        ttk.Label(sec2, text="저장 위치").grid(row=3, column=0, sticky="w", pady=(4, 0))
        self.output = tk.StringVar(value=str(DESKTOP / "이미지북.epub"))
        ttk.Entry(sec2, textvariable=self.output).grid(row=3, column=1, sticky="ew", padx=(6, 6), pady=(4, 0))
        ttk.Button(sec2, text="찾아보기", command=self._browse_output).grid(row=3, column=2, sticky="e", pady=(4, 0))

        # ── ③ 여러 편·권 묶기 ──
        sec3 = ttk.LabelFrame(root, text=" ③ 여러 편·권 묶기 ", style="Section.TLabelframe", padding=8)
        sec3.pack(fill="x", pady=(0, 6))
        nr = ttk.Frame(sec3); nr.pack(fill="x", pady=(0, 2))
        ttk.Label(nr, text="'다음' 링크 따라가기 최대").pack(side="left")
        self.max_chapters = tk.IntVar(value=1)
        sp = ttk.Spinbox(nr, from_=1, to=999, width=5, textvariable=self.max_chapters)
        sp.pack(side="left", padx=(4, 2))
        ttk.Label(nr, text="편 (1 = 이 화만)").pack(side="left", padx=(0, 16))
        self.headed = tk.BooleanVar(value=False)
        hb = ttk.Checkbutton(nr, text="브라우저 창 보기", variable=self.headed); hb.pack(side="left")
        _tip(sp, "주소로 받을 때 '다음화'를 자동으로 따라가며 최대 N편 연속 수집. 1 = 이 화만")
        _tip(hb, "주소로 받을 때 크롬 창을 띄움. 보안확인(사람 확인)이 뜨면 그 창에서 직접 통과")
        wr = ttk.Frame(sec3); wr.pack(fill="x", pady=(0, 2))
        self.webtoon_merge = tk.BooleanVar(value=False)
        wb = ttk.Checkbutton(wr, text="웹툰처럼 길게 이어붙이기 (끊김 줄이기)", variable=self.webtoon_merge)
        wb.pack(side="left")
        _tip(wb, "웹툰 전용. 작게 잘린 조각을 긴 한 쪽으로 묶어 끊김을 줄임(장면 사이 빈 곳에서 자름)")
        self.split = tk.BooleanVar(value=False)
        sb = ttk.Checkbutton(wr, text="회차(화)마다 파일 따로", variable=self.split)
        sb.pack(side="left", padx=(16, 0))
        _tip(sb, "여러 편을 받을 때 1화, 2화 … 처럼 화마다 파일을 따로 저장(제목은 '작품명 N화'). "
                 "끄면 한 파일에 전부 합쳐짐")
        gr = ttk.Frame(sec3); gr.pack(fill="x", pady=(2, 0))
        glbl = ttk.Label(gr, text="파일 여러 개 묶기:"); glbl.pack(side="left")
        self.group_mode = tk.StringVar(value="cover")
        ttk.Radiobutton(gr, text="표지가 바뀔 때마다 새 권", value="cover", variable=self.group_mode).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(gr, text="전부 한 권", value="all", variable=self.group_mode).pack(side="left", padx=(6, 0))
        ttk.Radiobutton(gr, text="", value="count", variable=self.group_mode).pack(side="left", padx=(6, 0))
        self.group = tk.IntVar(value=7)
        ttk.Spinbox(gr, from_=1, to=99, width=4, textvariable=self.group).pack(side="left")
        ttk.Label(gr, text="개씩 한 권").pack(side="left", padx=(2, 0))
        ttk.Label(gr, text="  첫 권 번호").pack(side="left")
        self.first_volume = tk.IntVar(value=1)
        ttk.Spinbox(gr, from_=1, to=999, width=4, textvariable=self.first_volume).pack(side="left", padx=(4, 0))
        _tip(glbl, "여러 편/파일을 권으로 묶기. 표지로 자동 / 전부 한 권 / N개씩(표지 없을 때 '첫 권 번호'부터)")

        # ── 고급 설정 (접이식) — 평소 안 쓰는 옵션 ──
        self.adv_open = tk.BooleanVar(value=False)
        ttk.Checkbutton(root, text="고급 설정 (저자·제목 · 권/화 범위 · 세부 옵션)", variable=self.adv_open,
                        command=self._toggle_adv, style="Toolbutton").pack(anchor="w", pady=(2, 0))
        self.adv = ttk.Frame(root)
        self.adv.columnconfigure(1, weight=1)
        am = ttk.Frame(self.adv); am.grid(row=0, column=0, columnspan=3, sticky="ew", **pad)
        am.columnconfigure(1, weight=1); am.columnconfigure(3, weight=1)
        ttk.Label(am, text="저자").grid(row=0, column=0, sticky="w")
        self.author = tk.StringVar()
        ttk.Entry(am, textvariable=self.author).grid(row=0, column=1, sticky="ew", padx=(6, 16))
        ttk.Label(am, text="책 제목 (비우면 자동)").grid(row=0, column=2, sticky="w")
        self.book_title = tk.StringVar()
        ttk.Entry(am, textvariable=self.book_title).grid(row=0, column=3, sticky="ew", padx=(6, 0))
        rr = ttk.Frame(self.adv); rr.grid(row=1, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(rr, text="만들 권").pack(side="left")
        self.vol_from = tk.StringVar(); self.vol_to = tk.StringVar()
        ttk.Entry(rr, textvariable=self.vol_from, width=5).pack(side="left", padx=(4, 0))
        ttk.Label(rr, text="~").pack(side="left", padx=2)
        ttk.Entry(rr, textvariable=self.vol_to, width=5).pack(side="left")
        ttk.Label(rr, text="권 (비우면 전부)").pack(side="left", padx=(4, 16))
        ttk.Label(rr, text="읽을 화").pack(side="left")
        self.ep_from = tk.StringVar(); self.ep_to = tk.StringVar()
        ttk.Entry(rr, textvariable=self.ep_from, width=5).pack(side="left", padx=(4, 0))
        ttk.Label(rr, text="~").pack(side="left", padx=2)
        ttk.Entry(rr, textvariable=self.ep_to, width=5).pack(side="left")
        ttk.Label(rr, text="화 (비우면 전부)").pack(side="left", padx=(4, 0))
        ttk.Label(self.adv, text="다음 페이지 selector").grid(row=3, column=0, sticky="w", **pad)
        self.next_selector = tk.StringVar()
        ttk.Entry(self.adv, textvariable=self.next_selector).grid(row=3, column=1, columnspan=2, sticky="ew", **pad)
        ttk.Label(self.adv, text="최소 이미지 폭(px)").grid(row=4, column=0, sticky="w", **pad)
        self.min_width = tk.IntVar(value=300)
        ttk.Spinbox(self.adv, from_=50, to=3000, increment=50, width=7, textvariable=self.min_width).grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(self.adv, text="요청 간격(초)").grid(row=5, column=0, sticky="w", **pad)
        self.delay = tk.DoubleVar(value=0.3)
        ttk.Spinbox(self.adv, from_=0, to=10, increment=0.1, width=7, textvariable=self.delay).grid(row=5, column=1, sticky="w", **pad)
        ttk.Label(self.adv, text="로그인 세션 파일").grid(row=6, column=0, sticky="w", **pad)
        self.cookies = tk.StringVar()
        ttk.Entry(self.adv, textvariable=self.cookies).grid(row=6, column=1, sticky="ew", **pad)
        cbtn = ttk.Frame(self.adv); cbtn.grid(row=6, column=2, **pad)
        ttk.Button(cbtn, text="찾기", command=self._browse_cookies).pack(side="left")
        ttk.Button(cbtn, text="로그인해서 만들기", command=self._make_login).pack(side="left", padx=(4, 0))

        # ── 변환 시작 · 진행 · 기록 ──
        self.start_btn = tk.Button(root, text="변환 시작", command=self._start,
                                   bg="#2e7d32", fg="white", activebackground="#1b5e20", activeforeground="white",
                                   font=("맑은 고딕", 13, "bold"), relief="flat", padx=10, pady=9, cursor="hand2")
        self.start_btn.pack(fill="x", pady=(10, 4))
        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.pack(fill="x")

        strow = ttk.Frame(root); strow.pack(fill="x", pady=(4, 0))
        self.log_btn = ttk.Button(strow, text="자세히 보기 ▾", command=self._toggle_log, style="Toolbutton")
        self.log_btn.pack(side="right")
        self.status = ttk.Label(strow, text="준비됨 — 빠른 설정을 고르고 [변환 시작]을 누르세요.")
        self.status.pack(side="left")

        # 진행 내역(기록) — 평소엔 접어둠. [자세히 보기]로 펼침.
        self.log = ScrolledText(root, height=12, state="disabled", wrap="word",
                                font=("Consolas", 10), bg="#141414", fg="#e6e6e6", insertbackground="#e6e6e6")
        self.log.tag_config("warn", foreground="#ffd166")
        self.log.tag_config("error", foreground="#ff6b6b")
        self.log.tag_config("ok", foreground="#8ce99a")
        self.log.tag_config("sep", foreground="#7aa2f7")
        self.issues: list[str] = []
        self._log_shown = False

        self._bottom = ttk.Frame(root)
        self._bottom.pack(fill="x", pady=(6, 0))
        ttk.Button(self._bottom, text="❓ 도움말", command=self._show_help).pack(side="left")
        self.open_dir_btn = ttk.Button(self._bottom, text="폴더 열기", state="disabled", command=self._open_dir)
        self.open_dir_btn.pack(side="right")
        self.open_btn = ttk.Button(self._bottom, text="결과 열기", state="disabled", command=self._open_result)
        self.open_btn.pack(side="right", padx=(0, 6))
        ttk.Button(self._bottom, text="내역 지우기", command=self._clear_log).pack(side="right", padx=(0, 12))
        ttk.Button(self._bottom, text="업데이트 확인", command=self._check_update_now).pack(side="right", padx=(0, 12))

    @staticmethod
    def _range(v_from: tk.StringVar, v_to: tk.StringVar) -> tuple[int, int] | None:
        a, b = v_from.get().strip(), v_to.get().strip()
        if not a and not b:
            return None
        return parse_range(f"{a or '1'}-{b or '9999'}")  # 한쪽만 넣으면 '그 이상' / '그 이하'

    def _result_ext(self) -> str:
        """결과 파일 확장자. JPG/WEBP는 폴더로 저장하므로 확장자 없음("")."""
        f = self.format.get()
        if f in ("jpg", "webp"):
            return ""
        return ".pdf" if f == "pdf" else ".epub"

    def _sync_output_ext(self) -> None:
        p = Path(self.output.get().strip() or str(DESKTOP / "이미지북"))
        if p.suffix.lower() in (".pdf", ".epub", ".jpg", ".webp", ""):
            ext = self._result_ext()
            self.output.set(str(p.with_suffix(ext)))  # ext="" → 폴더(확장자 제거)

    def _toggle_adv(self) -> None:
        if self.adv_open.get():
            self.adv.pack(fill="x", padx=2, pady=(0, 4), before=self.start_btn)
        else:
            self.adv.pack_forget()

    def _show_log(self) -> None:
        if not getattr(self, "_log_shown", False):
            self.log.pack(fill="both", expand=True, pady=(4, 0), before=self._bottom)
            self.log_btn.config(text="접기 ▴")
            self._log_shown = True

    def _toggle_log(self) -> None:
        if getattr(self, "_log_shown", False):
            self.log.pack_forget()
            self.log_btn.config(text="자세히 보기 ▾")
            self._log_shown = False
        else:
            self._show_log()

    def _preset_webtoon(self) -> None:
        """웹툰용 추천 설정을 한 번에."""
        self.format.set("pdf")
        self.direction.set("ltr")
        self.mode.set("auto")
        self.webtoon_merge.set(True)
        self.split.set(True)   # 웹툰은 화마다 따로 저장(1화, 2화 …)
        self.headed.set(True)  # 웹툰 사이트(뉴토끼 등)는 봇 차단이 많아 '브라우저 창 보기'가 필요
        self._sync_output_ext()
        self.status.config(text="웹툰용 설정 적용: PDF · 왼→오 · 자동 · 길게 이어붙이기 · 화마다 따로 · 브라우저 창 보기 켬")

    def _preset_manga(self) -> None:
        """일본 만화책용 추천 설정을 한 번에."""
        self.format.set("epub")
        self.direction.set("rtl")
        self.mode.set("auto")
        self.webtoon_merge.set(False)
        self.split.set(False)  # 만화는 권으로 묶으므로 화별 분리는 끔
        self.group_mode.set("cover")
        self._sync_output_ext()
        self.status.config(text="일본 만화책용 설정 적용: EPUB · 오른→왼 · 표지가 바뀔 때마다 새 권")

    # ---------- 동작 ----------
    def _paste(self) -> None:
        try:
            self.url.set(self.clipboard_get().strip())
        except tk.TclError:
            pass

    def _browse_html(self) -> None:
        initial = SAVED_DIR if SAVED_DIR.is_dir() else (DOWNLOADS if DOWNLOADS.is_dir() else DESKTOP)
        paths = filedialog.askopenfilenames(
            title=f"크롬 확장({HOTKEY}) 또는 Ctrl+S로 저장한 웹페이지 파일 (여러 개: Ctrl/Shift 누른 채 클릭)",
            initialdir=str(initial),
            filetypes=[("웹페이지", "*.mhtml *.mht *.html *.htm"), ("모든 파일", "*.*")],
        )
        if not paths:
            return
        self.status.config(text=f"파일 {len(paths)}개 불러오는 중…")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        self.update_idletasks()
        paths = sorted(paths, key=lambda p: _natural_key(Path(p)))  # 배치 접두·저장 시각 무시, 회차 번호 순
        self.url.set(" | ".join(paths))
        self.progress.stop()
        if len(paths) > 1:
            first, last = _display_stem(Path(paths[0])), _display_stem(Path(paths[-1]))
            ext = self._result_ext()
            self.output.set(str(Path(self.output.get() or str(DESKTOP)).parent / f"{first} ~ {last}{ext}"))
            self.status.config(text=f"파일 {len(paths)}개 선택: {first} … {last} (이름 순서대로 묶음)")

    def _use_latest_saved(self) -> None:
        """크롬 확장이 저장한 폴더(없으면 다운로드·바탕화면)에서 가장 최근 웹페이지 파일을 고른다.
        연속 저장 배치의 일부면 배치 전체를 이름순으로 넣어 한 권으로 묶는다."""
        files = latest_saved_batch([SAVED_DIR, DOWNLOADS, DESKTOP])
        if not files:
            messagebox.showinfo(
                "저장된 파일 없음",
                f"크롬에서 {HOTKEY}로 저장한 파일이 없습니다.\n\n"
                f"찾은 위치: {SAVED_DIR}\n"
                "확장 프로그램 설치 방법은 README의 '크롬 확장 설치'를 보세요.",
            )
            return
        self.max_chapters.set(1)
        if len(files) == 1:
            p = files[0]
            self.url.set(str(p))
            when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            self._append_log(f"최근 저장 파일: {p.name}  ({when}, {p.stat().st_size / 1048576:.1f} MB)")
            self.status.config(text=f"{p.name} 선택됨. [변환 시작]을 누르세요.")
            return
        self.url.set(" | ".join(str(f) for f in files))
        first, last = _display_stem(files[0]), _display_stem(files[-1])
        ext = self._result_ext()
        self.output.set(str(Path(self.output.get() or str(DESKTOP)).parent / f"{first} ~ {last}{ext}"))
        total = sum(f.stat().st_size for f in files) / 1048576
        self._append_log(f"최근 연속 저장 배치: {len(files)}개 ({first} … {last}, {total:.1f} MB)")
        self.status.config(text=f"파일 {len(files)}개 선택: {first} … {last} (연속 저장 배치, 이름 순으로 묶음)")

    def _browse_folder(self) -> None:
        initial = SAVED_DIR if SAVED_DIR.is_dir() else (DOWNLOADS if DOWNLOADS.is_dir() else DESKTOP)
        p = filedialog.askdirectory(title="저장한 페이지(.mhtml/.html)들이 든 폴더, 또는 이미지 폴더", initialdir=str(initial))
        if not p:
            return
        self.url.set(p)
        pages = saved_pages_in(Path(p))
        if pages:
            first, last = _display_stem(pages[0]), _display_stem(pages[-1])
            ext = self._result_ext()
            self.output.set(str(Path(self.output.get() or str(DESKTOP)).parent / f"{first} ~ {last}{ext}"))
            self.status.config(text=f"폴더 안 저장 페이지 {len(pages)}개: {first} … {last} (이름 순서대로 묶음)")
        else:
            self.status.config(text="폴더 안 이미지 파일을 이름 순서대로 한 권으로 묶습니다.")

    def _browse_output(self) -> None:
        if self.format.get() in ("jpg", "webp"):
            # 이미지 형식은 폴더로 저장 → 저장할 "상위 폴더"를 고르고 그 안에 이름 폴더를 만든다
            cur = Path(self.output.get() or str(DESKTOP / "이미지북"))
            d = filedialog.askdirectory(title="이미지를 저장할 상위 폴더", initialdir=str(cur.parent))
            if d:
                self.output.set(str(Path(d) / (cur.name or "이미지북")))
            return
        ext = self._result_ext()
        p = filedialog.asksaveasfilename(defaultextension=ext, filetypes=[("EPUB", "*.epub"), ("PDF", "*.pdf")],
                                         initialfile=Path(self.output.get()).name,
                                         initialdir=str(Path(self.output.get()).parent))
        if p:
            self.output.set(p)

    def _browse_cookies(self) -> None:
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("모든 파일", "*.*")])
        if p:
            self.cookies.set(p)

    def _make_login(self) -> None:
        url = self.url.get().strip()
        if not url:
            messagebox.showwarning("주소 필요", "먼저 사이트 주소를 넣어 주세요.")
            return
        out = Path(self.cookies.get().strip() or (APP_DIR / "storage_state.json"))
        self.cookies.set(str(out))
        done = threading.Event()

        def wait() -> None:
            # 워커 스레드에서 호출됨 → 메인 스레드에 메시지 박스를 띄우고 닫힐 때까지 대기
            self.after(0, lambda: (messagebox.showinfo(
                "로그인", "브라우저 창에서 로그인을 마친 뒤 [확인]을 누르세요."), done.set()))
            done.wait()

        def job() -> None:
            try:
                br.save_login_state(url, out, wait=wait)
                self.q.put(("log", f"로그인 세션 저장됨: {out}"))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"로그인 세션 저장 실패: {e}"))

        threading.Thread(target=job, daemon=True).start()

    # ---------- 크롬 연동: 모으기 → 분석 → 불러오기 (변환은 사용자가) ----------
    def _install_extension(self) -> None:
        ext = _ext_dir()
        if ext is None:
            messagebox.showwarning("확장 폴더 없음",
                                   "chrome-extension 폴더를 찾지 못했습니다.\n앱 폴더 안에 있는지 확인해 주세요.")
            return
        _open_path(ext)
        _open_in_chrome("chrome://extensions")
        messagebox.showinfo(
            "크롬 확장 설치",
            "크롬에 확장을 한 번만 설치하면 됩니다.\n\n"
            "1) 방금 열린 크롬의 'chrome://extensions' 화면에서\n"
            "   오른쪽 위 '개발자 모드'를 켜세요.\n"
            "2) '압축해제된 확장 프로그램을 로드합니다'를 누르세요.\n"
            f"3) 방금 열린 이 폴더를 선택하세요:\n   {ext}\n\n"
            "설치 후, 주소창 오른쪽 퍼즐 아이콘에서 고정해두면 편합니다.")

    HELP_TEXT = (
        "전자책 변환기 — 옵션 고르는 법\n"
        "════════════════════════════════\n\n"

        "■ 빠른 추천 (이대로만 해도 됨)\n"
        "  • 웹툰(세로 스크롤): 형식 PDF · 읽는 방향 '왼쪽→오른쪽' · 이미지 '자동'\n"
        "      · '웹툰처럼 길게 이어붙이기' 켜기\n"
        "  • 일본 만화책(저장 파일): 형식 EPUB · 읽는 방향 '오른쪽→왼쪽'\n"
        "      · 파일 여러 개 묶기 '표지가 바뀔 때마다 새 권'\n\n"

        "■ 형식 (무엇으로 만드나)\n"
        "  • EPUB : 아이폰·아이패드 '도서(Books)' 앱용. 만화책에 적합.\n"
        "  • PDF  : 어디서나 열림. 웹툰(세로 스크롤) 보기에 편함.\n"
        "  • JPG / WEBP : 책이 아니라 '낱장 이미지 폴더'로 저장(0001.jpg …).\n"
        "  • 둘 다 : EPUB과 PDF를 같이 만듦.\n\n"

        "■ 읽는 방향  (EPUB에서만 적용 — PDF·JPG·WEBP는 영향 없음)\n"
        "  • 왼쪽→오른쪽 (웹툰) : 웹툰·보통 책. 웹툰이면 이거.\n"
        "  • 오른쪽→왼쪽 : 일본 만화책 전용(오른쪽 페이지부터 넘김).\n\n"

        "■ 이미지 선택  (페이지에서 어떤 그림을 담을지)\n"
        "  • 자동 : 알아서 고름. 보통 이거면 충분.\n"
        "  • 큰 이미지 전부 : 큰 이미지를 다 담음(광고가 섞일 수 있음).\n"
        "       → 본문 그림이 빠질 때 사용.\n"
        "  • 웹툰 본문만 : 본문 구간만, 양끝 광고·안내를 떼어냄.\n"
        "       → 광고·잡것이 섞여 들어올 때 사용.\n\n"

        "■ '다음' 링크 따라가기 최대 N 페이지  (여러 편 자동 수집)\n"
        "  • 1  = 넣은 주소의 '이 한 편'만.\n"
        "  • 10 = 다음화를 자동으로 따라가며 최대 10편 연속 수집.\n"
        "  • 마지막 화(다음 링크 없음)에 닿으면 거기서 멈춤. 같은 사이트 안에서만.\n\n"

        "■ 웹툰처럼 길게 이어붙이기(끊김↓)  (웹툰 전용)\n"
        "  • 작게 잘린 조각들을 '긴 한 쪽'으로 묶어 끊김을 줄임.\n"
        "  • 자르는 곳은 장면 사이 빈 구간(여백)이라 그림을 가로지르지 않음.\n"
        "  • 웹툰일 때만 켜기. 일반 만화(쪽 단위)엔 끄기.\n\n"

        "■ 회차(화)마다 파일 따로  (웹툰 여러 편)\n"
        "  • 1화, 2화 … 처럼 화마다 파일을 따로 저장(제목 '작품명 N화').\n"
        "  • 끄면 여러 편이 한 파일에 전부 합쳐짐('작품명 1화~5화').\n"
        "  • [웹툰용] 빠른 설정을 누르면 자동으로 켜집니다.\n\n"

        "■ 파일 여러 개 묶기  (저장한 파일·여러 편을 권으로)\n"
        "  • 표지가 바뀔 때마다 새 권 : 표지로 자동 구분(표지에 'N권'이 있으면 읽어서 씀).\n"
        "  • 전부 한 권 : 다 합쳐 한 권.\n"
        "  • N개씩 한 권 + 첫 권 번호 : 표지가 없을 때 N편씩 묶고, 번호는 '첫 권 번호'부터.\n\n"

        "■ 그 밖에\n"
        "  • 브라우저 창 보기 : 주소로 받을 때 크롬 창을 띄움. 보안확인(사람 확인)이\n"
        "       뜨면 그 창에서 직접 통과. (주소로 받을 땐 켜두는 게 안전)\n\n"

        "■ 크롬 연동 버튼 (주소로 바로 안 받아지는 사이트용)\n"
        "  • 크롬 확장 설치 : 처음 한 번만. 저장용 확장을 크롬에 설치.\n"
        "  • 크롬에서 모으기 : 크롬에서 연속 저장 → 권 분석 → 불러오기.\n"
        "  • 권 분석 : 모아둔 파일이 몇 권인지, 마지막 권이 다 모였는지 분석.\n"
        "  • 업데이트 확인 : 새 버전이 있으면 받기.\n"
    )

    def _show_help(self) -> None:
        win = tk.Toplevel(self)
        win.title("도움말 — 옵션 고르는 법")
        win.geometry("640x620")
        try:
            win.transient(self)
        except tk.TclError:
            pass
        txt = ScrolledText(win, wrap="word", font=("맑은 고딕", 10), padx=12, pady=10)
        txt.pack(fill="both", expand=True)
        txt.insert("1.0", self.HELP_TEXT)
        txt.config(state="disabled")
        ttk.Button(win, text="닫기", command=win.destroy).pack(pady=(0, 8))

    def _check_update_now(self) -> None:
        """수동 업데이트 점검. 새 버전이 있으면 받아두고, 다시 켤 때 적용된다."""
        try:
            import updater
        except Exception:  # noqa: BLE001
            messagebox.showinfo("업데이트", "이 버전에서는 업데이트 기능을 쓸 수 없습니다.")
            return
        self.status.config(text="업데이트 확인 중…")
        self.update_idletasks()
        newver = updater.update_available()
        if not newver:
            self.status.config(text="이미 최신 버전입니다.")
            messagebox.showinfo("업데이트", "이미 최신 버전입니다.")
            return
        if not messagebox.askyesno("새 버전이 있어요", "새 버전이 있습니다. 지금 받을까요?"):
            self.status.config(text="업데이트를 미뤘습니다.")
            return
        if updater.apply_update(log=lambda m: self.q.put(("log", m))):
            messagebox.showinfo("업데이트", "받았습니다.\n앱을 껐다 다시 켜면 새 버전으로 실행됩니다.")
            self.status.config(text="업데이트 받음 — 앱을 껐다 켜면 적용됩니다.")
        else:
            messagebox.showwarning("업데이트", "업데이트에 실패했습니다. (인터넷 연결을 확인해 주세요)")
            self.status.config(text="업데이트 실패")

    def _collect_in_chrome(self) -> None:
        url = self.url.get().strip().strip('"')
        if not url.lower().startswith(("http://", "https://")):
            url = (simpledialog.askstring("크롬에서 모으기",
                                          "모을 만화의 '첫 화' 주소를 붙여넣으세요:") or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            if url:
                messagebox.showwarning("주소 확인", "http:// 또는 https:// 로 시작하는 주소를 넣어 주세요.")
            return
        self.url.set(url)
        try:
            SAVED_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        started_at = time.time()
        chrome_found = _open_in_chrome(url)
        self._show_collect_dialog(started_at, chrome_found)

    def _show_collect_dialog(self, started_at: float, chrome_found: bool) -> None:
        win = tk.Toplevel(self)
        win.title("크롬에서 모으는 중")
        win.transient(self)
        try:
            win.attributes("-topmost", True)
        except tk.TclError:
            pass
        box = ttk.Frame(win, padding=14)
        box.pack(fill="both", expand=True)
        head = "크롬 창에서 이렇게 하세요" if chrome_found else \
            "크롬을 못 찾아 기본 브라우저로 열었어요 (확장은 크롬에 설치돼 있어야 합니다)"
        ttk.Label(box, text=head, font=("", 10, "bold"), wraplength=420, justify="left").pack(anchor="w")
        ttk.Label(box, justify="left", wraplength=420, text=(
            "1) 만화 '첫 화'가 열렸는지 확인하세요.\n"
            "2) 확장 아이콘을 눌러 '연속 저장 시작'을 누르세요.\n"
            "   (로그인·보안확인이 뜨면 크롬에서 직접 통과하세요)\n"
            "3) 다 모이면 아래 [다 모았어요 → 분석]을 누르세요.")
        ).pack(anchor="w", pady=(6, 10))
        count_var = tk.StringVar(value="아직 저장된 파일이 없습니다.")
        ttk.Label(box, textvariable=count_var, foreground="#2b6cb0").pack(anchor="w")
        btns = ttk.Frame(box)
        btns.pack(anchor="e", pady=(14, 0))

        state = {"n": 0, "since": started_at}

        def refresh() -> None:
            if not win.winfo_exists():
                return
            files = new_saved_files(started_at)
            n = len(files)
            if n != state["n"]:
                state["n"] = n
                state["since"] = time.time()
            if n == 0:
                count_var.set("아직 저장된 파일이 없습니다. 크롬에서 '연속 저장'을 시작하세요.")
            else:
                idle = int(time.time() - state["since"])
                tail = f" · {idle}초간 새 파일 없음" if idle >= 5 else ""
                count_var.set(f"지금까지 {n}개 저장됨 · 최근: {_display_stem(files[-1])[:24]}{tail}")
            win.after(1000, refresh)

        def done_clicked() -> None:
            files = new_saved_files(started_at)
            win.destroy()
            if not files:
                messagebox.showinfo("저장된 파일 없음",
                                    f"이번에 새로 저장된 파일이 없습니다.\n\n확인한 폴더: {SAVED_DIR}\n"
                                    "크롬에서 확장으로 저장했는지, 확장이 설치돼 있는지 확인해 주세요.")
                return
            self._analyze_and_load(files)

        ttk.Button(btns, text="다 모았어요 → 분석", command=done_clicked).pack(side="left")
        ttk.Button(btns, text="취소", command=win.destroy).pack(side="left", padx=(6, 0))
        refresh()

    def _analyze_clicked(self) -> None:
        cur = self.url.get().strip()
        parts = [p.strip().strip('"') for p in cur.split("|") if p.strip().strip('"')]
        if parts and all(Path(p).exists() for p in parts):
            self._analyze_and_load([Path(p) for p in parts])
            return
        p = filedialog.askdirectory(
            title="분석할 저장 폴더를 고르세요 (여러 번 나눠 모은 폴더)",
            initialdir=str(SAVED_DIR if SAVED_DIR.is_dir() else (DOWNLOADS if DOWNLOADS.is_dir() else DESKTOP)))
        if p:
            self._analyze_and_load([Path(p)])

    def _analyze_cfg(self) -> JobConfig | None:
        cookies = self.cookies.get().strip()
        try:
            return JobConfig(
                start_url=self.url.get().strip(),
                output=Path(self.output.get().strip() or str(DESKTOP / "이미지북.epub")),
                max_chapters=1, min_width=int(self.min_width.get()), mode=self.mode.get(),
                delay=float(self.delay.get()), headed=self.headed.get(),
                cookies=Path(cookies) if cookies else None, work_dir=APP_DIR / "work",
                group=int(self.group.get()) if self.group_mode.get() == "count" else 0,
                group_by_cover=self.group_mode.get() == "cover",
                first_volume=int(self.first_volume.get()), format=self.format.get(),
                episode_range=self._range(self.ep_from, self.ep_to),
            )
        except (tk.TclError, ValueError, RuntimeError) as e:
            messagebox.showwarning("설정 확인", f"입력값을 확인해 주세요: {e}")
            return None

    def _analyze_and_load(self, targets: list[Path]) -> None:
        if getattr(self, "worker", None) and self.worker.is_alive():
            messagebox.showinfo("잠깐만요", "앞 작업이 끝난 뒤에 다시 눌러 주세요.")
            return
        paths = sorted((Path(t) for t in targets), key=lambda p: _natural_key(p))
        joined = " | ".join(str(p) for p in paths)
        self.url.set(joined)
        try:
            inputs = resolve_inputs(joined)
        except RuntimeError as e:
            messagebox.showwarning("입력 확인", str(e))
            return
        cfg = self._analyze_cfg()
        if cfg is None:
            return
        self.result = None
        self.issues = []
        self.start_btn.config(state="disabled")
        self.open_btn.config(state="disabled")
        self.open_dir_btn.config(state="disabled")
        self._append_log(f"──── {datetime.now():%Y-%m-%d %H:%M:%S} 권 분석 시작 ────", "sep")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        self.status.config(text="권 분석 중… (파일이 많으면 1~2분)")
        self.worker = threading.Thread(target=self._analyze_job, args=(cfg, inputs), daemon=True)
        self.worker.start()

    def _analyze_job(self, cfg: JobConfig, inputs) -> None:
        try:
            report = analyze_volumes(cfg, inputs, log=lambda m: self.q.put(("log", _plain(m))),
                                     progress=lambda done, total: self.q.put(("progress", (done, total))))
            self.q.put(("analyzed", report))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", str(e)))

    def _show_report(self, report) -> None:
        self.progress.stop()
        self.progress.config(mode="determinate", value=self.progress["maximum"] or 100)
        self.start_btn.config(state="normal")
        series = report.series or "만화"
        self._append_log(f"── 권 분석 결과: {series} (총 {report.total_chapters}화, {len(report.volumes)}권) ──", "sep")
        if not report.covers_detected:
            self._append_log(
                f"표지를 못 찾아 권을 자동으로 나눌 수 없어요. '파일 여러 개 묶기 → {report.count_per_volume}개씩'과 "
                "'첫 권 번호'로 지정한 뒤 변환하세요. (아래 권 번호는 추정값)", "warn")
        for v in report.volumes:
            span = v.first_ep if v.first_ep == v.last_ep else f"{v.first_ep}~{v.last_ep}"
            if v.status == "완결":
                self._append_log(f"  {v.number}권 ({span}, {v.chapter_count}화) — 완결", "ok")
            elif v.status == "미확정":
                self._append_log(f"  {v.number}권 ({span}, {v.chapter_count}화) — 미확정: 다음 권 표지가 아직 없어요 "
                                 "(이어서 더 모으면 확정됩니다)", "warn")
            else:
                self._append_log(f"  {v.number}권(추정) ({span}, {v.chapter_count}화)")
        if report.missing_episodes:
            shown = ", ".join(f"{n}화" for n in report.missing_episodes[:15]) + (
                " …" if len(report.missing_episodes) > 15 else "")
            self._append_log(f"  빠진 화: {shown} — 크롬에서 다시 저장해 추가하세요.", "warn")
        if report.incomplete_labels:
            self._append_log(f"  저장이 덜 된 회차 {len(report.incomplete_labels)}개: "
                             + "; ".join(report.incomplete_labels[:8]), "warn")
        if report.volumes:
            first_v, last_v = report.volumes[0], report.volumes[-1]
            ext = self._result_ext()
            name = (f"{series} {first_v.number}권" if first_v.number == last_v.number
                    else f"{series} {first_v.number}~{last_v.number}권")
            self.output.set(str(Path(self.output.get() or str(DESKTOP)).parent / f"{name}{ext}"))
        self.status.config(text="분석 완료. 권 묶기 설정을 확인하고 [변환 시작]을 누르세요.")
        last = report.volumes[-1] if report.volumes else None
        lines = [f"{series}: {len(report.volumes)}권"]
        if report.volumes and report.covers_detected:
            lines[0] += f" ({report.volumes[0].number}~{report.volumes[-1].number}권)"
        if last is not None and last.status == "미확정":
            lines.append(f"\n마지막 {last.number}권은 '미확정'입니다.\n"
                         f"다음 권 표지가 아직 없어 {last.number}권 도중에 멈췄을 수 있어요.\n이어서 더 모으면 확정됩니다.")
        if not report.covers_detected:
            lines.append("\n표지가 없어 권을 자동으로 못 나눴어요.\n'N개씩'과 '첫 권 번호'로 지정한 뒤 변환하세요.")
        if report.missing_episodes:
            lines.append(f"\n빠진 화 {len(report.missing_episodes)}개가 있어요. (로그 확인)")
        lines.append("\n권 묶기 설정을 확인하고 [변환 시작]을 누르세요.")
        messagebox.showinfo("권 분석 결과", "".join(lines))

    def _start(self) -> None:
        url = self.url.get().strip().strip('"')
        parts = [p.strip().strip('"') for p in url.split("|") if p.strip()]
        if not parts or not all(p.startswith(("http://", "https://")) or Path(p).exists() for p in parts):
            messagebox.showwarning("입력 확인", "http:// 또는 https:// 로 시작하는 주소, 저장한 .html/.mhtml 파일, 또는 이미지 폴더를 넣어 주세요.")
            return
        output = Path(self.output.get().strip() or (DESKTOP / "이미지북.epub"))
        cookies = self.cookies.get().strip()
        try:
            cfg = JobConfig(
                start_url=url, output=output, max_chapters=int(self.max_chapters.get()),
                min_width=int(self.min_width.get()), mode=self.mode.get(), delay=float(self.delay.get()),
                headed=self.headed.get(), cookies=Path(cookies) if cookies else None,
                next_selector=self.next_selector.get().strip() or None, split=self.split.get(),
                webtoon_merge=self.webtoon_merge.get(),
                work_dir=APP_DIR / "work", format=self.format.get(),
                group=int(self.group.get()) if self.group_mode.get() == "count" else 0,
                group_by_cover=self.group_mode.get() == "cover",
                author=self.author.get().strip() or None, title=self.book_title.get().strip() or None,
                rtl=self.direction.get() == "rtl", first_volume=int(self.first_volume.get()),
                volume_range=self._range(self.vol_from, self.vol_to), episode_range=self._range(self.ep_from, self.ep_to),
            )
        except (tk.TclError, ValueError, RuntimeError) as e:
            messagebox.showwarning("설정 확인", f"입력값을 확인해 주세요: {e}")
            return

        self.result = None
        self.issues = []
        self.precheck_shown = False
        self.open_btn.config(state="disabled")
        self.open_dir_btn.config(state="disabled")
        self.start_btn.config(state="disabled")
        n_files = len(parts)
        self._append_log(f"──── {datetime.now():%Y-%m-%d %H:%M:%S} 변환 시작 ({n_files}개 입력, {cfg.format.upper()}) ────", "sep")
        self.progress.config(mode="indeterminate")
        self.progress.start(12)
        self.status.config(text="변환 중... (페이지가 많으면 몇 분 걸립니다)")
        self.worker = threading.Thread(target=self._job, args=(cfg,), daemon=True)
        self.worker.start()

    def _job(self, cfg: JobConfig) -> None:
        try:
            result = run(cfg, log=lambda m: self.q.put(("log", _plain(m))),
                         progress=lambda done, total: self.q.put(("progress", (done, total))),
                         ask_replacements=self._ask_replacements_from_worker)
            self.q.put(("done", result))
        except ChallengeRequired as e:
            self.q.put(("challenge", str(e)))
        except Exception as e:  # noqa: BLE001
            self.q.put(("error", str(e)))

    def _poll(self) -> None:
        # 어떤 메시지 처리에서 오류가 나도 폴링은 계속 돌아야 한다 (멈추면 창이 굳은 것처럼 보인다)
        try:
            self._poll_once()
        except Exception as e:  # noqa: BLE001
            self._append_log(f"화면 갱신 중 오류: {e!r}", "error")
        self.after(100, self._poll)

    def _poll_once(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "ask":
                    # 창을 못 띄우면 워커가 영원히 기다리므로, 실패 시 취소로 답한다
                    bad, missing, done, holder = payload
                    try:
                        self._replacement_dialog(bad, missing, done, holder)
                    except Exception as e:  # noqa: BLE001
                        self._append_log(f"점검 창을 띄우지 못했습니다: {e!r}", "error")
                        holder[0] = None
                        done.set()
                    continue
                if kind == "log":
                    self._append_log(payload, self._classify(payload))
                    head = re.match(r"^\s*(\d+)\.\s+\S+\s+(.+)$|^\s*(\d+화)\s", payload)
                    if head:  # "3. 저장한 페이지(MHTML) 파일명" / "3화 주소" → 지금 처리 중인 항목
                        self.current_item = (head.group(2) or head.group(3)).strip()[:60]
                    if self._is_issue(payload):
                        # 다운로드 단계의 메시지([건너뜀]/[대체]/[경고])에만 지금 처리 중인 회차 이름을 붙인다.
                        # 회차 이름이 이미 들어 있는 메시지(저장이 덜 된 회차 제외: …)는 그대로.
                        needs_where = payload.lstrip().startswith(("[건너뜀]", "[대체]", "[경고]"))
                        where = f"[{self.current_item}] " if needs_where and getattr(self, "current_item", "") else ""
                        self.issues.append(where + payload.strip())
                elif kind == "progress":
                    done, total = payload
                    if total > 1:
                        self.progress.stop()
                        self.progress.config(mode="determinate", maximum=total, value=done)
                        self.status.config(text=f"파일 {done}/{total} 처리 중…" if done < total else "책 만드는 중…")
                elif kind == "done":
                    self._finish(payload)
                elif kind == "analyzed":
                    self._show_report(payload)
                elif kind == "error":
                    self._fail(payload)
                elif kind == "challenge":
                    self._challenge(payload)
        except queue.Empty:
            pass

    ISSUE_KEYS = ("저장이 덜 된", "건너뜀", "실패", "중복 저장된", "찾지 못했습니다", "손상", "빠진 회차",
                  "받지 못했습니다", "장만 받았습니다")

    @classmethod
    def _is_issue(cls, line: str) -> bool:
        return any(k in line for k in cls.ISSUE_KEYS)

    @classmethod
    def _classify(cls, line: str) -> str | None:
        if "오류" in line or "실패" in line:
            return "error"
        if cls._is_issue(line) or "경고" in line or "읽지 못해" in line:
            return "warn"
        if line.startswith("완료") or "완료:" in line:
            return "ok"
        return None

    def _finish(self, result: Path) -> None:
        self.result = result
        self.progress.stop()
        self.progress.config(mode="determinate", value=self.progress["maximum"] or 100)
        self.start_btn.config(state="normal")
        self.open_btn.config(state="normal")
        self.open_dir_btn.config(state="normal")
        self.status.config(text=f"완료: {result.name}")
        self._append_log(f"──── 완료: {result} ────", "sep")
        # 점검 창에서 이미 알린 항목(저장이 덜 된 회차·빠진 회차·중복)은 다시 띄우지 않는다.
        # 변환 중에만 알 수 있는 항목(회차 안의 일부 이미지 누락·손상·실패)이 있을 때만 알림.
        already = ("저장이 덜 된", "빠진 회차", "중복 저장된", "저장된 파일에 없는 이미지", "빠진 본문 이미지",
                   "받지 못했습니다", "장만 받았습니다") if getattr(self, "precheck_shown", False) else ()
        remaining = [s for s in self.issues if not any(k in s for k in already)]
        if remaining:
            self._show_issues(sorted(remaining, key=_episode_of))

    # ---------- 점검 결과 → 다시 저장한 파일 받기 ----------
    FAILED_DIR = "_저장실패"

    def _ask_replacements_from_worker(self, bad, missing):
        """워커 스레드에서 호출: 메인 스레드에 창을 띄우고 사용자가 답할 때까지 기다린다."""
        done = threading.Event()
        holder: list = [None]
        self.q.put(("ask", (bad, missing, done, holder)))
        done.wait()
        return holder[0]

    def _replacement_dialog(self, bad, missing, done: threading.Event, holder: list) -> None:
        """점검 결과 창. 문제 회차마다 한 줄씩 보여 주고, 줄마다 [파일…] 또는 [주소…]로 대체본을 붙인다."""
        self.precheck_shown = True
        # 1) 저장이 덜 된 파일은 같은 폴더의 _저장실패 폴더로 옮긴다 (되돌릴 수 있게 지우지 않음)
        moved = []
        for title, src in bad:
            if src and src.exists():
                dest_dir = src.parent / self.FAILED_DIR
                dest_dir.mkdir(exist_ok=True)
                try:
                    src.replace(dest_dir / src.name)
                    moved.append(src.name)
                except OSError as e:
                    self._append_log(f"옮기지 못함: {src.name} ({e})", "warn")
        if moved:
            self._append_log(f"저장이 덜 된 파일 {len(moved)}개를 {self.FAILED_DIR} 폴더로 옮겼습니다.", "warn")
        self.progress.stop()
        self.status.config(text="점검 결과 확인 중… (회차마다 파일이나 주소를 붙이고 [계속]을 누르세요)")

        # 2) 창 — 변환기가 다른 창 뒤에 있어도 앞으로 나오게 하고, 화면에 보인 뒤에만 잠근다
        self.deiconify()
        self.lift()
        win = tk.Toplevel(self)
        win.title("점검 결과 — 다시 저장이 필요한 회차")
        win.geometry("860x520")
        win.transient(self)
        win.attributes("-topmost", True)
        win.after(300, lambda: win.attributes("-topmost", False))
        win.update_idletasks()

        def try_grab() -> None:
            try:
                win.grab_set()
            except tk.TclError:
                win.after(200, try_grab)
        win.after(100, try_grab)
        self.bell()

        ttk.Label(win, justify="left", wraplength=830, text=(
            "책을 만들기 전 점검 결과입니다. 저장이 덜 된 회차는 책에서 뺐고 파일은 _저장실패 폴더로 옮겼습니다.\n"
            "회차마다 크롬에서 다시 저장한 파일을 [파일…]로, 저장이 안 되는 사이트(일일툰 등)는 그 회차 주소를 [주소…]로 붙이세요. "
            "붙이지 않은 회차는 빠진 채로 만듭니다.")).pack(anchor="w", padx=12, pady=(10, 6))

        # 회차별 줄 (많으면 스크롤)
        rows: list[tuple[str, str]] = [(label, "bad") for label, _ in bad] + [(f"{n}화 (저장 파일 없음)", "gap") for n in missing]
        rows.sort(key=lambda r: _episode_of(r[0]))
        outer = ttk.Frame(win)
        outer.pack(fill="both", expand=True, padx=12)
        canvas = tk.Canvas(outer, highlightthickness=0)
        vbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        canvas.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(-int(e.delta / 120), "units"))
        inner.columnconfigure(1, weight=1)

        attached: dict[int, tk.StringVar] = {}
        for r, (label, kind) in enumerate(rows):
            ttk.Label(inner, text=label, width=34, anchor="w").grid(row=r, column=0, sticky="w", padx=(0, 8), pady=3)
            var = tk.StringVar(value="")
            attached[r] = var
            # 주소는 이 칸에 바로 붙여넣는다(별도 입력창 없음). 파일은 [파일…]로 고르면 이 칸에 경로가 들어간다.
            ent = ttk.Entry(inner, textvariable=var)
            ent.grid(row=r, column=1, sticky="ew", pady=3)

            def pick_file(rr=r, lab=label) -> None:
                initial = SAVED_DIR if SAVED_DIR.is_dir() else DOWNLOADS
                try:
                    win.grab_release()  # 파일 창이 점검 창 뒤에 숨지 않도록 잠금을 잠시 푼다
                except tk.TclError:
                    pass
                path = filedialog.askopenfilename(parent=win, title=f"{lab} — 다시 저장한 파일 선택", initialdir=str(initial),
                                                  filetypes=[("웹페이지", "*.mhtml *.mht *.html *.htm"), ("모든 파일", "*.*")])
                win.lift()
                try:
                    win.grab_set()
                except tk.TclError:
                    pass
                if path:
                    attached[rr].set(path)

            def clear(rr=r) -> None:
                attached[rr].set("")

            ttk.Button(inner, text="파일…", width=7, command=pick_file).grid(row=r, column=2, padx=2)
            ttk.Button(inner, text="지우기", width=7, command=clear).grid(row=r, column=4, padx=(2, 0))
        ttk.Label(inner, text="주소는 가운데 칸에 바로 붙여넣으세요 (Ctrl+V). 파일은 [파일…]로 고릅니다.",
                  foreground="#555").grid(row=len(rows), column=0, columnspan=5, sticky="w", pady=(6, 0))

        def collected() -> list[str] | None:
            out: list[str] = []
            for v in attached.values():
                val = v.get().strip().strip('"')
                if not val:
                    continue
                if val.startswith(("http://", "https://")) or Path(val).exists():
                    out.append(val)
                else:
                    messagebox.showwarning("확인", f"주소(http://…)나 실제 있는 파일 경로가 아닙니다:\n{val}", parent=win)
                    return None
            return out

        def finish(result) -> None:
            holder[0] = result
            try:
                win.grab_release()
            except tk.TclError:
                pass
            try:
                canvas.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass
            win.destroy()
            self.progress.config(mode="indeterminate")
            self.progress.start(12)
            self.status.config(text="변환 중…")
            done.set()

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=(8, 10))
        ttk.Label(btns, text=f"문제 회차 {len(rows)}개").pack(side="left")
        ttk.Button(btns, text="취소", command=lambda: finish(None)).pack(side="right")
        def go() -> None:
            got = collected()
            if got is not None:
                finish(got)
        ttk.Button(btns, text="계속", command=go).pack(side="right", padx=(0, 6))
        win.protocol("WM_DELETE_WINDOW", lambda: finish(None))

    def _show_issues(self, issues: list[str]) -> None:
        """복사할 수 있는 '확인이 필요한 항목' 창. 마우스로 긁어 복사하거나 [목록 복사]로 전체 복사."""
        win = tk.Toplevel(self)
        win.title("확인이 필요한 항목")
        win.geometry("720x420")
        win.transient(self)
        ttk.Label(win, text=f"변환은 끝났지만 아래 {len(issues)}개 항목을 확인해 주세요. "
                            "저장이 덜 된 회차는 책에서 빠졌으니 크롬에서 다시 저장한 뒤 추가하세요.",
                  wraplength=690, justify="left").pack(anchor="w", padx=12, pady=(10, 4))
        text = ScrolledText(win, wrap="word", font=("Consolas", 10), height=14)
        text.pack(fill="both", expand=True, padx=12, pady=4)
        body = "\n".join(f"• {s}" for s in issues)
        text.insert("1.0", body)
        text.bind("<Key>", lambda e: "break" if not (e.state & 0x4) else None)  # 타이핑 막기, Ctrl+C/A는 허용
        text.bind("<Control-a>", lambda e: (text.tag_add("sel", "1.0", "end-1c"), "break")[1])
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=12, pady=(4, 10))

        def copy_all() -> None:
            self.clipboard_clear()
            self.clipboard_append(body)
            status.config(text="복사됨")

        status = ttk.Label(btns, text="")
        status.pack(side="left")
        ttk.Button(btns, text="닫기", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="목록 복사", command=copy_all).pack(side="right", padx=(0, 6))
        win.focus_set()

    def _fail(self, msg: str) -> None:
        self.progress.stop()
        self.progress.config(mode="determinate", value=0)
        self.start_btn.config(state="normal")
        self.status.config(text="실패")
        self._append_log(f"오류: {msg}")
        self._show_log()  # 오류 내용을 바로 볼 수 있게 기록창 펼침
        messagebox.showerror("실패", msg)

    def _challenge(self, msg: str) -> None:
        self.progress.stop()
        self.start_btn.config(state="normal")
        self.status.config(text="보안 확인 필요")
        self._append_log(f"중단: {msg}")
        if messagebox.askyesno(
            "보안 확인 필요",
            "이 사이트는 봇 차단 보안 확인을 요구합니다.\n\n"
            f"더 확실한 방법: 평소 쓰는 크롬에서 그 페이지를 연 뒤 {HOTKEY}를 누르고,\n"
            "이 창의 [최근 저장 파일] → [변환 시작]을 누르세요.\n\n"
            "아니면 '브라우저 창 보기'를 켜고 다시 시도할까요?\n"
            "창이 뜨면 그 안에서 확인을 직접 완료해 주세요. 완료되면 자동으로 이어집니다.",
        ):
            self.headed.set(True)
            self._start()

    def _open_result(self) -> None:
        if self.result:
            _open_path(self.result)

    def _open_dir(self) -> None:
        if self.result:
            _open_path(self.result if self.result.is_dir() else self.result.parent)

    LOG_FILE = APP_DIR / "work" / "변환기록.log"

    def _append_log(self, line: str, tag: str | None = None) -> None:
        try:  # 창을 닫아도 남는 기록 (문제 진단용)
            self.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self.LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {line}\n")
        except OSError:
            pass
        self.log.config(state="normal")
        stamp = datetime.now().strftime("%H:%M:%S ")
        if tag:
            self.log.insert("end", stamp + line + "\n", tag)
        else:
            self.log.insert("end", stamp + line + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _clear_log(self) -> None:
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("종료", "변환이 진행 중입니다. 정말 종료할까요?"):
                return
        self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
