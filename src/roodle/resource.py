"""
File resources: a course module holding one file, or a set of files with one
main file (a static site, say).
"""

import re
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from .errors import NotAFileResource
from .files import pluginfile_url, serves_same, sha1_file
from .forms import EditForm


@dataclass(frozen=True)
class HeldFile:
    """The main file a File resource serves."""

    name: str
    context_id: int


@dataclass(frozen=True)
class Comparison:
    """How a local file or directory compares with what a resource holds.

    `status` is "same", "changed" or "unknown" (Moodle doesn't give content
    hashes for the resource's files, because it filters them); `held` is the
    name of the resource's main file now, or None if it has none.
    """

    status: str
    held: str | None


def local_files(path):
    """The files that make up a resource's content, with their names in it:
    a file on its own, or everything in a directory."""
    path = Path(path)
    if path.is_dir():
        return [(p, p.relative_to(path).as_posix())
                for p in sorted(path.rglob("*"))
                if p.is_file() and p.name != ".DS_Store"]
    return [(path, path.name)]


def zip_directory(path, zip_path):
    """Zip a directory's content, at the root of the archive (so that its
    index.html is at the resource's top level)."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p, name in local_files(path):
            zf.write(p, name)


class FileResource:
    """A File resource.  `name`, `visible` and `section` are as the course
    page lists them, when it came from `Course.resources()`."""

    def __init__(self, course, cmid, name=None, visible=None, section=None):
        self.course = course
        self.cmid = int(cmid)
        self.name = name
        self.visible = visible
        self.section = section
        self._held = None

    def __repr__(self):
        return f"FileResource(cmid={self.cmid}, name={self.name!r})"

    @property
    def moodle(self):
        return self.course.moodle

    @property
    def url(self):
        return self.moodle.url(f"/mod/resource/view.php?id={self.cmid}")

    def held_file(self, refresh=False):
        """The main file the resource serves, or None if it has none.

        Asked for with redirect=1, Moodle sends you straight to the file, at
        .../pluginfile.php/CONTEXTID/mod_resource/content/REVISION/FILENAME,
        so the redirect names both.  Failing that, the same URL appears in
        the page when the resource is set to embed or open its file.
        """
        if self._held is None or refresh:
            r = self.moodle.get("/mod/resource/view.php",
                                params={"id": self.cmid, "redirect": 1})
            self.moodle.check(r)
            where = r.headers.get("Location", "") if r.is_redirect else r.text
            m = re.search(r"/pluginfile\.php/(\d+)/mod_resource/content/\d+/"
                          r"([^\"'?#\s<>]+)", where)
            self._held = HeldFile(unquote(m.group(2)), int(m.group(1))) if m else False
        return self._held or None

    def compare(self, path, workers=8):
        """Compare a local file, or a directory of them, with what the
        resource holds, by content hash, downloading nothing.

        A directory is the same only if every file in it is.  The newest file
        is checked first, then the rest in parallel, stopping at the first
        that differs.
        """
        held = self.held_file()
        files = local_files(path)
        if held is None or held.name not in {name for _, name in files}:
            return Comparison("changed", held and held.name)

        def check(f):
            url = pluginfile_url(self.moodle, held.context_id, "mod_resource",
                                 "content", 0, f[1])
            return serves_same(self.moodle, url, sha1_file(f[0]))

        files.sort(key=lambda f: f[0].stat().st_mtime, reverse=True)
        found = [check(files[0])]
        if found[0] is True and len(files) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(check, f) for f in files[1:]]
                for future in as_completed(futures):
                    found.append(future.result())
                    if found[-1] is False:
                        for other in futures:
                            other.cancel()
                        break
        if False in found:
            status = "changed"
        elif None in found:
            status = "unknown"
        else:
            status = "same"
        return Comparison(status, held.name)

    def edit_form(self):
        return EditForm.load(self.moodle, self.cmid)

    def replace(self, path):
        """Replace the resource's files with a local file, or with the
        contents of a local directory, and save it.

        This is what you would do by hand in "Edit settings": delete the old
        files, upload the new one, and save.  A directory is uploaded as a
        zip, unzipped and the zip deleted.  Nothing is changed if the upload
        would be over Moodle's limit (TooLarge).
        """
        path = Path(path)
        form = self.edit_form()
        if form.module != "resource":
            raise NotAFileResource(f"cmid {self.cmid} is a '{form.module}', "
                                   "not a File resource")
        area = form.draft_area("files")
        with tempfile.TemporaryDirectory() as tmp:
            if path.is_dir():
                upload = Path(tmp) / "roodle-upload.zip"
                zip_directory(path, upload)
            else:
                upload = path
            area.check_size(upload)
            area.clear()
            area.upload(upload)
            if path.is_dir():
                area.unzip(upload.name)
                area.delete("/", upload.name)
            form.submit()
        self._held = None
