import httpx
import pytest

from fake_moodle import SERVER, SESSKEY, page
from roodle import Moodle, MoodleError, NotLoggedIn
from roodle.client import Page


def test_logged_in(fake, moodle):
    assert moodle.logged_in()
    fake.logged_in = False
    assert not moodle.logged_in()
    with pytest.raises(NotLoggedIn):
        moodle.page("/course/view.php?id=1")


def test_page_marked_logged_out_is_not_logged_in():
    def handle(request):
        return httpx.Response(200, text=page("Front page", logged_in=False))
    moodle = Moodle(SERVER, transport=httpx.MockTransport(handle))
    assert not moodle.logged_in()


def test_redirect_to_single_sign_on_is_not_logged_in():
    def handle(request):
        if request.url.host == "sso.example.ac.uk":
            return httpx.Response(200, text="<html><body>Sign in</body></html>")
        return httpx.Response(302, headers={"Location": "https://sso.example.ac.uk/idp"})
    moodle = Moodle(SERVER, transport=httpx.MockTransport(handle))
    assert not moodle.logged_in()


def test_sesskey_comes_from_m_cfg(moodle):
    assert moodle.sesskey == SESSKEY


def test_page_without_charset_is_read_as_utf8():
    def handle(request):
        return httpx.Response(200, content=page("1a: distributions – ‘quoted’").encode(),
                              headers={"Content-Type": "text/html"})
    moodle = Moodle(SERVER, transport=httpx.MockTransport(handle))
    assert "1a: distributions – ‘quoted’" in moodle.page("/").html


def test_ajax_errors(fake, moodle):
    with pytest.raises(MoodleError, match="invalidrecord"):
        moodle.ajax("no_such_function", {})
    fake.logged_in = False
    with pytest.raises(NotLoggedIn):
        moodle._sesskey = SESSKEY
        moodle.ajax("core_courseformat_get_state", {"courseid": 1})


def test_refresh_cookies_reads_them_again():
    calls = []

    def source():
        calls.append(1)
        return {"MoodleSession": f"cookie{len(calls)}"}

    moodle = Moodle(SERVER, cookie_source=source)
    assert moodle.http.cookies["MoodleSession"] == "cookie1"
    moodle.refresh_cookies()
    assert moodle.http.cookies["MoodleSession"] == "cookie2"


def test_server_without_scheme_and_with_a_path():
    moodle = Moodle("moodle.example.ac.uk/moodle/")
    assert moodle.server == "https://moodle.example.ac.uk/moodle"
    assert moodle.url("/course/view.php") == "https://moodle.example.ac.uk/moodle/course/view.php"


def test_m_cfg():
    p = Page(httpx.Response(200, text=page("x", cfg={"contextid": 42}),
                            request=httpx.Request("GET", SERVER)))
    assert p.cfg["contextid"] == 42 and p.cfg["sesskey"] == SESSKEY


def test_m_cfg_with_more_script_on_its_line():
    # As Bath's Moodle (boost_union theme) writes it.
    html = r"""<html><head><script>
//<![CDATA[
var M = {}; M.yui = {};
M.pageloadstarttime = new Date();
M.cfg = {"wwwroot":"https:\/\/moodle.bath.ac.uk","homeurl":{},"sesskey":"SESSKEY","contextid":2,"userId":104829};var yui1ConfigFn = function(me) {if(/-skin|reset|fonts|grids|base/.test(me.name)){me.type='css';me.path=me.path.replace(/\.js/,'.css')}};
var yui2ConfigFn = function(me) {var parts=me.name.replace(/^moodle-/,'').split('-')};
//]]>
</script></head><body class="path-site"></body></html>"""
    p = Page(httpx.Response(200, text=html, request=httpx.Request("GET", SERVER)))
    assert p.cfg["sesskey"] == "SESSKEY" and p.cfg["contextid"] == 2
