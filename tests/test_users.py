import json
import math

import pytest

from roodle.users import Group, Participants, parse_participants_table, seconds_ago


def test_seconds_ago():
    assert seconds_ago("Never") == math.inf
    assert seconds_ago("2 days 3 hours") == 2 * 86400 + 3 * 3600
    assert seconds_ago("1 min 10 secs") == 70
    assert seconds_ago("now") == 0
    assert seconds_ago("whenever") is None


def row(user_id, username, groups, last_access, options):
    editable = (f"<span data-options='{json.dumps({'options': options})}' "
                f"data-value='{json.dumps([str(g) for g in groups])}'></span>")
    return (f'<tr><td><input id="user{user_id}"></td><td>{username}</td><td>Student</td>'
            f"<td>{editable}</td><td>{last_access}</td><td>Active</td></tr>")


OPTIONS = [{"key": "7", "value": "SD-101"}, {"key": "8", "value": "SD-102"},
           {"key": "9", "value": "Solo groups"}]


@pytest.fixture
def participants():
    html = ("<table><tbody>"
            + row(1, "abc12", [7], "2 days", OPTIONS)
            + row(2, "def34", [], "Never", OPTIONS)
            + '<tr class="emptyrow"><td></td></tr></tbody></table>')
    return Participants(None, *parse_participants_table(html))


def test_parse(participants):
    assert participants.groups[0] == Group(7, "SD-101")
    assert participants["abc12"].groups == [7]
    assert participants["def34"].seconds_since_last_access == math.inf
    assert len(participants) == 2


def test_group_changes_dry_run(participants):
    change = participants.modify_groups("abc12", remove=["SD-101"],
                                        add=["SD-102", "Solo groups"], dry_run=True)
    assert [g.name for g in change.removed] == ["SD-101"]
    assert [g.name for g in change.added] == ["SD-102", "Solo groups"]
    assert participants.modify_groups("abc12", add=["SD-101"], dry_run=True) is None
    assert participants.set_groups("def34", [], dry_run=True) is None
