from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def is_banner_shaped(width: int, height: int) -> bool:
    """광고 배너·구분선 모양: 높이 120px 이하이면서 폭이 높이의 3배 이상. 본문 컷은 이렇게 납작하지 않다."""
    return 0 < height <= 120 and width >= height * 3


@dataclass
class ImageItem:
    """페이지에서 발견한 이미지 후보 하나."""

    src: str
    width: int          # 렌더된 폭(px)
    height: int         # 렌더된 높이(px)
    top: int            # 문서 기준 y 좌표
    left: int
    selector: str       # 요소를 다시 찾기 위한 CSS selector
    kind: str = "img"   # img | background
    natural_width: int = 0    # 원본 픽셀 폭(naturalWidth 또는 srcset 추정)
    original_src: str = ""    # 썸네일을 감싼 링크가 이미지 파일을 가리키면 그 URL (먼저 시도, 실패 시 src)
    web_src: str = ""         # 저장한 페이지에서 로컬 파일 대신 남아 있는 원래 웹 주소 (빠진 이미지 보완용)
    frame_url: str = ""       # iframe 안의 요소면 그 프레임 URL, 아니면 빈 문자열
    path: Path | None = None  # 다운로드 후 저장 경로

    @property
    def original_link(self) -> bool:
        return bool(self.original_src)


@dataclass
class Chapter:
    index: int
    url: str
    title: str
    images: list[ImageItem] = field(default_factory=list)
    dir: Path | None = None
    missing: list[tuple[int, str]] = field(default_factory=list)  # (본문 순서 위치, 원본 URL) — 저장 파일에 없던 본문 이미지
    page_url: str = ""  # 저장 파일이 온 원래 페이지 주소 (빠진 이미지를 받을 때 Referer)

    @property
    def image_paths(self) -> list[Path]:
        return [i.path for i in self.images if i.path is not None]


@dataclass
class JobConfig:
    start_url: str
    output: Path
    max_chapters: int = 50
    min_width: int = 300
    mode: str = "auto"  # auto | all | webtoon
    format: str = "pdf"  # pdf | epub | both
    group: int = 0       # 저장 파일 여러 개 입력 시 N개씩 나눠 여러 권으로 (0 = 한 권)
    group_by_cover: bool = False  # 회차 첫 장(표지)이 바뀔 때마다 새 권으로 나눔. 같은 권의 반복 표지는 첫 회차에만 남김
    author: str | None = None     # 저자 (EPUB dc:creator, PDF /Author)
    rtl: bool = True              # EPUB 읽는 방향 오른쪽→왼쪽(일본 만화). False면 왼쪽→오른쪽(웹툰)
    first_volume: int = 1         # 여러 권으로 나눌 때 첫 권 번호 (표지에서 읽지 못했을 때 사용)
    volume_range: tuple[int, int] | None = None   # 만들 권 범위 (양끝 포함). None이면 전부
    episode_range: tuple[int, int] | None = None  # 읽을 회차 범위 (양끝 포함, 파일명·제목의 N화). None이면 전부
    delay: float = 0.3
    headed: bool = False
    cookies: Path | None = None
    next_selector: str | None = None
    split: bool = False
    webtoon_merge: bool = False   # 웹툰: 작게 잘린 조각들을 '긴 한 쪽'으로 이어 붙여 끊김 줄임
    webtoon_page_px: int = 0      # 그때 한 쪽 최대 높이(px). 0=자동(최대)
    keep_images: bool = False
    title: str | None = None
    fit_width: bool = False
    work_dir: Path = Path("work")
    scroll_wait: float = 0.4
    timeout_ms: int = 30000
