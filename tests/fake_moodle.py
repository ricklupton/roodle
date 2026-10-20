"""
A small fake Moodle, served through httpx.MockTransport, for testing roodle
without a network.

It answers the requests roodle makes, the way Moodle does as far as roodle
relies on it: pages carry M.cfg and a body class saying whether they were
seen logged in; File resources hold files with SHA-1 content hashes, served
with ETags; and an edit form's draft area can be listed, cleared, uploaded
to, unzipped and saved back.  The HTML is the minimum roodle reads -- the
fixtures in tests/pages/ are real Moodle pages, for the parsers.
"""

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote

import httpx

from roodle import Moodle

SERVER = "https://moodle.example.ac.uk"
COURSE = 101
SESSKEY = "s3ssk3y"
UPLOAD_REPO = 3
MAXBYTES = 1_000_000


def page(body, logged_in=True, cfg=None):
    cfg = {"wwwroot": SERVER, "sesskey": SESSKEY, **(cfg or {})}
    cls = "path-site" if logged_in else "path-login notloggedin"
    return (f"<html><head><script>\nM.cfg = {json.dumps(cfg)};\n</script></head>"
            f'<body id="page" class="{cls}">{body}</body></html>')


@dataclass
class Resource:
    cmid: int
    name: str
    context_id: int
    files: dict = field(default_factory=dict)     # path -> bytes
    visible: bool = True
    section: int = 1
    module: str = "resource"
    filtered: bool = False                        # "Use filters on file content"

    @property
    def main_file(self):
        if not self.files:
            return None
        return "index.html" if "index.html" in self.files else sorted(self.files)[0]


class FakeMoodle:
    def __init__(self):
        self.logged_in = True
        self.resources = {}
        self.drafts = {}          # itemid -> {path: bytes}
        self.requests = []        # (method, path, action) of each request
        self.next_item = 900
        self.use_course_state = True

    def add(self, cmid, name, files=None, **kw):
        self.resources[cmid] = Resource(cmid, name, 5000 + cmid, dict(files or {}), **kw)
        return self.resources[cmid]

    def moodle(self):
        return Moodle(SERVER, transport=httpx.MockTransport(self.handle))

    # ------------------------------------------------------------ routing

    def handle(self, request):
        path = request.url.path
        query = {k: v[0] for k, v in parse_qs(request.url.query.decode()).items()}
        form = {}
        if request.method == "POST" and request.headers.get("content-type", "").startswith(
                "application/x-www-form-urlencoded"):
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.requests.append((request.method, path, query.get("action")))

        if path.startswith("/login/"):
            return httpx.Response(200, text=page("Log in", logged_in=False))
        if not self.logged_in:
            if path == "/lib/ajax/service.php":
                return httpx.Response(200, json=[{"error": True, "exception": {
                    "message": "Login required", "errorcode": "servicerequireslogin"}}])
            return httpx.Response(303, headers={"Location": f"{SERVER}/login/index.php"})

        if path in ("/", "/course/view.php"):
            return httpx.Response(200, text=page("Home"))
        if path == "/lib/ajax/service.php":
            return self.ajax(json.loads(request.content)[0])
        if path == "/mod/resource/index.php":
            return httpx.Response(200, text=self.index_page())
        if path == "/mod/resource/view.php":
            return self.view(int(query["id"]))
        if path.startswith("/pluginfile.php/"):
            return self.pluginfile(request, path)
        if path == "/course/modedit.php" and request.method == "GET":
            return self.edit_page(int(query["update"]))
        if path == "/course/modedit.php":
            return self.save(form)
        if path == "/repository/draftfiles_ajax.php":
            return self.draftfiles(query["action"], form)
        if path == "/repository/repository_ajax.php":
            return self.upload(request)
        return httpx.Response(404, text="Not found")

    def ajax(self, call):
        if call["methodname"] == "core_courseformat_get_state" and self.use_course_state:
            state = {
                "course": {"id": COURSE},
                "section": [{"id": 70 + s, "number": s,
                             "cmlist": [str(r.cmid) for r in self.resources.values()
                                        if r.section == s]}
                            for s in sorted({r.section for r in self.resources.values()})],
                "cm": [{"id": str(r.cmid), "name": r.name, "visible": r.visible,
                        "sectionid": 70 + r.section, "module": r.module,
                        "url": f"{SERVER}/mod/{r.module}/view.php?id={r.cmid}"}
                       for r in self.resources.values()],
            }
            return httpx.Response(200, json=[{"error": False, "data": json.dumps(state)}])
        return httpx.Response(200, json=[{"error": True, "exception": {
            "message": "Can't find data record in database table external_functions.",
            "errorcode": "invalidrecord"}}])

    def index_page(self):
        dimmed = 'class="dimmed" '
        rows = "".join(
            f'<tr><td><a {"" if r.visible else dimmed}'
            f'href="view.php?id={r.cmid}">{r.name}</a></td></tr>'
            for r in self.resources.values() if r.module == "resource")
        return page(f"<table>{rows}</table>")

    def view(self, cmid):
        r = self.resources[cmid]
        if r.main_file is None:
            return httpx.Response(200, text=page("No file"))
        return httpx.Response(303, headers={"Location": (
            f"{SERVER}/pluginfile.php/{r.context_id}/mod_resource/content/7/"
            f"{quote(r.main_file)}")})

    def pluginfile(self, request, path):
        m = re.match(r"/pluginfile\.php/(\d+)/mod_resource/content/\d+/(.+)", path)
        r = next((r for r in self.resources.values() if r.context_id == int(m.group(1))), None)
        data = r and r.files.get(unquote(m.group(2)))
        if data is None:
            return httpx.Response(404)
        sha1 = hashlib.sha1(data).hexdigest()
        if r.filtered:
            return httpx.Response(200, content=data)
        if request.headers.get("If-None-Match", "").strip('W/"') == sha1:
            return httpx.Response(304, headers={"ETag": f'W/"{sha1}"'})
        return httpx.Response(200, content=data, headers={"ETag": f'W/"{sha1}"'})

    # ---------------------------------------------------------- edit form

    def edit_page(self, cmid):
        r = self.resources[cmid]
        itemid = self.next_item
        self.next_item += 1
        self.drafts[itemid] = dict(r.files)
        options = {"maxbytes": MAXBYTES, "areamaxbytes": -1, "itemid": itemid,
                   "client_id": "abc123", "filepicker": {"repositories": {
                       "1": {"id": "1", "type": "recent"},
                       str(UPLOAD_REPO): {"id": str(UPLOAD_REPO), "type": "upload"}}}}
        body = f"""
        <form action="{SERVER}/course/modedit.php" method="post">
          <input type="hidden" name="sesskey" value="{SESSKEY}">
          <input type="hidden" name="_qf__mod_resource_mod_form" value="1">
          <input type="hidden" name="update" value="{cmid}">
          <input type="hidden" name="modulename" value="{r.module}">
          <input type="text" name="name" value="{r.name}">
          <input type="hidden" name="files" value="{itemid}">
          <input type="submit" name="submitbutton" value="Save and display">
        </form>
        <script>Y.use("core_filepicker", function(Y) {{
          M.form_filemanager.init(Y, {json.dumps(options)});
        }});</script>"""
        return httpx.Response(200, text=page(body, cfg={"contextid": r.context_id}))

    def save(self, form):
        r = self.resources[int(form["update"])]
        if not form.get("name"):
            return httpx.Response(200, text=page(
                '<form><span class="error">Required</span></form>'))
        r.files = self.drafts.pop(int(form["files"]))
        return httpx.Response(303, headers={"Location": f"{SERVER}/mod/resource/view.php?id={r.cmid}"})

    def draftfiles(self, action, form):
        assert form["sesskey"] == SESSKEY
        draft = self.drafts[int(form["itemid"])]
        if action == "list":
            top = sorted({p.split("/")[0] for p in draft})
            return httpx.Response(200, json={"list": [
                {"filename": "." if any(p.startswith(t + "/") for p in draft) else t,
                 "filepath": f"/{t}/" if any(p.startswith(t + "/") for p in draft) else "/"}
                for t in top]})
        if action == "delete":
            if form["filename"] == ".":
                prefix = form["filepath"].strip("/") + "/"
                for p in [p for p in draft if p.startswith(prefix)]:
                    del draft[p]
            else:
                draft.pop((form["filepath"].strip("/") + "/" + form["filename"]).lstrip("/"))
            return httpx.Response(200, json={"filepath": form["filepath"]})
        if action == "unzip":
            with zipfile.ZipFile(io.BytesIO(draft[form["filename"]])) as zf:
                for name in zf.namelist():
                    draft[name] = zf.read(name)
            return httpx.Response(200, json={"filepath": "/"})
        return httpx.Response(400)

    def upload(self, request):
        fields, files = parse_multipart(request)
        assert fields["sesskey"] == SESSKEY and int(fields["repo_id"]) == UPLOAD_REPO
        name, data = files["repo_upload_file"]
        if len(data) > int(fields["maxbytes"]) > 0:
            return httpx.Response(200, json={"error": "File too big"})
        self.drafts[int(fields["itemid"])][fields["title"]] = data
        return httpx.Response(200, json={"file": fields["title"]})


def parse_multipart(request):
    boundary = re.search(r"boundary=([^;]+)", request.headers["content-type"]).group(1)
    fields, files = {}, {}
    for part in request.content.split(b"--" + boundary.encode())[1:-1]:
        head, _, body = part[2:-2].partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head).group(1).decode()
        filename = re.search(rb'filename="([^"]*)"', head)
        if filename:
            files[name] = (filename.group(1).decode(), body)
        else:
            fields[name] = body.decode()
    return fields, files
