"""
Moodle's file areas: the draft areas that a form's file manager edits, and
the files Moodle serves through pluginfile.php.

A file manager on a form doesn't change a module's files directly.  Moodle
copies them into a draft area when it shows the form; the file manager's
JavaScript lists, uploads, unzips and deletes files there; and saving the
form copies the draft area back.  `DraftArea` makes the same requests.
"""

import hashlib
import mimetypes
import re
from pathlib import Path
from urllib.parse import quote

from .errors import MoodleError, TooLarge


class DraftArea:
    """A draft file area, as a file manager on a form sees it.

    `options` are the file manager's own (see `forms.filemanager_options`):
    they give the upload limits and the id of the "Upload a file"
    repository.  Get one from `EditForm.draft_area()`.
    """

    def __init__(self, moodle, itemid, sesskey, context_id, options):
        self.moodle = moodle
        self.itemid = itemid
        self.sesskey = sesskey
        self.context_id = context_id or (options.get("context") or {}).get("id")
        self.client_id = options.get("client_id") or "roodle"
        maxbytes = int(options.get("maxbytes") or -1)
        self.maxbytes = maxbytes if maxbytes > 0 else None
        self.areamaxbytes = int(options.get("areamaxbytes") or -1)
        self.repositories = (options.get("filepicker") or {}).get("repositories") or {}

    @property
    def upload_repo_id(self):
        """The id of the file picker's "Upload a file" repository."""
        for repo in self.repositories.values():
            if repo.get("type") == "upload":
                return int(repo["id"])
        raise MoodleError("The file picker offers no 'Upload a file' repository "
                          f"(it offers: {', '.join(r.get('type', '?') for r in self.repositories.values()) or 'none'})")

    def _answer(self, response, action):
        data = self.moodle.check(response).json()
        if isinstance(data, dict) and data.get("error"):
            raise MoodleError(f"Moodle draft area {action}: {data['error']}")
        return data

    def _draftfiles(self, action, **data):
        r = self.moodle.post("/repository/draftfiles_ajax.php", params={"action": action},
                             data={"sesskey": self.sesskey, "client_id": self.client_id,
                                   "itemid": self.itemid, **data})
        return self._answer(r, action)

    def list(self, filepath="/"):
        """The files and folders at `filepath` in the area."""
        return self._draftfiles("list", filepath=filepath).get("list", [])

    def delete(self, filepath, filename):
        """Delete a file, or a folder and all it holds."""
        return self._draftfiles("delete", filepath=filepath, filename=filename)

    def clear(self):
        """Delete everything in the area."""
        for entry in self.list():
            self.delete(entry["filepath"], entry["filename"])

    def check_size(self, path):
        """Raise TooLarge if a file is over the area's upload limit."""
        size = Path(path).stat().st_size
        if self.maxbytes is not None and size > self.maxbytes:
            raise TooLarge(f"{size / 2**20:.1f} MB is over Moodle's "
                           f"{self.maxbytes / 2**20:.1f} MB upload limit")

    def upload(self, path, filename=None, filepath="/"):
        """Upload a local file into the area through the "Upload a file"
        repository, as the file picker does, replacing any file of the same
        name."""
        path = Path(path)
        filename = filename or path.name
        self.check_size(path)
        mimetype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        with open(path, "rb") as f:
            r = self.moodle.post(
                "/repository/repository_ajax.php",
                params={"action": "upload"},
                data={
                    "repo_id": self.upload_repo_id,
                    "p": "/", "page": "",
                    "env": "filemanager",
                    "sesskey": self.sesskey,
                    "client_id": self.client_id,
                    "itemid": self.itemid,
                    "maxbytes": self.maxbytes or -1,
                    "areamaxbytes": self.areamaxbytes,
                    "ctx_id": self.context_id,
                    "savepath": filepath,
                    "title": filename,
                    "author": "",
                    "overwrite": 1,
                },
                files={"repo_upload_file": (filename, f, mimetype)},
                timeout=None,
            )
        return self._answer(r, "upload")

    def unzip(self, filename, filepath="/"):
        """Unzip a zip in the area where it is."""
        return self._draftfiles("unzip", filepath=filepath, filename=filename,
                                areamaxbytes=self.areamaxbytes)


def sha1_file(path):
    """The SHA-1 of a file's bytes: what Moodle calls its content hash."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def pluginfile_url(moodle, context_id, component, filearea, itemid, filepath):
    """The URL at which Moodle serves a stored file."""
    return moodle.url(f"/pluginfile.php/{context_id}/{component}/{filearea}/"
                      f"{itemid}/{quote(filepath.lstrip('/'))}")


def serves_same(moodle, url, sha1):
    """Whether Moodle serves exactly the bytes with this SHA-1 at `url`:
    True, False, or None if Moodle doesn't say.

    Moodle sends a stored file's content hash as its ETag (some servers mark
    it weak, W/"..."), and answers If-None-Match carrying it with 304 Not
    Modified, so nothing is downloaded: the response is closed unread.  A
    missing file is False.  No ETag means Moodle ran the file through its
    filters ("Use filters on file content"), so the bytes it sends aren't the
    ones it holds.
    """
    with moodle.http.stream("GET", url, headers={"Accept-Encoding": "identity",
                                                 "If-None-Match": f'"{sha1}"'}) as r:
        if r.status_code == 304:
            return True
        if r.status_code == 404:
            return False
        moodle.check(r)
        if r.status_code != 200:
            raise MoodleError(f"Moodle answered {r.status_code} for {url}")
        etag = re.search(r"[0-9a-f]{40}", r.headers.get("ETag", ""))
        return etag.group(0) == sha1 if etag else None


def download(moodle, url):
    """The bytes Moodle serves at `url`, or None if it has no file there."""
    r = moodle.http.get(url, headers={"Accept-Encoding": "identity"})
    if r.status_code == 404:
        return None
    moodle.check(r)
    if r.status_code != 200:
        raise MoodleError(f"Moodle answered {r.status_code} for {url}")
    return r.content
