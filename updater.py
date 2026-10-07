"""자동 업데이트: GitHub 공개 저장소에서 webtoon2pdf 소스(앱 두뇌)만 받아 교체한다.

이 파일은 frozen exe에 '고정'으로 들어간다(자동 업데이트 대상 아님). 그래서 업데이트 로직 자체를 바꾸려면
설치본을 다시 빌드해야 한다 — 로직은 단순하게 유지한다. 실제 기능 코드(webtoon2pdf/)만 자동으로 바뀐다.

UI(물어보기)는 호출하는 쪽(app_main / gui)에서 담당하고, 여기서는 '확인·내려받기·교체'만 한다.
"""
from __future__ import annotations

import io
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

REPO = "hyokusam-gif/ebook-converter"  # 자동 업데이트를 받아오는 공개 저장소
BRANCH = "main"
VER_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/VERSION"
ZIP_URL = f"https://github.com/{REPO}/archive/refs/heads/{BRANCH}.zip"
TIMEOUT = 5  # 버전 확인용 짧은 제한(초). 다운로드는 더 길게.

Log = Callable[[str], None]


def app_dir() -> Path:
    """exe(또는 소스)가 있는 폴더. VERSION과 webtoon2pdf/가 여기에 있다."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


def _as_num(v: str) -> int:
    v = (v or "").strip()
    return int(v) if v.isdigit() else 0


def local_version() -> str:
    try:
        return (app_dir() / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def remote_version() -> str | None:
    try:
        with urllib.request.urlopen(VER_URL, timeout=TIMEOUT) as r:
            return r.read().decode("utf-8").strip()
    except Exception:  # noqa: BLE001  (오프라인·차단 등 모든 경우 조용히 실패)
        return None


def update_available() -> str | None:
    """원격이 로컬보다 새 버전이면 그 버전 문자열, 아니면 None."""
    rv = remote_version()
    if rv and _as_num(rv) > _as_num(local_version()):
        return rv
    return None


def apply_update(log: Log = lambda m: None) -> bool:
    """GitHub에서 소스 zip을 받아 app_dir/webtoon2pdf 를 최신으로 교체한다.
    기존 폴더는 webtoon2pdf.bak 로 백업하고, 실패 시 되돌린다. 성공하면 True."""
    d = app_dir()
    pkg = d / "webtoon2pdf"
    bak = d / "webtoon2pdf.bak"
    tmp: Path | None = None
    try:
        with urllib.request.urlopen(ZIP_URL, timeout=max(TIMEOUT, 60)) as r:
            data = r.read()
        tmp = Path(tempfile.mkdtemp())
        zipfile.ZipFile(io.BytesIO(data)).extractall(tmp)
        # zip 구조: <repo>-main/webtoon2pdf/… , <repo>-main/VERSION
        src_root = next((p for p in tmp.iterdir() if p.is_dir() and (p / "webtoon2pdf").is_dir()), None)
        if src_root is None:
            log("업데이트 파일에서 webtoon2pdf 폴더를 찾지 못했습니다.")
            return False
        src_pkg = src_root / "webtoon2pdf"

        if bak.exists():
            shutil.rmtree(bak, ignore_errors=True)
        if pkg.exists():
            pkg.rename(bak)                 # 기존을 백업으로
        shutil.copytree(src_pkg, pkg)       # 새 소스 설치

        new_ver = src_root / "VERSION"
        if new_ver.is_file():
            try:
                (d / "VERSION").write_text(new_ver.read_text(encoding="utf-8").strip(), encoding="utf-8")
            except OSError:
                pass
        log("업데이트 적용 완료")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"업데이트 실패: {e!r}")
        if not pkg.exists() and bak.exists():  # 교체 도중 실패 → 되돌리기
            bak.rename(pkg)
        return False
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def restore_backup() -> bool:
    """백업(webtoon2pdf.bak)이 있으면 되돌린다. 새 코드가 import 실패할 때 쓴다."""
    d = app_dir()
    pkg, bak = d / "webtoon2pdf", d / "webtoon2pdf.bak"
    if bak.exists():
        shutil.rmtree(pkg, ignore_errors=True)
        bak.rename(pkg)
        return True
    return False
