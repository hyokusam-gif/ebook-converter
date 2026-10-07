"""로컬 HTTP 서버로 fixtures를 서빙한다. 이미지는 세션 시작 시 생성."""
from __future__ import annotations

import shutil
import socket
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

FIXTURES = Path(__file__).parent / "fixtures"


def _make_image(path: Path, w: int, h: int, label: str, fmt: str) -> None:
    im = Image.new("RGB", (w, h), (240, 240, 240))
    d = ImageDraw.Draw(im)
    d.rectangle([10, 10, w - 10, h - 10], outline=(30, 30, 30), width=4)
    d.text((20, 20), label, fill=(0, 0, 0))
    if fmt == "png_alpha":
        im = im.convert("RGBA")
        im.putalpha(200)
        im.save(path, "PNG")
    else:
        im.save(path, fmt)


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D401
        pass


@pytest.fixture(scope="session")
def site(tmp_path_factory) -> str:
    root = tmp_path_factory.mktemp("site")
    for f in FIXTURES.glob("*.html"):
        shutil.copy(f, root / f.name)
    img = root / "img"
    img.mkdir()
    for i in range(1, 6):
        _make_image(img / f"body_0{i}.jpg", 800, 1200, f"body {i}", "JPEG")
    _make_image(img / "body_02.webp", 800, 1200, "body 2 webp", "WEBP")
    _make_image(img / "body_03.png", 800, 1200, "body 3 png alpha", "png_alpha")
    _make_image(img / "logo.png", 120, 40, "logo", "PNG")
    ads = root / "ads"
    ads.mkdir()
    _make_image(ads / "banner.jpg", 728, 90, "banner", "JPEG")
    for c in "abc":
        _make_image(img / f"thumb_{c}.jpg", 200, 280, f"thumb {c}", "JPEG")

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(_Quiet, directory=str(root)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
