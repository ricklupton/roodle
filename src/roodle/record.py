"""
A record of what roodle has uploaded, and from which git commit, so that
what Moodle holds can be traced back to the source it was built from.

The record is `moodle-published.json`, beside the config file, meant to be
committed with the sources, so that it travels between machines and the
people who publish.  For each built file or directory it keeps only the
latest upload:

    {
      "build/slides/lecture-04.html": {
        "cmid": 1594533,
        "file": "lecture-04.html",          # the resource's main file
        "sha1": "3f2a9c1...",               # ...and its content hash
        "commit": "a1b2c3d...",             # HEAD when it was uploaded
        "dirty": false,                     # tracked files had changes
        "uploaded": "2026-11-12T14:03:00+00:00"
      }
    }

Moodle gives each file's content hash (see `files.serves_same`), so whether
the resource still holds what was recorded can be checked, downloading
nothing.
"""

import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

RECORD = "moodle-published.json"


@dataclass(frozen=True)
class Upload:
    cmid: int
    file: str
    sha1: str
    commit: str | None
    dirty: bool
    uploaded: str

    @property
    def when(self):
        """The date of the upload, in local time, e.g. '12 Nov 2026 14:03'."""
        try:
            t = datetime.fromisoformat(self.uploaded).astimezone()
        except ValueError:
            return self.uploaded
        return t.strftime("%-d %b %Y %H:%M")

    @property
    def source(self):
        """Where it was built from, e.g. 'a1b2c3d, with uncommitted changes'."""
        if not self.commit:
            return "outside git"
        return self.commit[:7] + (", with uncommitted changes" if self.dirty else "")


def git(root, *args):
    """The output of a git command in `root`'s repository, or None."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                           text=True, check=False)
    except OSError:
        return None
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def git_state(root):
    """(HEAD's commit hash, whether tracked files have changes), or
    (None, False) outside git.  Changes to the record itself don't count."""
    commit = git(root, "rev-parse", "HEAD")
    if commit is None:
        return None, False
    status = git(root, "status", "--porcelain", "--untracked-files=no") or ""
    dirty = any(not line.endswith(RECORD) for line in status.splitlines())
    return commit, dirty


def commits_since(root, commit):
    """The commits since `commit`, newest first, as one-line summaries;
    None if git can't say (not a repository, or not a commit it has)."""
    out = git(root, "log", "--oneline", "--no-decorate", f"{commit}..HEAD")
    return None if out is None else [line for line in out.splitlines() if line]


class Record:
    def __init__(self, root):
        self.path = Path(root) / RECORD
        self.root = Path(root)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (ValueError, OSError) as e:
            raise ValueError(f"{self.path}: {e}") from None
        self.uploads = {}
        for rel, entry in data.items():
            try:
                self.uploads[rel] = Upload(**entry)
            except TypeError:
                continue  # an entry this version doesn't understand

    def get(self, rel):
        return self.uploads.get(rel)

    def add(self, rel, cmid, file, sha1, commit, dirty):
        self.uploads[rel] = Upload(
            cmid, file, sha1, commit, dirty,
            datetime.now(timezone.utc).replace(microsecond=0).isoformat())
        self.save()

    def save(self):
        data = {rel: asdict(u) for rel, u in sorted(self.uploads.items())}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
