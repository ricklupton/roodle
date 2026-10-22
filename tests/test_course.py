from roodle.course import Module


def test_modules_from_course_state(fake, moodle):
    fake.add(11, "Lecture 1", {"l1.html": b"x"}, section=2)
    fake.add(12, "Quiz", module="quiz", section=1)
    fake.add(13, "Hidden notes", {"n.pdf": b"y"}, visible=False, section=1)
    modules = moodle.course(101).modules()
    assert [(m.cmid, m.module, m.section, m.visible) for m in modules] == \
        [(12, "quiz", 1, True), (13, "resource", 1, False), (11, "resource", 2, True)]
    assert isinstance(modules[0], Module)
    assert [(r.cmid, r.name) for r in moodle.course(101).resources()] == \
        [(13, "Hidden notes"), (11, "Lecture 1")]


def test_resources_from_index_page_without_course_state(fake, moodle):
    fake.use_course_state = False
    fake.add(11, "Lecture 1", {"l1.html": b"x"})
    fake.add(13, "Hidden notes", {"n.pdf": b"y"}, visible=False)
    found = moodle.course(101).resources()
    assert [(r.cmid, r.name, r.visible) for r in found] == \
        [(11, "Lecture 1", True), (13, "Hidden notes", False)]
