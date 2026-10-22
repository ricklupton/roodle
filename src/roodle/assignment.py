"""
Assignment activities: their grading worksheets and submissions.
"""

import zipfile
from io import BytesIO

from .errors import MoodleError


class Assignment:
    def __init__(self, course, cmid):
        self.course = course
        self.cmid = int(cmid)

    def __repr__(self):
        return f"Assignment(cmid={self.cmid})"

    @property
    def moodle(self):
        return self.course.moodle

    def grading_worksheet(self, plugin="offline"):
        """The "offline grading worksheet" CSV, as its download gives it.

        `plugin` is the offline feedback plugin: "offline" is Moodle's own;
        Bath's is "offline_bath_2023".
        """
        r = self.moodle.check(self.moodle.get("/mod/assign/view.php", params={
            "id": str(self.cmid),
            "plugin": plugin,
            "pluginsubtype": "assignfeedback",
            "action": "viewpluginpage",
            "pluginaction": "downloadgrades",
        }))
        return r.text

    def download_submissions(self, usernames):
        """The submissions of these users, as the zip that "Download
        selected submissions" gives."""
        participants = self.course.participants()
        user_ids = [participants[u].id for u in usernames]
        r = self.moodle.check(self.moodle.post("/mod/assign/view.php", data={
            "id": str(self.cmid),
            "action": "gradingbatchoperation",
            "selectedusers": ",".join(str(x) for x in user_ids),
            "returnaction": "grading",
            "sesskey": self.moodle.sesskey,
            "_qf__mod_assign_grading_batch_operations_form": "1",
            "operation": "downloadselected",
            "submit": "Go",
        }, timeout=None))
        try:
            return zipfile.ZipFile(BytesIO(r.content))
        except zipfile.BadZipFile as e:
            raise MoodleError(f"Moodle didn't send a zip of the submissions: {e}") from e
