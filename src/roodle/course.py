"""
A Moodle course, and the modules in it.
"""

import json
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlparse

from .errors import MoodleError, NotLoggedIn


@dataclass(frozen=True)
class Module:
    """A course module (an activity or resource) as the course page lists it."""

    cmid: int
    name: str
    module: str              # "resource" for a File, "folder", "assign"...
    section: int | None
    visible: bool | None     # False: hidden from students
    url: str | None = None
    stealth: bool | None = None  # available, but not shown on the course page


class Course:
    def __init__(self, moodle, course_id):
        self.moodle = moodle
        self.id = int(course_id)

    def __repr__(self):
        return f"Course({self.moodle.server!r}, {self.id})"

    @property
    def url(self):
        return self.moodle.url(f"/course/view.php?id={self.id}")

    # ------------------------------------------------------------ modules

    def modules(self):
        """Every module in the course, hidden ones included, in course order.

        This is the course state the course page's own JavaScript loads
        (core_courseformat_get_state, Moodle 4.0 on).
        """
        state = self.moodle.ajax("core_courseformat_get_state", {"courseid": self.id})
        if isinstance(state, str):
            state = json.loads(state)
        sections = {s["id"]: s.get("number", s.get("section"))
                    for s in state.get("section", [])}
        order = [cmid for s in state.get("section", []) for cmid in s.get("cmlist", [])]
        cms = {int(cm["id"]): cm for cm in state.get("cm", [])}
        ids = [int(c) for c in order if int(c) in cms] or list(cms)
        ids += [c for c in cms if c not in ids]
        return [Module(
            cmid=cmid,
            name=cms[cmid].get("name", ""),
            module=cms[cmid].get("module", ""),
            section=cms[cmid].get("sectionnumber", sections.get(cms[cmid].get("sectionid"))),
            visible=cms[cmid].get("visible"),
            url=cms[cmid].get("url"),
            stealth=cms[cmid].get("stealth"),
        ) for cmid in ids]

    def resources(self):
        """Every File resource in the course, hidden ones included.

        From the course state if this Moodle gives it, and otherwise from
        the course's "Files" index page.
        """
        from .resource import FileResource
        try:
            found = [m for m in self.modules() if m.module == "resource"]
        except NotLoggedIn:
            raise
        except MoodleError:
            found = self._resources_from_index()
        return [FileResource(self, m.cmid, m.name, m.visible, m.section, m.stealth)
                for m in found]

    def _resources_from_index(self):
        page = self.moodle.page("/mod/resource/index.php", params={"id": self.id})
        return parse_resource_index(page.soup, page.url)

    def resource(self, cmid):
        from .resource import FileResource
        return FileResource(self, cmid)

    # ------------------------------------------------- people and activities

    def participants(self):
        from .users import Participants
        return Participants.load(self)

    def assignment(self, cmid):
        from .assignment import Assignment
        return Assignment(self, cmid)

    def grades(self):
        """The course's gradebook, as the CSV that "Export to plain text
        file" downloads."""
        r = self.moodle.check(self.moodle.post(
            "/grade/export/txt/export.php",
            data={
                "id": str(self.id),
                "sesskey": self.moodle.sesskey,
                "mform_isexpanded_id_gradeitems": "1",
                "checkbox_controller1": "0",
                "mform_isexpanded_id_options": "1",
                "_qf__grade_export_form": "1",
                "export_feedback": "0",
                "export_onlyactive": ["0", "1"],
                "display[real]": ["0", "1"],
                "display[percentage]": "0",
                "display[letter]": "0",
                "decimals": "2",
                "separator": "comma",
                "submitbutton": "Download",
            },
        ))
        return r.text

    def choice_results(self, cmid):
        """A Choice activity's responses, as the text its report downloads."""
        r = self.moodle.check(self.moodle.post(
            "/mod/choice/report.php",
            data={"id": str(cmid), "download": "txt", "sesskey": self.moodle.sesskey},
        ))
        return r.text

    def choicegroup_results(self, cmid):
        """A Group choice activity's responses, as the text its report
        downloads."""
        r = self.moodle.check(self.moodle.post(
            "/mod/choicegroup/report.php",
            data={"id": str(cmid), "download": "txt", "sesskey": self.moodle.sesskey},
        ))
        return r.text


def parse_resource_index(soup, page_url):
    """The File resources linked from a course's "Files" index page.

    Moodle writes the links relative to the page (href="view.php?id=..."),
    so each is resolved against the page's URL before it is matched; a
    resource hidden from students has class="dimmed".
    """
    found = {}
    for a in soup.find_all("a", href=True):
        url = urlparse(urljoin(page_url, a["href"]))
        cmid = parse_qs(url.query).get("id", [""])[0]
        if url.path.endswith("/mod/resource/view.php") and cmid.isdigit():
            found.setdefault(int(cmid), Module(
                cmid=int(cmid),
                name=a.get_text(" ", strip=True),
                module="resource",
                section=None,
                visible="dimmed" not in a.get("class", []),
                url=url.geturl(),
            ))
    return list(found.values())
