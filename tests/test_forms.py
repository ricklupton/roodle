import pytest
from bs4 import BeautifulSoup

from roodle import MoodleError
from roodle.forms import EditForm, filemanager_options, find_moodleform, serialize_form


def test_serialize_form_as_a_browser_would():
    soup = BeautifulSoup("""
      <form id="search"><input name="q" value="ignored"></form>
      <form action="modedit.php">
        <input type="hidden" name="_qf__mod_resource_mod_form" value="1">
        <input type="text" name="name" value="Notes">
        <input type="hidden" name="showsize" value="0">
        <input type="checkbox" name="showsize" value="1" checked>
        <input type="hidden" name="showtype" value="0">
        <input type="checkbox" name="showtype" value="1">
        <input type="radio" name="display" value="1">
        <input type="radio" name="display" value="5" checked>
        <select name="visible"><option value="0">Hide</option>
          <option value="1" selected>Show</option></select>
        <textarea name="intro">Some text</textarea>
        <input type="text" name="disabledfield" value="x" disabled>
        <input type="submit" name="submitbutton" value="Save">
        <input type="file" name="upload">
      </form>""", "lxml")
    fields = serialize_form(find_moodleform(soup))
    assert fields == {"_qf__mod_resource_mod_form": "1", "name": "Notes", "showsize": "1",
                      "showtype": "0", "display": "5", "visible": "1", "intro": "Some text"}


def test_page_without_an_mform():
    with pytest.raises(ValueError):
        find_moodleform(BeautifulSoup("<form><input name='q'></form>", "lxml"))


def test_filemanager_options():
    html = """<script>
    Y.use("core_filepicker", function(Y) { M.form_filemanager.init(Y, {"itemid":12,
      "maxbytes":100, "filepicker": {"repositories": {"3": {"id": "3", "type": "upload"}}},
      "note": "a } and a ) in a string"}); });
    M.form_filemanager.init(Y, {"itemid": 13, "maxbytes": -1});
    </script>"""
    options = filemanager_options(html)
    assert set(options) == {12, 13}
    assert options[12]["filepicker"]["repositories"]["3"]["type"] == "upload"


def test_edit_form(fake, moodle):
    fake.add(11, "Lecture 1", {"l1.html": b"<html>"})
    form = EditForm.load(moodle, 11)
    assert form.module == "resource" and form.name == "Lecture 1"
    area = form.draft_area()
    assert (area.upload_repo_id, area.maxbytes, area.context_id, area.client_id) == \
        (3, 1_000_000, 5011, "abc123")
    assert [e["filename"] for e in area.list()] == ["l1.html"]


def test_refused_form(fake, moodle):
    fake.add(11, "Lecture 1", {"l1.html": b"<html>"})
    form = EditForm.load(moodle, 11)
    form.fields["name"] = ""
    with pytest.raises(MoodleError, match="Required"):
        form.submit()
