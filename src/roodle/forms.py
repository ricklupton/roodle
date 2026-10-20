"""
Generic helpers for scraping and resubmitting Moodle "mform" pages.

Moodle's own file manager / edit-module pages are ordinary HTML forms with
no public API of their own.  The approach here is the same one a browser
uses: load the page, read off the form's current field values, change the
one or two fields you actually mean to change, and POST the rest back
unchanged.  That avoids having to know (and keep up to date) every hidden
field a particular mform happens to render.
"""

import json
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .errors import MoodleError


def find_moodleform(soup):
    """Find the <form> on a page that is a Moodle quickform (mform).

    Moodle quickforms always carry a hidden `_qf__<formclass>` marker input;
    this is a more reliable way to find the form than guessing an id (which
    can vary, and a page may contain other forms, e.g. a search box).
    """
    marker = soup.find("input", attrs={"name": lambda n: n and n.startswith("_qf__")})
    if marker is None:
        raise ValueError("No moodle form (_qf__ marker) found on page")
    form = marker.find_parent("form")
    if form is None:
        raise ValueError("_qf__ marker input is not inside a <form>")
    return form


def serialize_form(form):
    """Serialise a Moodle mform the way a browser would submit it.

    Hidden/text/number/etc. inputs, selects and textareas contribute their
    current value. Checked checkboxes/radios contribute their value;
    unchecked ones are skipped -- Moodle's "advcheckbox" elements render an
    off-value hidden input immediately before the checkbox sharing the same
    name, so processing elements in document order and skipping unchecked
    boxes naturally reproduces the value a real submit would send. Submit,
    button, image, reset and file inputs are skipped entirely (the caller
    sets `submitbutton` itself, and file uploads are handled separately via
    the file manager's draft area, not a raw file input).
    """
    fields = {}
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or el.get("disabled") is not None:
            continue

        if el.name == "select":
            selected = el.find("option", selected=True) or el.find("option")
            fields[name] = selected.get("value", selected.text) if selected else ""
            continue

        if el.name == "textarea":
            fields[name] = el.text
            continue

        input_type = (el.get("type") or "text").lower()
        if input_type in ("submit", "button", "image", "file", "reset"):
            continue
        if input_type in ("checkbox", "radio"):
            if el.get("checked") is not None:
                fields[name] = el.get("value", "1")
            continue
        fields[name] = el.get("value", "")

    return fields


def filemanager_options(html):
    """The options of each file manager on a page, by draft item id.

    Moodle starts each file manager with `M.form_filemanager.init(Y, {...})`,
    whose options give the draft area's item id, the upload limits (already
    the smallest of the site's, the course's and the field's) and the
    repositories the file picker offers, keyed by id.
    """
    found = {}
    decoder = json.JSONDecoder()
    for m in re.finditer(r"M\.form_filemanager\.init\(\s*Y\s*,\s*", html):
        try:
            options, _ = decoder.raw_decode(html, m.end())
        except json.JSONDecodeError:
            continue
        if isinstance(options, dict) and "itemid" in options:
            found[int(options["itemid"])] = options
    return found


@dataclass
class EditForm:
    """A course module's "Edit settings" form, as it would be submitted
    unchanged, with the file managers on it."""

    moodle: object
    cmid: int
    action: str
    fields: dict
    context_id: int | None
    filemanagers: dict = field(repr=False)

    @classmethod
    def load(cls, moodle, cmid):
        page = moodle.page("/course/modedit.php", params={"update": cmid, "return": 0})
        form = find_moodleform(page.soup)
        action = urljoin(page.url, form.get("action") or "modedit.php")
        return cls(moodle, cmid, action, serialize_form(form),
                   page.cfg.get("contextid"), filemanager_options(page.html))

    @property
    def module(self):
        """The module's type: "resource" for a File, "folder", "assign"..."""
        return self.fields.get("modulename")

    @property
    def name(self):
        return self.fields.get("name")

    def draft_area(self, field_name="files"):
        """The draft file area behind one of the form's file managers, already
        holding the module's current files."""
        from .files import DraftArea
        if field_name not in self.fields:
            raise MoodleError(f"cmid {self.cmid}: no '{field_name}' file manager "
                              "on its edit form")
        itemid = int(self.fields[field_name])
        return DraftArea(self.moodle, itemid, self.fields["sesskey"],
                         self.context_id, self.filemanagers.get(itemid, {}))

    def submit(self, submitbutton="Save and display"):
        """Save the form.  Moodle answers a successful save by redirecting,
        and a refused one by showing the form again, with its errors."""
        data = dict(self.fields, submitbutton=submitbutton)
        r = self.moodle.check(self.moodle.post(self.action, data=data))
        if not r.is_redirect:
            errors = BeautifulSoup(r.text, "lxml").select(".error, .invalid-feedback")
            message = "; ".join(e.get_text(" ", strip=True) for e in errors
                                if e.get_text(strip=True))
            raise MoodleError(f"cmid {self.cmid}: Moodle didn't save the form"
                              + (f": {message}" if message else ""))
        return r
