from webtoon2pdf import browser as br
from webtoon2pdf.navigator import find_next_url, same_site


def test_same_site_ignores_common_subdomains():
    assert same_site("https://www.example.com/a", "https://m.example.com/b")
    assert not same_site("https://example.com/a", "https://other.com/b")


def test_find_next_by_text_rel_and_absence(site):
    with br.launch() as (_b, _c, page):
        br.load(page, f"{site}/ep1.html")
        assert find_next_url(page, page.url) == f"{site}/ep2.html"

        br.load(page, f"{site}/ep2.html")
        assert find_next_url(page, page.url) == f"{site}/ep3.html"

        br.load(page, f"{site}/ep3.html")
        assert find_next_url(page, page.url) is None

        # 사용자 지정 selector
        br.load(page, f"{site}/ep3.html")
        assert find_next_url(page, page.url, selector="nav a:first-child") == f"{site}/ep2.html"


def test_find_next_by_clicking_button_without_href(site):
    with br.launch() as (_b, _c, page):
        br.load(page, f"{site}/ep_button.html")
        assert find_next_url(page, page.url) == f"{site}/ep2.html"
        assert page.url == f"{site}/ep2.html"  # 클릭으로 이미 이동한 상태

        # 사용자 selector가 버튼을 가리켜도 클릭으로 이동
        br.load(page, f"{site}/ep_button.html")
        assert find_next_url(page, page.url, selector="button.link_next") == f"{site}/ep2.html"
