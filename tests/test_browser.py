import pytest

from webtoon2pdf import browser as br
from webtoon2pdf.cli import ChallengeRequired, run
from webtoon2pdf.models import JobConfig


def test_challenge_detection_and_wait(site):
    with br.launch() as (_b, _c, page):
        page.goto(f"{site}/challenge.html", wait_until="domcontentloaded")
        assert br.is_challenge_page(page) is not None
        assert br.wait_for_challenge_clear(page, timeout_s=1) is False  # 그대로 머물면 시간 초과

        # ?go=1: 2초 뒤 ep2.html로 이동(사용자가 확인을 통과한 상황) → True
        page.goto(f"{site}/challenge.html?go=1", wait_until="domcontentloaded")
        assert br.wait_for_challenge_clear(page, timeout_s=15) is True
        assert page.url.endswith("ep2.html")
        assert br.is_challenge_page(page) is None

        page.goto(f"{site}/blog.html", wait_until="domcontentloaded")
        assert br.is_challenge_page(page) is None


def test_headless_run_raises_clear_error_on_challenge(site, tmp_path):
    cfg = JobConfig(start_url=f"{site}/challenge.html", output=tmp_path / "x.pdf",
                    max_chapters=1, delay=0, work_dir=tmp_path / "work", scroll_wait=0.1)
    with pytest.raises(ChallengeRequired, match="보안 확인"):
        run(cfg, log=lambda s: None)
