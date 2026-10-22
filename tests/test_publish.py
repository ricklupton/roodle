import pytest

from roodle import cli
from roodle.publish import Config, ConfigError, plan

CONFIG = """
server = "https://moodle.example.ac.uk"
course = 101
publish_files = ["build/slides/*.html", "build/slides/*.pdf", "build/sheets/*.pdf"]

[[publish]]
path = "build/book"
cmid = 20

[[publish]]
path = "build/extra/notes.pdf"
"""


def write(root, files):
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


@pytest.fixture
def project(tmp_path):
    (tmp_path / "moodle.toml").write_text(CONFIG)
    write(tmp_path, {
        "build/slides/lecture-01.html": "deck 1",
        "build/slides/lecture-01.pdf": "deck 1 pdf",
        "build/slides/lecture-02.html": "deck 2",
        "build/slides/.DS_Store": "",
        "build/sheets/sheet-01.pdf": "sheet",
        "build/book/index.html": "home",
        "build/book/site_libs/x.js": "js",
        "build/extra/notes.pdf": "notes",
    })
    return tmp_path


@pytest.fixture
def config(project):
    return Config.load(project / "moodle.toml")


# ------------------------------------------------------------------ config

@pytest.mark.parametrize("text, message", [
    ('course = 1', "server"),
    ('server = "x"', "course"),
    ('server = "x"\ncourse = 1\nsurprise = 2', "unknown setting surprise"),
    ('server = "x"\ncourse = 1\n[[publish]]\npath = "build/book"', "needs a cmid"),
    ('server = "x"\ncourse = 1\n[[publish]]\npath = "a"\ncmid = "12"', "should be a number"),
    ('server = "x"\ncourse = 1\n[[publish]]\npath = "a"\nid = 12', "unknown setting id"),
    ('server = "x"\ncourse = 1\npublish_files = "*.pdf"', "list of patterns"),
])
def test_bad_config(project, text, message):
    (project / "moodle.toml").write_text(text)
    with pytest.raises(ConfigError, match=message):
        Config.load(project / "moodle.toml")


def test_missing_config(tmp_path):
    with pytest.raises(ConfigError, match="-c"):
        Config.load(tmp_path / "moodle.toml")


def test_items(config):
    items, missing = config.items()
    assert [(i.rel, i.cmid) for i in items] == [
        ("build/book", 20), ("build/extra/notes.pdf", None),
        ("build/sheets/sheet-01.pdf", None), ("build/slides/lecture-01.html", None),
        ("build/slides/lecture-01.pdf", None), ("build/slides/lecture-02.html", None)]
    assert missing == []


def test_unbuilt_and_pinned_items(project):
    (project / "moodle.toml").write_text(CONFIG + """
[[publish]]
path = "build/slides/lecture-02.html"
cmid = 12

[[publish]]
path = "build/later.pdf"
""")
    items, missing = Config.load(project / "moodle.toml").items()
    assert {i.rel: i.cmid for i in items}["build/slides/lecture-02.html"] == 12
    assert missing == ["build/later.pdf"]


def test_glob_matching_a_directory(project):
    (project / "moodle.toml").write_text(CONFIG.replace('"build/sheets/*.pdf"', '"build/*"'))
    with pytest.raises(ConfigError, match="directory"):
        Config.load(project / "moodle.toml").items()


def test_select(config, project, monkeypatch):
    items, _ = config.items()
    rels = lambda names: [i.rel for i in config.select(names, items)]  # noqa: E731
    assert rels(["build/slides/lecture-02.html"]) == ["build/slides/lecture-02.html"]
    assert rels(["lecture-02.html"]) == ["build/slides/lecture-02.html"]
    assert rels(["lecture-01"]) == ["build/slides/lecture-01.html", "build/slides/lecture-01.pdf"]
    assert rels(["build/book/"]) == ["build/book"]
    monkeypatch.chdir(project / "build" / "slides")
    assert rels(["./lecture-02.html"]) == ["build/slides/lecture-02.html"]
    with pytest.raises(ConfigError, match="isn't anything"):
        config.select(["lecture-09.html"], items)


def test_check_names_before_a_build(config):
    config.check_names(["lecture-09.html", "lecture-09", "book", "sheet-05.pdf"])
    with pytest.raises(ConfigError):
        config.check_names(["notebook.ipynb"])


# -------------------------------------------------------------------- plan

def set_up_moodle(fake, project):
    read = lambda rel: (project / rel).read_bytes()  # noqa: E731
    fake.add(11, "Lecture 1 slides", {"lecture-01.html": b"old deck"})
    fake.add(12, "Lecture 1 PDF", {"lecture-01.pdf": read("build/slides/lecture-01.pdf")})
    fake.add(13, "Sheet 1 (copy)", {"sheet-01.pdf": b"x"})
    fake.add(14, "Sheet 1", {"sheet-01.pdf": b"y"}, visible=False)
    fake.add(20, "Book", {"index.html": read("build/book/index.html"),
                          "site_libs/x.js": read("build/book/site_libs/x.js")})
    fake.add(21, "Last year's book", {"index.html": b"old"})


def test_plan(fake, moodle, config, project):
    set_up_moodle(fake, project)
    p = plan(moodle.course(101), config)
    by_rel = {t.item.rel: t for t in p.targets}
    assert (by_rel["build/slides/lecture-01.html"].resource.cmid,
            by_rel["build/slides/lecture-01.html"].status) == (11, "changed")
    assert by_rel["build/slides/lecture-01.pdf"].status == "same"
    assert by_rel["build/book"].status == "same"           # pinned: 21 isn't a candidate
    assert "more than one resource" in by_rel["build/sheets/sheet-01.pdf"].note
    assert "no resource" in by_rel["build/slides/lecture-02.html"].note
    assert [t.item.rel for t in p.pending()] == ["build/slides/lecture-01.html"]
    assert len(p.pending(force=True)) == 3


def test_plan_with_pinned_cmid_holding_another_file(fake, moodle, config, project):
    set_up_moodle(fake, project)
    fake.resources[20].files = {"other.pdf": b"x"}
    [t] = plan(moodle.course(101), config, only=[i for i in config.items()[0]
                                                 if i.rel == "build/book"]).targets
    assert (t.status, t.held, t.held_ok) == ("changed", "other.pdf", False)


def test_plan_with_cmid_that_is_not_a_file_resource(fake, moodle, config, project):
    set_up_moodle(fake, project)
    fake.resources[20].module = "folder"
    [t] = plan(moodle.course(101), config, only=[i for i in config.items()[0]
                                                 if i.rel == "build/book"]).targets
    assert t.resource is None and "isn't a File resource" in t.note


def test_two_built_files_with_one_name(fake, moodle, project):
    (project / "moodle.toml").write_text(CONFIG.replace(
        '"build/sheets/*.pdf"', '"build/sheets/*.pdf", "build/again/*.pdf"'))
    write(project, {"build/again/sheet-01.pdf": "again"})
    set_up_moodle(fake, project)
    p = plan(moodle.course(101), Config.load(project / "moodle.toml"))
    notes = {t.item.rel: t.note for t in p.unmatched}
    assert "more than one built file" in notes["build/again/sheet-01.pdf"]


# --------------------------------------------------------------------- cli

@pytest.fixture
def run(fake, project, monkeypatch, capsys):
    monkeypatch.setattr(cli.Moodle, "from_firefox", lambda server: fake.moodle())
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail(f"asked: {prompt}"))

    def run(*args):
        status = cli.main([*args, "-c", str(project / "moodle.toml")])
        return status, capsys.readouterr().out
    return run


def test_publish(fake, project, run):
    set_up_moodle(fake, project)
    status, out = run("publish", "--dry-run")
    assert status == 0 and ("Would upload (2 unchanged):\n  build/slides/lecture-01.html\n"
                            "      -> 'Lecture 1 slides', cmid 11, shown to students\n") in out
    assert fake.resources[11].files == {"lecture-01.html": b"old deck"}

    status, out = run("publish")
    assert status == 0, out
    assert "-> 'Lecture 1 slides', cmid 11, shown to students" in out
    assert "Uploaded 1." in out
    assert fake.resources[11].files == {"lecture-01.html": b"deck 1"}
    assert "no resource on Moodle yet" in out

    status, out = run("publish")
    assert "Moodle is up to date: 3 resources hold the same as the build." in out


def test_publish_named_files(fake, project, run):
    set_up_moodle(fake, project)
    status, out = run("publish", "lecture-01")
    assert status == 0 and "Uploaded 2." in out
    status, out = run("publish", "lecture-02.html")
    assert status == 1 and "no resource on Moodle yet" in out


def test_publish_check(fake, project, run):
    status, out = run("publish", "--check", "lecture-09.html")
    assert status == 0 and out == ""
    fake.logged_in = False
    status, out = run("publish", "--check")
    assert "Not logged in" in out
    with pytest.raises(SystemExit, match="isn't anything"):
        run("publish", "--check", "notebook.ipynb")


def test_publish_asks_about_a_pinned_resource_holding_another_file(
        fake, project, run, monkeypatch):
    set_up_moodle(fake, project)
    fake.resources[20].files = {"other.pdf": b"x"}
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    with pytest.raises(SystemExit, match="Not uploading"):
        run("publish", "book")
    assert fake.resources[20].files == {"other.pdf": b"x"}


def test_publish_retries_a_failed_upload(fake, project, run, monkeypatch):
    set_up_moodle(fake, project)
    handle, failures = fake.handle, [1]

    def flaky(request):
        if request.url.path == "/repository/repository_ajax.php" and failures:
            failures.pop()
            return fake_error()
        return handle(request)

    import httpx

    def fake_error():
        return httpx.Response(500)
    fake.handle = flaky
    monkeypatch.setattr("builtins.input", lambda prompt: "r")
    status, out = run("publish")
    assert status == 0 and "failed" in out and "Uploaded 1." in out
    assert fake.resources[11].files == {"lecture-01.html": b"deck 1"}


def test_resources(fake, project, run):
    set_up_moodle(fake, project)
    status, out = run("resources")
    assert status == 0
    assert "'Lecture 1 slides'  cmid 11, shown to students\n        holds lecture-01.html" \
        "  <- build/slides/lecture-01.html" in out
    assert "'Sheet 1'  cmid 14, hidden from students" in out
    assert "build/slides/lecture-02.html: no resource on Moodle yet" in out


def test_visibility_as_moodle_shows_it(fake, project, run):
    set_up_moodle(fake, project)
    fake.resources[11].stealth = True
    status, out = run("publish", "--dry-run")
    assert "cmid 11, available but not shown on course page\n" in out
    fake.use_course_state = False          # the Files index page can't tell
    status, out = run("publish", "--dry-run")
    assert "cmid 11, visible to students\n" in out
