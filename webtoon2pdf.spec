# -*- mode: python ; coding: utf-8 -*-
# 전자책 변환기 standalone 빌드 스펙 (onedir, 창 모드)
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# Playwright: 드라이버(node) 포함. 브라우저(Chromium)는 번들하지 않고 시스템 크롬 사용.
for pkg in ("playwright",):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# winocr(표지 권 번호 OCR): winsdk 의존. 실패해도 변환은 정상이라 있으면 포함.
for pkg in ("winocr", "winsdk"):
    try:
        d, b, h = collect_all(pkg)
        datas += d; binaries += b; hiddenimports += h
    except Exception:
        pass

# webtoon2pdf(앱 두뇌)는 exe에 넣지 않는다 — exe 옆 낱개 .py로 두고 자동 업데이트로 교체한다.
# 그래서 PyInstaller가 webtoon2pdf를 스캔하지 못하므로, 그 패키지가 쓰는 의존성을 여기서 직접 명시한다.
hiddenimports += [
    # 서드파티
    "img2pdf", "pypdf", "typer", "rich", "rich.console", "rich.text",
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "PIL._tkinter_finder",
    "playwright", "playwright.sync_api",
    # GUI (webtoon2pdf.gui가 쓰지만 app_main이 직접 import하지 않음)
    "tkinter", "tkinter.ttk", "tkinter.filedialog", "tkinter.messagebox",
    "tkinter.simpledialog", "tkinter.scrolledtext",
    # 표준 라이브러리 (webtoon2pdf만 써서 누락될 수 있는 것들)
    "email", "email.policy", "email.message", "zipfile", "json",
    "urllib", "urllib.parse", "urllib.request", "html", "hashlib", "zlib",
    "queue", "threading", "webbrowser", "subprocess", "shutil", "tempfile",
    "dataclasses", "datetime", "contextlib", "io", "re", "collections",
]

a = Analysis(
    ["app_main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pytest", "_pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="전자책변환기",
    console=False,          # 창 모드(검은 콘솔 없음)
    disable_windowed_traceback=False,
    icon=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="전자책변환기",
)
