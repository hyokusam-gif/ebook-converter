"""PyInstaller 진입점. 패키지 상대 임포트를 위해 gui.py를 직접 지정하지 않고 여기서 부른다.

숨은 점검 모드: `전자책변환기.exe --selftest <입력> <출력.epub>` 로 실행하면 창 없이 변환만 하고
결과를 stdout에 찍는다. 빌드된 exe가 제대로 도는지 확인하는 용도.
"""
import os
import sys


def _selftest(src: str, out: str) -> int:
    from pathlib import Path
    from webtoon2pdf.cli import run
    from webtoon2pdf.models import JobConfig
    report = Path(out).with_suffix(".selftest.txt")  # windowed exe라 결과를 파일로 남긴다
    logs: list[str] = []
    try:
        result = run(
            JobConfig(start_url=src, output=Path(out), max_chapters=1, delay=0,
                      work_dir=Path(out).parent / "_selftest_work", format="epub"),
            log=lambda m: logs.append(m),
        )
        report.write_text("SELFTEST_OK: " + str(result) + "\n" + "\n".join(logs), encoding="utf-8")
        return 0
    except Exception as e:  # noqa: BLE001
        report.write_text("SELFTEST_FAIL: " + repr(e) + "\n" + "\n".join(logs[-8:]), encoding="utf-8")
        return 1


def _ocrtest(image: str, report: str) -> int:
    from pathlib import Path
    from webtoon2pdf import volumes as vol
    try:
        n = vol.ocr_volume_number(Path(image))
        Path(report).write_text(f"OCR_RESULT: {n}", encoding="utf-8")
        return 0
    except Exception as e:  # noqa: BLE001
        Path(report).write_text(f"OCR_FAIL: {e!r}", encoding="utf-8")
        return 1


def _prepare_path() -> None:
    """frozen exe일 때, exe 옆 낱개 webtoon2pdf/ 소스를 import할 수 있도록 경로를 추가한다.
    (webtoon2pdf는 exe에 넣지 않고 자동 업데이트로 교체되는 '두뇌' 부분이다.)"""
    from pathlib import Path
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))


def _maybe_update() -> None:
    """시작 시 업데이트 점검 → 새 버전이 있으면 '물어본 뒤' 설치. 오프라인·오류면 조용히 넘어간다."""
    try:
        import updater
        if not updater.update_available():
            return
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        try:
            yes = messagebox.askyesno(
                "새 버전이 있어요",
                "프로그램의 새 버전이 있습니다.\n지금 업데이트할까요? (몇 초면 됩니다)")
            if yes:
                if updater.apply_update():
                    messagebox.showinfo("업데이트 완료", "최신 버전으로 시작합니다.")
                else:
                    messagebox.showwarning("업데이트 실패", "업데이트에 실패해 현재 버전으로 실행합니다.")
        finally:
            root.destroy()
    except Exception:  # noqa: BLE001  (업데이트는 실패해도 앱 실행을 막지 않는다)
        pass


def main() -> None:
    if len(sys.argv) >= 4 and sys.argv[1] == "--selftest":
        _prepare_path()
        sys.exit(_selftest(sys.argv[2], sys.argv[3]))
    if len(sys.argv) >= 4 and sys.argv[1] == "--ocrtest":
        _prepare_path()
        sys.exit(_ocrtest(sys.argv[2], sys.argv[3]))

    _prepare_path()
    _maybe_update()
    try:
        from webtoon2pdf.gui import main as gui_main
    except Exception:  # noqa: BLE001  (방금 받은 업데이트가 깨졌으면 백업으로 롤백 후 1회 재시도)
        import updater
        if not updater.restore_backup():
            raise
        from webtoon2pdf.gui import main as gui_main
    gui_main()


def _log_crash(e: BaseException) -> None:
    """창 모드(frozen) exe는 오류가 화면에 안 보이므로, 예외를 파일로 남긴다."""
    import traceback
    from datetime import datetime
    from pathlib import Path
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    text = f"{datetime.now():%Y-%m-%d %H:%M:%S}\n" + "".join(traceback.format_exception(type(e), e, e.__traceback__))
    for p in (base / "_오류기록.txt", Path(os.environ.get("TEMP", ".")) / "전자책변환기_오류기록.txt"):
        try:
            p.write_text(text, encoding="utf-8")
        except OSError:
            pass


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        _log_crash(e)
        raise
