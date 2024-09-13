"""
Interact with a Moodle session.
"""

import re
import json
from pathlib import Path
from datetime import datetime
import pandas as pd
from io import StringIO
from bs4 import BeautifulSoup
from .delta_time import time_expression


def get_moodle_grades(s, host, sesskey, course_id):
    r = s.post(
        host + "/grade/export/txt/export.php",
        data={
            "id": str(course_id),
            # XXX unfortunately the ids for assignments are internal
            # not the easily-found ids
            "sesskey": sesskey,
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
    )
    return r.text


def get_moodle_assignment_data_bath(s, host, assignment_id):
    r = s.get(
        host + "/mod/assign/view.php",
        params={
            "id": str(assignment_id),
            "plugin": "offline_bath",
            "pluginsubtype": "assignfeedback",
            "action": "viewpluginpage",
            "pluginaction": "downloadgrades",
        },
    )
    return r.text


def get_moodle_assignment_data(s, host, assignment_id):
    r = s.get(
        host + "/mod/assign/view.php",
        params={
            "id": str(assignment_id),
            "plugin": "offline",
            "pluginsubtype": "assignfeedback",
            "action": "viewpluginpage",
            "pluginaction": "downloadgrades",
        },
    )
    return r.text


def get_moodle_choicegroup(s, host, choice_id):
    r = s.post(
        host + "/mod/choicegroup/report.php",
        data={
            "id": str(choice_id),
            "download": "txt",
        },
    )
    return r.text


def _get_sesskey(s, host):
    r = s.get(host)
    matches = re.findall(r"^M\.cfg = *(.*?);", r.text, re.MULTILINE)
    assert len(matches) == 1
    data = json.loads(matches[0])
    return data["sesskey"]


def get_users_table_html(s, host, sesskey, course_id):
    r = s.post(
        host + "/lib/ajax/service.php",
        params={"sesskey": sesskey, "info": "core_table_get_dynamic_table_content"},
        json=[
            {
                "index": 0,
                "methodname": "core_table_get_dynamic_table_content",
                "args": {
                    "component": "core_user",
                    "handler": "participants",
                    "uniqueid": f"user-index-participants-{course_id}",
                    "sortdata": [
                        {"sortby": "lastname", "sortorder": 3},
                        {"sortby": "lastname", "sortorder": 4},
                    ],
                    "jointype": 1,
                    "filters": {
                        "courseid": {
                            "name": "courseid",
                            "jointype": 1,
                            "values": [course_id],
                        }
                    },
                    "firstinitial": "",
                    "lastinitial": "",
                    "pagenumber": "1",
                    "pagesize": "5000",
                    "hiddencolumns": [],
                    "resetpreferences": False,
                },
            }
        ],
    )
    r.raise_for_status()
    data = r.json()
    assert len(data) == 1
    if data[0]["error"]:
        raise RuntimeError(
            f"{data[0]['exception']['errorcode']}: {data[0]['exception']['message']}"
        )
    return data[0]["data"]["html"]


def parse_users_table_html(html):
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    data = []
    group_options = None
    for row in table.find("tbody").find_all("tr"):
        tds = list(row.find_all("td"))
        if not tds or not tds[0].find("input"):
            continue

        # Column 1 -- get the Moodle user ID
        user_id = tds[0].find("input")["id"]
        assert user_id.startswith("user")
        user_id = int(user_id[4:])

        # Column 2 -- email
        email = "".join(tds[1].stripped_strings)

        # Column 3 -- roles

        # Column 4 -- groups
        row_group_options = json.loads(tds[3].find("span")["data-options"])["options"]
        if group_options is None:
            group_options = row_group_options
        else:
            assert row_group_options == group_options
        row_groups = json.loads(tds[3].find("span")["data-value"])

        # Column 5 -- last access
        last_access_str = "".join(tds[4].stripped_strings)
        if last_access_str == "Never":
            last_access_secs = float("inf")
        else:
            last_access_secs = time_expression.parseString(last_access_str + " late")[
                "total_seconds"
            ]

        # Column 6 -- enrolments

        data.append(
            {
                "user_id": user_id,
                "email": email,
                "groups": [int(g) for g in row_groups],
                "last_access": last_access_str,
                "seconds_since_last_access": last_access_secs,
            }
        )
    df = pd.DataFrame.from_records(data)
    return group_options, df


def set_user_groups(s, host, sesskey, course_id, user_id, group_ids):
    r = s.post(
        host + "/lib/ajax/service.php",
        params={"sesskey": sesskey, "info": "core_update_inplace_editable"},
        json=[
            {
                "index": 0,
                "methodname": "core_update_inplace_editable",
                "args": {
                    "component": "core_group",
                    "itemid": f"{course_id}:{user_id}",
                    "itemtype": "user_groups",
                    "value": json.dumps([str(g) for g in group_ids]),
                },
            }
        ],
    )
    r.raise_for_status()
    data = r.json()
    assert len(data) == 1
    assert not data[0]["error"]
    # Don't know why it's a string in some places and an int in others...
    assert json.loads(data[0]["data"]["value"]) == [int(x) for x in group_ids]
    return True


class MoodleCourse:
    def __init__(self, server, course_id, dry_run=False):
        self.server = server
        self.course_id = course_id
        self._sesskey = None
        self.session = None
        self.groups = []
        self.users = pd.DataFrame()
        self.dry_run = dry_run

    def connect(self, session):
        self.session = session
        self._sesskey = _get_sesskey(session, self.server)

    def fetch_users(self):
        html = get_users_table_html(
            self.session, self.server, self._sesskey, self.course_id
        )
        self.groups, self.users = parse_users_table_html(html)

    def group_id_by_name(self, group_name):
        for g in self.groups:
            if g["value"] == group_name:
                return g["key"]
        raise KeyError(group_name)

    def user_id_by_email(self, user_email):
        df = self.users.set_index("email")
        return df.loc[user_email, "user_id"]

    def user_groups_by_email(self, user_email):
        df = self.users.set_index("email")
        return df.loc[user_email, "groups"]

    def _print_group_diff(self, before, after):
        removed = set(before) - set(after)
        if removed:
            for g in self.groups:
                if g["key"] in removed:
                    print(f"    - {g['key']} ({g['value']})")
        extra = set(after) - set(before)
        if extra:
            for g in self.groups:
                if g["key"] in extra:
                    print(f"    + {g['key']} ({g['value']})")

    def set_user_groups(self, user_email, group_names):
        group_ids = [self.group_id_by_name(group_name) for group_name in group_names]
        user_id = self.user_id_by_email(user_email)
        user_groups = self.user_groups_by_email(user_email)

        if set(group_ids) != set(user_groups):
            print(
                f"{user_id} <{user_email}>: current {user_groups}, desired {group_ids}"
            )
            self._print_group_diff(user_groups, group_ids)

            if not self.dry_run:
                return set_user_groups(
                    self.session,
                    self.server,
                    self._sesskey,
                    self.course_id,
                    user_id,
                    group_ids,
                )

    def modify_user_groups(self, user_email, remove=None, add=None):
        user_id = self.user_id_by_email(user_email)
        user_groups = self.user_groups_by_email(user_email)
        remove_group_ids = {self.group_id_by_name(g) for g in (remove or [])}
        group_ids = [g for g in user_groups if g not in remove_group_ids]
        new_group_ids = [self.group_id_by_name(g) for g in (add or [])]
        group_ids += [g for g in new_group_ids if g not in group_ids]

        if set(group_ids) != set(user_groups):
            print(f"{user_id} <{user_email}>: current {user_groups}")
            self._print_group_diff(user_groups, group_ids)

            if not self.dry_run:
                return set_user_groups(
                    self.session,
                    self.server,
                    self._sesskey,
                    self.course_id,
                    user_id,
                    group_ids,
                )

    def get_assignment_data(self, assignment_id, bath_version=False):
        if bath_version:
            return get_moodle_assignment_data_bath(
                self.session, self.server, assignment_id
            )
        else:
            return get_moodle_assignment_data(self.session, self.server, assignment_id)

    def get_grades(self):
        return get_moodle_grades(
            self.session, self.server, self._sesskey, self.course_id
        )
