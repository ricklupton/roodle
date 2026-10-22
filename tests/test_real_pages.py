"""
Tests of the parsers against real Moodle pages, saved by tests/snapshot.py.
Each is skipped if its page hasn't been saved.
"""

import json
import re
from pathlib import Path

import httpx
import pytest

from roodle import Moodle
from roodle.client import Page
from roodle.forms import filemanager_options, find_moodleform, serialize_form

PAGES = Path(__file__).parent / "pages"
SERVER = "https://moodle.example.ac.uk"


def saved(name):
    path = PAGES / name
    if not path.exists():
        pytest.skip(f"no {name}: see tests/snapshot.py")
    return path.read_text()


def as_page(name):
    return Page(httpx.Response(200, text=saved(name), request=httpx.Request("GET", SERVER)))


def test_front_page():
    page = as_page("front.html")
    assert page.logged_in
    assert page.cfg["sesskey"] == "SESSKEY"


def test_logged_out_course_page_sends_you_to_log_in():
    """roodle takes a redirect to /login/ or to another site (single sign-on)
    as not logged in."""
    a = json.loads(saved("answers.json"))["logged_out_course"]
    host = re.match(r"https?://[^/]+", a["url"]).group(0)
    steps = [*a["history"], a]
    assert any("/login/" in s["url"] or not s["url"].startswith(host) for s in steps[1:]) \
        or "/login/" in a["url"]


def test_logged_out_front_page():
    a = json.loads(saved("answers.json"))["logged_out_front"]
    if "/login/" not in a["url"]:
        assert not as_page("logged-out-front.html").logged_in


def test_edit_form():
    page = as_page("modedit.html")
    fields = serialize_form(find_moodleform(page.soup))
    assert fields["modulename"] == "resource"
    assert isinstance(page.cfg["contextid"], int)
    options = filemanager_options(page.html)
    files = options[int(fields["files"])]
    assert int(files["maxbytes"]) > 0
    assert "upload" in [r["type"] for r in files["filepicker"]["repositories"].values()]
    assert files["client_id"]


def test_course_state():
    text = saved("course-state.json")
    moodle = Moodle(SERVER, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=text)))
    moodle._sesskey = "SESSKEY"
    modules = moodle.course(1).modules()
    assert modules
    assert all(isinstance(m.visible, bool) and isinstance(m.stealth, bool)
               and isinstance(m.section, int) for m in modules)
    assert any(m.module == "resource" for m in modules)


def test_resource_view_redirects_to_its_file():
    view = json.loads(saved("answers.json"))["view"]
    assert view["status"] in (301, 302, 303)
    assert re.search(r"/pluginfile\.php/\d+/mod_resource/content/\d+/.", view["headers"]["location"])


def test_pluginfile_etag_is_the_content_hash():
    answers = json.loads(saved("answers.json"))
    if "pluginfile" not in answers:
        pytest.skip("the resource held no file")
    served = answers["pluginfile"]
    if served["status"] in (301, 302, 303, 307):
        # From a file store, which names the file by its content hash.
        assert re.search(r"[0-9a-f]{40}", served["headers"]["location"])
        return
    assert re.search(r"[0-9a-f]{40}", served["headers"]["etag"])
    assert answers["pluginfile_if_none_match"]["status"] == 304
