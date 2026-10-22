# roodle

Script a Moodle course from Python, or publish built files to it from the
command line.

Logging in to a university Moodle usually goes through single sign-on, which
is hard to script. roodle borrows the cookies of a Firefox window that is
already logged in instead, and makes the same requests as Moodle's own pages.

## Publishing built files: `roodle publish`

A `moodle.toml` says which course, and which built files go to it:

``` toml
server = "https://moodle.bath.ac.uk"
course = 12345

# Built files, each published to the File resource that holds a file of the
# same name.  To make a resource, drag the built file onto the course page.
publish_files = [
  "_build/slides/*.html",
  "_build/slides/*.pdf",
  "_build/sheets/*.pdf",
]

# One path each: a directory (zipped, then unzipped into the resource), which
# needs a cmid, or a single file, with an optional cmid to pin it.
[[publish]]
path = "_build/book"
cmid = 1594533       # the number in the resource's edit-settings URL
```

Paths are relative to the config file. Then:

```
roodle publish                    upload whatever differs from what Moodle holds
roodle publish lecture-04.html    upload just these, changed or not
roodle publish lecture-04         ...every file with that stem (.html and .pdf)
roodle publish --dry-run          only list what would be uploaded
roodle publish --check [FILE...]  only check the names and the login (before a build, say)
roodle resources                  list the File resources, what each holds,
                                  and which built file goes to each
```

All of them take `-c CONFIG`, which defaults to `moodle.toml`.

How it works:

- **Matching files to resources by name.** No ids need writing down, apart
  from a directory's. A built file with no resource yet is reported, and so
  is a file name that more than one resource holds. A resource pinned by a
  `cmid` is left out of the matching by name.
- **Comparing with what's on Moodle.** Moodle serves every stored file with
  its SHA-1 as the ETag, so each built file is checked against what Moodle
  really holds, with nothing kept locally and nothing downloaded. Fixing one
  deck uploads one deck, and a run that stops partway can simply be
  repeated.
- **Before uploading.** It lists where each file is going: the resource's
  name on Moodle, and whether students can see it.
- **Uploading.** Each upload does what you would do in "Edit settings":
  delete the old files, upload the new one (unzipping a directory's zip),
  and save. The resource's settings, visibility included, are left as they
  are.
- **When things go wrong.** If the login has expired, it waits for you to
  log in again. If an upload fails, it offers to retry, skip it or quit.

## The library

``` python
from roodle import Moodle

moodle = Moodle.from_firefox("https://moodle.bath.ac.uk")
moodle.wait_for_login()          # asks you to log in, in Firefox, if need be
course = moodle.course(12345)

for module in course.modules():  # everything on the course page
    print(module.cmid, module.module, module.name, module.visible, module.stealth)

notes = course.resource(1594533)
print(notes.held_file())         # HeldFile(name='index.html', context_id=...)
if notes.compare("_build/book").status != "same":
    notes.replace("_build/book")
```

Requests that need it carry the session key (`moodle.sesskey`), read from
the pages Moodle sends. `moodle.ajax(methodname, args)` calls any of
Moodle's AJAX web service functions, and `moodle.page(url)` gets a page.
Either raises `NotLoggedIn` when the session has expired.

### Groups

To put students into the groups that a CSV file gives:

``` python
import csv

participants = course.participants()
assigned = {row["Username"]: row["Group"] for row in csv.DictReader(open("groups.csv"))}
sd_groups = [g.name for g in participants.groups if g.name.startswith("SD-")]

for username, group in assigned.items():
    change = participants.modify_groups(username, remove=sd_groups, add=[group])
    if change:
        print(change)
```

`modify_groups` only touches the groups it is given, so groups used for
other purposes are left alone. `set_groups` sets a user's groups outright.
Both take `dry_run=True`, which shows the change without making it.

### Assignments, grades and choices

``` python
assignment = course.assignment(1234567)
csv_text = assignment.grading_worksheet()      # plugin="offline_bath_2023" at Bath
zip_file = assignment.download_submissions(["abc123", "def456"])

grades_csv = course.grades()
responses = course.choice_results(7654321)
```

## Development

```
uv run pytest
```

The tests run against a small fake Moodle (`tests/fake_moodle.py`), served
through `httpx.MockTransport`. Real pages, saved from Moodle, go in
`tests/pages/` for the parsers to be tested against.
