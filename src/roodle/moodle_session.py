"""
Interact with a Moodle session.
"""

import re
import json
import zipfile
from pathlib import Path
from datetime import datetime
import pandas as pd
from io import StringIO, BytesIO
from bs4 import BeautifulSoup
from .delta_time import time_expression
from .forms import find_moodleform, serialize_form


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
            "plugin": "offline_bath_2023",
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


def get_moodle_choice(s, host, sesskey, choice_id):
    r = s.post(
        host + "/mod/choice/report.php",
        data={
            "id": str(choice_id),
            "download": "txt",
            "sesskey": sesskey,
        },
    )
    return r.text


def get_moodle_assignment_submissions(s, host, sesskey, assignment_id, user_ids):
    r = s.post(
        host + "/mod/assign/view.php",
        data={
            "id": str(assignment_id),
            "action": "gradingbatchoperation",
            "id": assignment_id,
            "selectedusers": ",".join(str(x) for x in user_ids),
            "returnaction": "grading",
            "sesskey": sesskey,
            "_qf__mod_assign_grading_batch_operations_form": "1",
            "operation": "downloadselected",
            "submit": "Go",
        },
    )
    r.raise_for_status()
    try:
        z = zipfile.ZipFile(BytesIO(r.content))
        return z
    except Exception as e:
        print("Could not load assignment zip file: %s" % e)
        return None


def _get_sesskey(s, host):
    r = s.get(host)
    matches = re.findall(r"^M\.cfg = *(.*?);", r.text, re.MULTILINE)
    assert len(matches) == 1
    data = json.loads(matches[0])
    print("Loaded sesskey: ", data["sesskey"])
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
                    "jointype": 2,
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

        # Column 2 -- username
        username = "".join(tds[1].stripped_strings)

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
                "username": username,
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


def get_module_edit_form(s, host, cmid):
    """Fetch a course module's "Edit settings" page and scrape its form.

    Returns `(fields, draftitemid)`: `fields` is every field the form would
    submit if saved unchanged (see `serialize_form`), and `draftitemid` is
    the item id of the draft file area Moodle has already populated with
    the module's current files (from the `files` filemanager field) -- this
    is what a caller uploads/unzips into before saving.
    """
    r = s.get(host + "/course/modedit.php", params={"update": cmid, "return": 0})
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    form = find_moodleform(soup)
    fields = serialize_form(form)
    if "files" not in fields:
        raise KeyError(
            "No 'files' field on this module's edit form -- "
            "does it have a file manager?"
        )
    return fields, int(fields["files"])


def submit_module_edit_form(s, host, fields, submitbutton="Save and display"):
    fields = dict(fields)
    fields["submitbutton"] = submitbutton
    r = s.post(host + "/course/modedit.php", data=fields)
    r.raise_for_status()
    return r


def list_draft_files(s, host, sesskey, itemid, filepath="/", client_id="roodle"):
    r = s.post(
        host + "/repository/draftfiles_ajax.php",
        params={"action": "list"},
        data={
            "sesskey": sesskey,
            "client_id": client_id,
            "filepath": filepath,
            "itemid": itemid,
        },
    )
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise RuntimeError(f"Moodle draftfiles list error: {data['error']}")
    return data


def delete_draft_file(s, host, sesskey, itemid, filepath, filename, client_id="roodle"):
    r = s.post(
        host + "/repository/draftfiles_ajax.php",
        params={"action": "delete"},
        data={
            "sesskey": sesskey,
            "client_id": client_id,
            "filepath": filepath,
            "filename": filename,
            "itemid": itemid,
        },
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Moodle draftfiles delete error: {data['error']}")
    return data


def clear_draft_area(s, host, sesskey, itemid, client_id="roodle"):
    """Delete every file/folder currently in a draft file area."""
    data = list_draft_files(s, host, sesskey, itemid, client_id=client_id)
    for entry in data.get("list", []):
        delete_draft_file(
            s, host, sesskey, itemid, entry["filepath"], entry["filename"],
            client_id=client_id,
        )


def upload_file_to_draft(
    s, host, sesskey, itemid, ctx_id, repo_id, local_path, maxbytes, areamaxbytes=-1,
    client_id="roodle",
):
    """Upload a local file into a draft file area via the "Upload a file"
    repository, as the file manager's filepicker does."""
    local_path = Path(local_path)
    with open(local_path, "rb") as f:
        r = s.post(
            host + "/repository/repository_ajax.php",
            params={"action": "upload"},
            data={
                "repo_id": repo_id,
                "p": "/",
                "page": "",
                "env": "filemanager",
                "sesskey": sesskey,
                "client_id": client_id,
                "itemid": itemid,
                "maxbytes": maxbytes,
                "areamaxbytes": areamaxbytes,
                "ctx_id": ctx_id,
                "title": local_path.name,
                "author": "",
                "overwrite": 1,
            },
            files={"repo_upload_file": (local_path.name, f, "application/zip")},
        )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Moodle upload error: {data['error']}")
    return data


def unzip_draft_file(
    s, host, sesskey, itemid, filename, filepath="/", areamaxbytes=-1,
    client_id="roodle",
):
    r = s.post(
        host + "/repository/draftfiles_ajax.php",
        params={"action": "unzip"},
        data={
            "sesskey": sesskey,
            "client_id": client_id,
            "filepath": filepath,
            "itemid": itemid,
            "filename": filename,
            "areamaxbytes": areamaxbytes,
        },
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Moodle unzip error: {data['error']}")
    return data


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

    def user_id_by_username(self, username):
        df = self.users.set_index("username")
        return df.loc[username, "user_id"]

    def user_groups_by_username(self, username):
        df = self.users.set_index("username")
        return df.loc[username, "groups"]

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

    def set_user_groups(self, username, group_names):
        group_ids = [self.group_id_by_name(group_name) for group_name in group_names]
        user_id = self.user_id_by_username(username)
        user_groups = self.user_groups_by_username(username)

        if set(group_ids) != set(user_groups):
            print(
                f"{user_id} <{username}>: current {user_groups}, desired {group_ids}"
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

    def modify_user_groups(self, username, remove=None, add=None):
        user_id = self.user_id_by_username(username)
        user_groups = self.user_groups_by_username(username)
        remove_group_ids = {self.group_id_by_name(g) for g in (remove or [])}
        group_ids = [g for g in user_groups if g not in remove_group_ids]
        new_group_ids = [self.group_id_by_name(g) for g in (add or [])]
        group_ids += [g for g in new_group_ids if g not in group_ids]

        if set(group_ids) != set(user_groups):
            print(f"{user_id} <{username}>: current {user_groups}")
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

    def get_assignment_submissions(self, assignment_id, usernames):
        user_ids = [
            self.user_id_by_username(username)
            for username in usernames
        ]
        z = get_moodle_assignment_submissions(self.session, self.server, self._sesskey,
                                              assignment_id, user_ids)
        return z

    def get_grades(self):
        return get_moodle_grades(
            self.session, self.server, self._sesskey, self.course_id
        )

    def get_choice_results(self, choice_id):
        return get_moodle_choice(self.session, self.server, self._sesskey, choice_id)

    def replace_resource_files(
        self, cmid, zip_path, ctx_id, repo_id, maxbytes, areamaxbytes=-1, unzip=True,
    ):
        """Replace the files behind a course module (e.g. a "File" resource
        used to host a static site, or a "Folder") with the contents of a
        local zip.

        Wipes whatever files the module currently has, uploads `zip_path`,
        and (if `unzip`) unzips and removes the zip itself, then saves the
        module -- mirroring the "Edit settings -> delete old files -> Add
        -> Upload a file -> Unzip -> delete zip -> Save and display" steps
        you'd do by hand.

        `ctx_id` (the module's context id) and `repo_id` (the id of the
        "Upload a file" repository instance) are stable per Moodle site/
        module and are best found once from a browser network capture of a
        manual upload; `maxbytes` is the course's max upload size in bytes.
        """
        fields, itemid = get_module_edit_form(self.session, self.server, cmid)
        sesskey = fields["sesskey"]

        print(f"cmid {cmid}: draft item {itemid}, clearing existing files")
        if self.dry_run:
            print(f"[dry run] would clear draft area, upload {zip_path}, "
                  f"unzip, and save cmid={cmid}")
            return

        clear_draft_area(self.session, self.server, sesskey, itemid)

        zip_name = Path(zip_path).name
        print(f"cmid {cmid}: uploading {zip_path}")
        upload_file_to_draft(
            self.session, self.server, sesskey, itemid, ctx_id, repo_id,
            zip_path, maxbytes=maxbytes, areamaxbytes=areamaxbytes,
        )

        if unzip:
            print(f"cmid {cmid}: unzipping {zip_name}")
            unzip_draft_file(
                self.session, self.server, sesskey, itemid, zip_name,
                areamaxbytes=areamaxbytes,
            )
            delete_draft_file(self.session, self.server, sesskey, itemid, "/", zip_name)

        print(f"cmid {cmid}: saving module")
        resp = submit_module_edit_form(self.session, self.server, fields)
        print(f"cmid {cmid}: saved, now at {resp.url}")
        return resp
