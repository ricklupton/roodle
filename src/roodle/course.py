"""
A Moodle course, and the modules in it.
"""

import json
from dataclasses import dataclass

from .errors import MoodleError


@dataclass(frozen=True)
class Module:
    """A course module (an activity or resource) as the course page lists it."""

    cmid: int
    name: str
    module: str          # "resource" for a File, "folder", "assign"...
    section: int
    visible: bool        # False: hidden from students
    stealth: bool        # True: available, but not shown on the course page
    url: str | None = None


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

        This is the course state that the course page's own JavaScript loads
        (core_courseformat_get_state).
        """
        state = self.moodle.ajax("core_courseformat_get_state", {"courseid": self.id})
        if isinstance(state, str):
            state = json.loads(state)
        try:
            cms = {int(cm["id"]): cm for cm in state["cm"]}
            order = [int(c) for s in state["section"] for c in s["cmlist"]]
            order += [c for c in cms if c not in order]
            return [Module(
                cmid=c,
                name=cms[c]["name"],
                module=cms[c]["module"],
                section=cms[c]["sectionnumber"],
                visible=cms[c]["visible"],
                stealth=cms[c]["stealth"],
                url=cms[c].get("url"),
            ) for c in order if c in cms]
        except (KeyError, TypeError, ValueError) as e:
            raise MoodleError(f"The course state from Moodle isn't as roodle expects "
                              f"(missing or odd {e}): has Moodle changed?") from e

    def resources(self):
        """Every File resource in the course, hidden ones included."""
        from .resource import FileResource
        return [FileResource(self, m.cmid, m.name, m.visible, m.section, m.stealth)
                for m in self.modules() if m.module == "resource"]

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
