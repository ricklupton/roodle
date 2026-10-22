"""
Save the Moodle pages and answers that roodle reads, as fixtures for its
tests.  With a Firefox logged in to Moodle:

    uv run python tests/snapshot.py -c path/to/moodle.toml CMID

CMID is a File resource that holds a file.  Its edit form is read but not
saved, so nothing on Moodle changes.  Everything goes in tests/pages/
(or --out DIR).  Session keys are replaced by "SESSKEY", but look through
the files before committing them: your name appears in the edit form, for
example, as the default author of an upload.
"""

import argparse
import json
from pathlib import Path

from roodle import Moodle
from roodle.publish import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("-c", "--config", default="moodle.toml")
    parser.add_argument("cmid", type=int, help="a File resource holding a file")
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "pages")
    args = parser.parse_args()

    config = Config.load(args.config)
    moodle = Moodle.from_firefox(config.server)
    moodle.wait_for_login()
    sesskey = moodle.sesskey
    args.out.mkdir(parents=True, exist_ok=True)

    def save(name, text):
        (args.out / name).write_text(text.replace(sesskey, "SESSKEY"))
        print(f"  {args.out / name}")

    def meta(r):
        return {"status": r.status_code, "url": str(r.url),
                "headers": {k: v for k, v in r.headers.items()
                            if k.lower() in ("location", "etag", "content-type")}}

    save("front.html", moodle.http.get("/", follow_redirects=True).text)
    save("modedit.html", moodle.http.get(
        "/course/modedit.php", params={"update": args.cmid, "return": 0}).text)
    save("resource-index.html", moodle.http.get(
        "/mod/resource/index.php", params={"id": config.course}).text)
    save("course-state.json", moodle.http.post(
        "/lib/ajax/service.php",
        params={"sesskey": sesskey, "info": "core_courseformat_get_state"},
        json=[{"index": 0, "methodname": "core_courseformat_get_state",
               "args": {"courseid": config.course}}]).text)

    view = moodle.http.get("/mod/resource/view.php", params={"id": args.cmid, "redirect": 1})
    answers = {"view": meta(view)}
    held = moodle.course(config.course).resource(args.cmid).held_file()
    if held:
        url = moodle.url(f"/pluginfile.php/{held.context_id}/mod_resource/content/0/{held.name}")
        with moodle.http.stream("GET", url, headers={"Accept-Encoding": "identity"}) as r:
            answers["pluginfile"] = meta(r)
        etag = answers["pluginfile"]["headers"].get("etag", "")
        with moodle.http.stream("GET", url, headers={"Accept-Encoding": "identity",
                                                     "If-None-Match": etag}) as r:
            answers["pluginfile_if_none_match"] = meta(r)

    # The same front page, and a course page, without any cookies.
    with Moodle(config.server) as anon:
        for key, path in (("logged_out_front", "/"),
                          ("logged_out_course", f"/course/view.php?id={config.course}")):
            r = anon.http.get(path, follow_redirects=True)
            answers[key] = {"history": [meta(h) for h in r.history], **meta(r)}
            if key == "logged_out_front":
                save("logged-out-front.html", r.text)
    save("answers.json", json.dumps(answers, indent=2))


if __name__ == "__main__":
    main()
