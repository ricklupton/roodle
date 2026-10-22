import json

from roodle.course import Module


def test_modules_from_course_state(fake, moodle):
    fake.add(11, "Lecture 1", {"l1.html": b"x"}, section=2)
    fake.add(12, "Quiz", module="quiz", section=1)
    fake.add(13, "Hidden notes", {"n.pdf": b"y"}, visible=False, section=1)
    modules = moodle.course(101).modules()
    assert [(m.cmid, m.module, m.section, m.visible, m.stealth) for m in modules] == \
        [(12, "quiz", 1, True, False), (13, "resource", 1, False, False),
         (11, "resource", 2, True, False)]
    assert isinstance(modules[0], Module)
    assert [(r.cmid, r.name) for r in moodle.course(101).resources()] == \
        [(13, "Hidden notes"), (11, "Lecture 1")]


def test_course_state_without_a_field_it_needs(fake):
    import httpx
    import pytest
    from roodle import MoodleError
    fake.add(11, "Lecture 1", {"l1.html": b"x"})
    handle = fake.handle

    def without_stealth(request):
        r = handle(request)
        if request.url.path == "/lib/ajax/service.php":
            [result] = r.json()
            state = json.loads(result["data"])
            for cm in state["cm"]:
                del cm["stealth"]
            return httpx.Response(200, json=[{**result, "data": json.dumps(state)}])
        return r
    fake.handle = without_stealth
    with pytest.raises(MoodleError, match="stealth"):
        fake.moodle().course(101).resources()
