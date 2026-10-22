import pytest

from roodle import NotAFileResource, TooLarge
from roodle.resource import HeldFile


@pytest.fixture
def course(moodle):
    return moodle.course(101)


def test_held_file(fake, course):
    fake.add(11, "Lecture 1", {"lecture 1.html": b"x"})
    fake.add(12, "Empty")
    assert course.resource(11).held_file() == HeldFile("lecture 1.html", 5011)
    assert course.resource(12).held_file() is None


def test_compare_file(fake, course, tmp_path):
    f = tmp_path / "l1.html"
    f.write_bytes(b"new")
    fake.add(11, "Lecture 1", {"l1.html": b"new"})
    assert course.resource(11).compare(f).status == "same"
    fake.resources[11].files["l1.html"] = b"old"
    assert course.resource(11).compare(f).status == "changed"
    fake.resources[11].filtered = True
    assert course.resource(11).compare(f).status == "unknown"


def test_compare_with_another_file_held(fake, course, tmp_path):
    f = tmp_path / "l1.html"
    f.write_bytes(b"new")
    fake.add(11, "Lecture 1", {"other.html": b"new"})
    c = course.resource(11).compare(f)
    assert (c.status, c.held) == ("changed", "other.html")


def make_site(root, pages):
    for name, text in pages.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    return root


def test_compare_directory(fake, course, tmp_path):
    pages = {"index.html": "home", "a.html": "a", "site_libs/x.js": "js"}
    site = make_site(tmp_path / "site", pages)
    fake.add(20, "Book", {k: v.encode() for k, v in pages.items()})
    assert course.resource(20).compare(site).status == "same"
    (site / "site_libs" / "x.js").write_text("js2")
    assert course.resource(20).compare(site).status == "changed"
    (site / "new.html").write_text("new")
    (site / "site_libs" / "x.js").write_text("js")
    assert course.resource(20).compare(site).status == "changed"


def test_replace_file(fake, course, tmp_path):
    f = tmp_path / "l1.pdf"
    f.write_bytes(b"%PDF new")
    fake.add(11, "Lecture 1", {"l1.pdf": b"%PDF old", "stray.txt": b"x"})
    resource = course.resource(11)
    resource.replace(f)
    assert fake.resources[11].files == {"l1.pdf": b"%PDF new"}
    assert resource.compare(f).status == "same"
    assert ("POST", "/repository/draftfiles_ajax.php", "unzip") not in fake.requests


def test_replace_directory(fake, course, tmp_path):
    site = make_site(tmp_path / "site", {"index.html": "home", "site_libs/x.js": "js"})
    fake.add(20, "Book", {"index.html": b"old", "gone.html": b"old",
                          "site_libs/old.js": b"old"})
    course.resource(20).replace(site)
    assert fake.resources[20].files == {"index.html": b"home", "site_libs/x.js": b"js"}
    assert course.resource(20).compare(site).status == "same"


def test_replace_refuses_other_modules(fake, course, tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"x")
    fake.add(30, "A folder", module="folder")
    with pytest.raises(NotAFileResource):
        course.resource(30).replace(f)


def test_replace_too_large_changes_nothing(fake, course, tmp_path):
    f = tmp_path / "big.pdf"
    f.write_bytes(b"x" * 1_000_001)
    fake.add(11, "Big", {"big.pdf": b"small"})
    with pytest.raises(TooLarge):
        course.resource(11).replace(f)
    assert fake.resources[11].files == {"big.pdf": b"small"}
    assert ("POST", "/repository/draftfiles_ajax.php", "delete") not in fake.requests


def test_compare_file_served_from_an_object_store(fake, course, tmp_path):
    f = tmp_path / "book.pdf"
    f.write_bytes(b"%PDF new")
    fake.add(11, "Book PDF", {"book.pdf": b"%PDF new"}, object_store="hash")
    assert course.resource(11).compare(f).status == "same"
    fake.resources[11].files["book.pdf"] = b"%PDF old"
    assert course.resource(11).compare(f).status == "changed"
    fake.resources[11].object_store = "other"
    assert course.resource(11).compare(f).status == "unknown"


def test_compare_logged_out(fake, course, tmp_path):
    from roodle import NotLoggedIn
    f = tmp_path / "l1.html"
    f.write_bytes(b"x")
    fake.add(11, "Lecture 1", {"l1.html": b"x"})
    resource = course.resource(11)
    resource.held_file()
    fake.logged_in = False
    with pytest.raises(NotLoggedIn):
        resource.compare(f)
