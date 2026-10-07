"""naver 모듈: 제목→권 파싱, logNo 내림차순 정렬, 이미지 URL 추출 (네트워크 없이)."""
from webtoon2pdf import naver
from webtoon2pdf.naver import PostRef


def test_parse_volume():
    assert naver.parse_volume("최고의 신 제우스 / 홍은영의 '만화로 보는 그리스 로마 신화'  제1권 -세 번째 이야기") == 1
    assert naver.parse_volume("홍은영의 만화로 보는 그리스 로마신화 11권 -1 ▶첫 번째 이야기 편") == 11
    assert naver.parse_volume("홍은영 그림체 '만화로 보는 그리스 로마 신화' 18권 / 공주 나우시카") == 18
    # 소개·공지글: '권'이 없음 → None (자동 제외 대상)
    assert naver.parse_volume("홍은영의 <만화로 보는 그리스 로마 신화>에 대하여") is None
    assert naver.parse_volume("홍은영의 「이집트 신화」와 함께하는 목요일 / 네이버 웹툰") is None


def test_reading_order_desc_by_logno():
    # 같은 1권 안: logNo 큰 글이 이야기상 앞(첫 번째). 소개글은 빠진다.
    posts = [
        PostRef("223535779587", "1권 마지막 번째"),
        PostRef("223589158403", "제1권 첫 번째 이야기"),
        PostRef("223556754797", "제1권 세 번째 이야기"),
        PostRef("223583977743", "제1권 두 번째 이야기"),
        PostRef("221766896164", "18권 / 공주 나우시카"),
        PostRef("224390440070", "만화로 보는 그리스 로마 신화에 대하여"),  # 권 없음 → 제외
    ]
    order = naver.build_reading_order(posts, log=lambda *_: None)
    assert list(order.keys()) == [1, 18]  # 권 오름차순
    assert [p.log_no for p in order[1]] == [
        "223589158403", "223583977743", "223556754797", "223535779587",
    ]  # 첫 번째 → 마지막


def test_image_url_extraction():
    html = (
        '<div>'
        '<img src="https://blogthumb.pstatic.net/x/thumb.jpg?type=w80" '
        'data-lazy-src="https://postfiles.pstatic.net/AAA/SE-1.jpg?type=w773">'
        '<img data-lazy-src="https://postfiles.pstatic.net/BBB/SE-2.jpg?type=w773">'
        '<img data-lazy-src="https://postfiles.pstatic.net/AAA/SE-1.jpg?type=w773">'  # 중복
        '</div>'
    )
    urls = [m for m in naver._IMG_RE.findall(html)]
    # 중복 제거는 post_image_urls가 하므로 여기선 정규식이 postfiles만 잡는지 확인
    assert all("postfiles.pstatic.net" in u for u in urls)
    assert "blogthumb" not in "".join(urls)
    assert len(urls) == 3


def test_series_from_titles():
    titles = [
        "최고의 신 제우스 / 홍은영의 '만화로 보는 그리스 로마 신화' 제1권",
        "세계와 신들의 탄생 / 홍은영의 「만화로 보는 그리스 로마 신화」 제1권",
    ]
    assert naver.series_from_titles(titles) == "만화로 보는 그리스 로마 신화"


def test_resolve_category_parsing():
    bid, cno, _name = naver._parse_category_url("https://blog.naver.com/noteksc?categoryNo=83")
    assert bid == "noteksc" and cno == "83"
    bid, cno, _name = naver._parse_category_url(
        "https://blog.naver.com/PostList.naver?blogId=noteksc&categoryNo=83&from=postList")
    assert bid == "noteksc" and cno == "83"
