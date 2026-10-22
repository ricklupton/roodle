"""
Publish built files to a course's File resources, as a config file lists
them -- what `roodle publish` does.

    server = "https://moodle.example.ac.uk"
    course = 12345

    # Built files, each published to the File resource that holds a file of
    # the same name.
    publish_files = ["_build/slides/*.html", "_build/sheets/*.pdf"]

    # One path each: a directory, which needs a cmid, or a single file, with
    # an optional cmid to pin it to a resource.
    [[publish]]
    path = "_build/book"
    cmid = 1594533

Paths and patterns are relative to the config file.  Matching by name means
no ids need writing down: make a resource by dragging the built file onto
the course page, and from then on it is kept up to date.  A resource that a
cmid pins is left out of the matching by name.
"""

import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

import httpx

from .errors import MoodleError, NotLoggedIn
from .resource import local_files

DEFAULT_CONFIG = "moodle.toml"


class ConfigError(ValueError):
    """The config file is missing or wrong, or names something it can't."""


@dataclass(frozen=True)
class Item:
    """A built file or directory to publish."""

    path: Path               # absolute
    rel: str                 # relative to the config file
    cmid: int | None = None  # pinned to this resource

    @property
    def name(self):
        return self.path.name


@dataclass
class Config:
    file: Path
    server: str
    course: int
    publish_files: list = field(default_factory=list)
    pinned: list = field(default_factory=list)   # [(rel, cmid or None)]

    @property
    def root(self):
        return self.file.parent

    @classmethod
    def load(cls, path=DEFAULT_CONFIG):
        file = Path(path).resolve()
        try:
            data = tomllib.loads(file.read_text())
        except FileNotFoundError:
            raise ConfigError(f"No {path}: give the config file with -c") from None
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from None

        def need(cond, message):
            if not cond:
                raise ConfigError(f"{path}: {message}")

        unknown = set(data) - {"server", "course", "publish_files", "publish"}
        need(not unknown, f"unknown setting {', '.join(sorted(unknown))}")
        need(isinstance(data.get("server"), str), 'needs server = "https://..."')
        need(isinstance(data.get("course"), int), "needs course = <the course id>")
        patterns = data.get("publish_files", [])
        need(isinstance(patterns, list) and all(isinstance(p, str) for p in patterns),
             "publish_files should be a list of patterns")
        for p in patterns:
            need(not Path(p).is_absolute(), f"{p}: patterns are relative to {path}")
        entries = data.get("publish", [])
        need(isinstance(entries, list), "write each publish entry as [[publish]]")
        pinned = []
        for e in entries:
            need(isinstance(e, dict) and isinstance(e.get("path"), str),
                 'each [[publish]] needs path = "..."')
            extra = set(e) - {"path", "cmid"}
            need(not extra, f"{e['path']}: unknown setting {', '.join(sorted(extra))}")
            cmid = e.get("cmid")
            need(cmid is None or isinstance(cmid, int) and cmid > 0,
                 f"{e['path']}: cmid should be a number")
            need(cmid is not None or not (file.parent / e["path"]).is_dir(),
                 f"{e['path']} is a directory, so it needs a cmid: nothing in it "
                 "says which resource it goes to")
            pinned.append((Path(e["path"]).as_posix().rstrip("/"), cmid))
        return cls(file, data["server"], data["course"], patterns, pinned)

    # -------------------------------------------------------- local files

    def items(self):
        """(items, missing): what there is to publish, and the [[publish]]
        paths that haven't been built."""
        found = {}
        for pattern in self.publish_files:
            for p in sorted(self.root.glob(pattern)):
                if p.name.startswith("."):
                    continue
                if p.is_dir():
                    raise ConfigError(
                        f"{pattern} matches the directory {p.relative_to(self.root)}: "
                        "publish a directory with its own [[publish]] entry and cmid")
                found[p.resolve()] = Item(p.resolve(), p.relative_to(self.root).as_posix())
        missing = []
        for rel, cmid in self.pinned:
            p = (self.root / rel).resolve()
            if not p.exists():
                missing.append(rel)
                continue
            if p.is_dir() and cmid is None:
                raise ConfigError(f"{rel} is a directory, so it needs a cmid")
            found[p] = Item(p, rel, cmid)
        return sorted(found.values(), key=lambda i: i.rel), missing

    def select(self, names, items):
        """The items named on a command line.

        A name can be the item's path, any path to the same file, its file
        name, or its file name without the extension -- which picks every
        file with that stem, e.g. both a deck's .html and its .pdf.
        """
        chosen = []
        for name in names:
            hits = self._named(name, items)
            if not hits:
                raise ConfigError(self._not_found(name))
            if len(hits) > 1 and len({i.name for i in hits}) == 1:
                raise ConfigError(f"{name} could be any of {', '.join(i.rel for i in hits)}: "
                                  "give its path")
            chosen += [i for i in hits if i not in chosen]
        return chosen

    def check_names(self, names):
        """Check, before a build, that each name is one `select` will find
        once it is built."""
        items, _ = self.items()
        for name in names:
            if self._named(name, items):
                continue
            given = Path(name)
            could_be = [rel for rel, _ in self.pinned
                        if given.as_posix().rstrip("/") == rel
                        or given.name in (Path(rel).name, Path(rel).stem)]
            for pattern in self.publish_files:
                base = Path(pattern).name
                if (fnmatch(given.as_posix(), pattern) or fnmatch(given.name, base)
                        or not given.suffix and fnmatch(given.name + Path(base).suffix, base)):
                    could_be.append(pattern)
            if not could_be:
                raise ConfigError(self._not_found(name))

    def _named(self, name, items):
        given = Path(name)
        path = given.resolve()
        for test in (lambda i: i.rel == given.as_posix().rstrip("/") or i.path == path,
                     lambda i: i.name == given.name,
                     lambda i: i.path.stem == given.name):
            hits = [i for i in items if test(i)]
            if hits:
                return hits
        return []

    def _not_found(self, name):
        return (f"{name} isn't anything {self.file.name} publishes, or hasn't been "
                "built: give a built file's path or name, e.g. lecture-01.html")


# ------------------------------------------------------------------ plan

@dataclass
class Target:
    """A built item and the resource it goes to.

    `status` is "same", "changed" or "unknown" (see `Comparison`) once
    compared; or, if it has no resource, "unmatched", with `note` saying
    why.  `held_ok` is False if the resource holds a file of another name
    now -- possible only for a pinned cmid, and a sign of a mistyped one.
    """

    item: Item
    resource: object = None
    status: str = "unmatched"
    note: str = ""
    held: str | None = None
    held_ok: bool = True


@dataclass
class Plan:
    targets: list
    missing: list           # [[publish]] paths not built

    @property
    def unmatched(self):
        return [t for t in self.targets if t.resource is None]

    def pending(self, force=False):
        """The targets to upload: those that differ from what Moodle holds,
        or with `force`, all of them."""
        return [t for t in self.targets if t.resource is not None
                and (force or t.status != "same")]

    @property
    def unchanged(self):
        return [t for t in self.targets if t.status == "same"]

    @property
    def unknown(self):
        return [t for t in self.targets if t.status == "unknown"]


def match(course, config, items, workers=8):
    """Find each item's resource: by cmid if pinned, otherwise the one File
    resource (not pinned by another item) holding a file of its name.
    Returns (targets, resources)."""
    all_resources = course.resources()
    by_cmid = {r.cmid: r for r in all_resources}
    all_items, _ = config.items()
    pinned = {i.cmid for i in all_items if i.cmid}
    pool = [r for r in all_resources if r.cmid not in pinned]

    holders = {}
    if any(i.cmid is None for i in items):
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for r, held in zip(pool, ex.map(lambda r: r.held_file(), pool)):
                if held:
                    holders.setdefault(held.name, []).append(r)
    names = {}
    for i in all_items:
        if i.cmid is None:
            names.setdefault(i.name, []).append(i.rel)

    targets = []
    for item in items:
        t = Target(item)
        if item.cmid is not None:
            t.resource = by_cmid.get(item.cmid)
            if t.resource is None:
                t.note = f"cmid {item.cmid} isn't a File resource in this course"
        elif len(names.get(item.name, [])) > 1:
            t.note = (f"more than one built file is named {item.name} "
                      f"({', '.join(names[item.name])}): pin each with a cmid")
        else:
            hits = holders.get(item.name, [])
            if len(hits) == 1:
                t.resource = hits[0]
            elif hits:
                t.note = (f"more than one resource holds {item.name}: "
                          + ", ".join(f"'{r.name}' (cmid {r.cmid})" for r in hits))
            else:
                t.note = "no resource on Moodle yet: drag it onto the course page"
        targets.append(t)
    return targets, all_resources


def plan(course, config, only=None, workers=8):
    """Match each item to be published to its resource, and compare it with
    what the resource holds.  `only`, if given, are the items to consider
    (see `Config.select`)."""
    items, missing = config.items()
    if only is not None:
        items, missing = only, []
    targets, _ = match(course, config, items, workers)

    def compare(t):
        try:
            c = t.resource.compare(t.item.path)
        except NotLoggedIn:
            raise
        except (MoodleError, OSError, httpx.HTTPError) as e:
            t.status, t.note = "unknown", f"couldn't check it against Moodle: {e}"
            return
        t.status, t.held = c.status, c.held
        t.held_ok = c.held in {name for _, name in local_files(t.item.path)}

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(compare, [t for t in targets if t.resource is not None]))
    return Plan(targets, missing)
