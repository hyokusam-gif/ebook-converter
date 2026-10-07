"""회차 이미지를 낱장 이미지 파일(JPG/WEBP)로 내보낸다. (책이 아니라 페이지별 이미지 폴더)"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from PIL import Image

from .models import Chapter

IMAGE_FORMATS = ("jpg", "webp")
_EXT = {"jpg": ".jpg", "webp": ".webp"}


def _to_saveable(im: Image.Image) -> Image.Image:
    """투명(알파)은 흰 배경에 합성하고, 팔레트·기타 모드는 RGB로. (만화 페이지용)"""
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    if im.mode == "P":
        im = im.convert("RGBA" if has_alpha else "RGB")
    if has_alpha:
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.getchannel("A") if "A" in im.getbands() else None)
        return bg
    if im.mode not in ("RGB", "L"):
        return im.convert("RGB")
    return im


def build_images(chapters: list[Chapter], out_dir: Path, fmt: str,
                 log: Callable[[str], None] = print) -> int:
    """chapters의 이미지를 읽는 순서대로 out_dir에 0001.jpg / 0001.webp … 로 저장한다.
    out_dir가 이미 있으면 비우고 새로 쓴다. 저장한 장 수를 돌려준다."""
    if fmt not in _EXT:
        raise RuntimeError(f"이미지 형식은 {IMAGE_FORMATS} 중 하나여야 합니다: {fmt}")
    ext = _EXT[fmt]
    out_dir = out_dir.with_suffix("")
    if out_dir.exists():
        shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    n = 0
    for ch in chapters:
        for p in ch.image_paths:
            try:
                with Image.open(p) as im:
                    im.load()
                    saveable = _to_saveable(im)
                    dest = out_dir / f"{n + 1:04d}{ext}"
                    if fmt == "jpg":
                        saveable.save(dest, "JPEG", quality=92, optimize=True)
                    else:
                        saveable.save(dest, "WEBP", quality=90, method=4)
                n += 1
            except Exception:  # noqa: BLE001
                log(f"  [건너뜀] 변환 실패한 이미지: {p.name}")
    if n == 0:
        raise RuntimeError("내보낼 이미지가 없습니다.")
    return n
