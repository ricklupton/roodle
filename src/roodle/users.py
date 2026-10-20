"""
A course's participants, and the groups they are in.
"""

import json
import math
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from .errors import MoodleError


@dataclass(frozen=True)
class Group:
    id: int
    name: str


@dataclass
class User:
    id: int
    username: str
    groups: list           # group ids
    last_access: str       # as Moodle shows it: "2 days 3 hours", "Never"
    seconds_since_last_access: float | None


@dataclass(frozen=True)
class GroupChange:
    """A change to one user's groups, made or (with dry_run) only planned."""

    user: User
    added: list
    removed: list

    def __str__(self):
        lines = [f"{self.user.id} <{self.user.username}>"]
        lines += [f"    - {g.id} ({g.name})" for g in self.removed]
        lines += [f"    + {g.id} ({g.name})" for g in self.added]
        return "\n".join(lines)


UNIT_SECONDS = {"year": 365 * 86400, "week": 7 * 86400, "day": 86400, "hour": 3600,
                "minute": 60, "min": 60, "second": 1, "sec": 1}


def seconds_ago(text):
    """How long ago a "last access" time like "2 days 3 hours" was, in
    seconds: infinite for "Never", None if it can't be read."""
    if text.strip().lower() == "never":
        return math.inf
    items = re.findall(r"(\d+)\s*(year|week|day|hour|minute|min|second|sec)s?\b",
                       text, re.I)
    if not items:
        return 0.0 if "now" in text.lower() else None
    return float(sum(int(n) * UNIT_SECONDS[unit.lower()] for n, unit in items))


def parse_participants_table(html):
    """The (groups, users) in the participants table's HTML."""
    table = BeautifulSoup(html, "lxml").find("table")
    users, groups = [], None
    for row in table.find("tbody").find_all("tr"):
        tds = row.find_all("td")
        if not tds or not tds[0].find("input"):
            continue
        user_id = tds[0].find("input")["id"]
        if not user_id.startswith("user"):
            raise MoodleError(f"Unexpected participants table row: {user_id}")
        editable = tds[3].find("span")
        if groups is None:
            options = json.loads(editable["data-options"])["options"]
            groups = [Group(int(o["key"]), o["value"]) for o in options]
        last_access = "".join(tds[4].stripped_strings)
        users.append(User(
            id=int(user_id[4:]),
            username="".join(tds[1].stripped_strings),
            groups=[int(g) for g in json.loads(editable["data-value"])],
            last_access=last_access,
            seconds_since_last_access=seconds_ago(last_access),
        ))
    return groups or [], users


class Participants:
    """Everyone enrolled on a course, and the course's groups.

    Index by username for a `User`; iterate for all of them.
    """

    def __init__(self, course, groups, users):
        self.course = course
        self.groups = groups
        self.users = {u.username: u for u in users}

    @classmethod
    def load(cls, course):
        data = course.moodle.ajax("core_table_get_dynamic_table_content", {
            "component": "core_user",
            "handler": "participants",
            "uniqueid": f"user-index-participants-{course.id}",
            "sortdata": [{"sortby": "lastname", "sortorder": 3},
                         {"sortby": "lastname", "sortorder": 4}],
            "jointype": 2,
            "filters": {"courseid": {"name": "courseid", "jointype": 1,
                                     "values": [course.id]}},
            "firstinitial": "",
            "lastinitial": "",
            "pagenumber": "1",
            "pagesize": "5000",
            "hiddencolumns": [],
            "resetpreferences": False,
        })
        return cls(course, *parse_participants_table(data["html"]))

    def __getitem__(self, username):
        return self.users[username]

    def __iter__(self):
        return iter(self.users.values())

    def __len__(self):
        return len(self.users)

    def group(self, name):
        for g in self.groups:
            if g.name == name:
                return g
        raise KeyError(name)

    def set_groups(self, username, group_names, dry_run=False):
        """Put a user in exactly these groups.  Returns the change, or None
        if there is none."""
        return self._change(self[username], [self.group(n).id for n in group_names],
                            dry_run)

    def modify_groups(self, username, remove=(), add=(), dry_run=False):
        """Take a user out of some groups and put them in others, leaving
        the rest alone.  Returns the change, or None if there is none."""
        user = self[username]
        drop = {self.group(n).id for n in remove}
        ids = [g for g in user.groups if g not in drop]
        ids += [g for g in (self.group(n).id for n in add) if g not in ids]
        return self._change(user, ids, dry_run)

    def _change(self, user, group_ids, dry_run):
        if set(group_ids) == set(user.groups):
            return None
        by_id = {g.id: g for g in self.groups}
        change = GroupChange(
            user,
            added=[by_id[g] for g in group_ids if g not in user.groups],
            removed=[by_id[g] for g in user.groups if g not in group_ids and g in by_id],
        )
        if not dry_run:
            data = self.course.moodle.ajax("core_update_inplace_editable", {
                "component": "core_group",
                "itemid": f"{self.course.id}:{user.id}",
                "itemtype": "user_groups",
                "value": json.dumps([str(g) for g in group_ids]),
            })
            if sorted(int(g) for g in json.loads(data["value"])) != sorted(group_ids):
                raise MoodleError(f"Moodle didn't set {user.username}'s groups")
            user.groups = list(group_ids)
        return change
