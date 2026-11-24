"""
roodle's command line.

    roodle publish [-c CONFIG] [FILE...]   upload what has changed
    roodle resources [-c CONFIG]           what's on Moodle, and what goes where

The config file (moodle.toml by default) says which course, and which built
files go to it: see roodle.publish.
"""

import argparse
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from .client import Moodle
from .diffview import prepare
from .errors import MoodleError, TooLarge
from .files import sha1_file
from .publish import DEFAULT_CONFIG, Config, ConfigError, match, plan
from .record import Record, commits_since, git_state
from .resource import local_files

try:
    import termios
except ImportError:  # Windows
    termios = None

STOPPED = "Stopped. Whatever wasn't uploaded will be on the next run."


def ask(prompt):
    """input(), but anything typed before the prompt appeared -- an impatient
    Enter during a build, say -- is discarded first, so it can't answer the
    question; and Ctrl-C or a closed stdin gives up cleanly."""
    if termios is not None and sys.stdin.isatty():
        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(STOPPED)


def yes(prompt, default):
    answer = ask(prompt).strip().lower()
    return default if not answer else answer in ("y", "yes")


def plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def size(path):
    n = path.stat().st_size if path.is_file() else sum(
        p.stat().st_size for p in path.rglob("*") if p.is_file())
    return f"{n / 2**20:.1f} MB"


def visibility(resource):
    """Whether students can see the resource, as Moodle's course page puts it."""
    if not resource.visible:
        return "hidden from students"
    if resource.stealth:
        return "available but not shown on course page"
    return "shown to students"


def connect(config):
    moodle = Moodle.from_firefox(config.server)
    return moodle, moodle.course(config.course)


# ---------------------------------------------------------------- publish

def publish(args):
    config = Config.load(args.config)
    if args.check:
        return check(config, args.files)

    items, _ = config.items()
    only = config.select(args.files, items) if args.files else None
    try:
        record = Record(config.root)
    except ValueError as e:
        raise ConfigError(str(e)) from None
    mode = "text" if args.text else "raw" if args.raw else "code"
    show_diff = args.diff or args.text or args.raw
    moodle, course = connect(config)
    moodle.wait_for_login(course.url, ask=ask)

    n = len(only) if only is not None else len(items)
    print(f"Checking {plural(n, 'file')} against Moodle...")
    p = plan(course, config, only)
    if p.missing:
        print(f"Not built yet: {', '.join(p.missing)}")
    if p.unmatched:
        print("Not published:")
        for t in p.unmatched:
            print(f"  {t.item.rel}: {t.note}")
    for t in p.unknown:
        if t.note:
            print(f"  {t.item.rel}: {t.note}")
    unhashed = [t.item.name for t in p.unknown if not t.note]
    if unhashed:
        print(f"Moodle didn't give a content hash for {', '.join(unhashed)}, so "
              "there's no telling whether they changed.  Is 'Use filters on "
              "file content' on for them?")
    failed_matches = 1 if only is not None and p.unmatched else 0

    pending = p.pending(force=args.force)
    if only is not None and not args.force and p.unchanged:
        if pending:
            print("Moodle holds the same as the build already, so not uploading: "
                  + ", ".join(t.item.rel for t in p.unchanged))
        else:
            history(p.unchanged, record)
    if not pending:
        if p.unchanged:
            n = len(p.unchanged)
            print(f"\nMoodle is up to date: {plural(n, 'resource')} "
                  f"{'holds' if n == 1 else 'hold'} the same as the build.")
        return failed_matches
    if args.dry_run:
        print("\nWould upload" + ("" if only else f" ({len(p.unchanged)} unchanged)") + ":")
        list_targets(pending)
        if show_diff:
            diff(pending, mode, record)
        return failed_matches

    confirm(pending, mode if show_diff else None, record)
    failed = upload(moodle, course, pending, record)
    if failed:
        print(f"\nNot uploaded: {', '.join(failed)}")
        return 1
    print(f"\nUploaded {len(pending)}.")
    return failed_matches


def check(config, names):
    """What to check before a build: that the names given are ones there
    will be something to publish for, and the login, with a warning in time
    to log in while the build runs."""
    if names:
        config.check_names(names)
    moodle, course = connect(config)
    try:
        ok = moodle.logged_in(course.url)
    except httpx.HTTPError as e:
        print(f"  Could not reach Moodle: {e}")
        return 0
    if not ok:
        print(f"\n  ** Not logged in to Moodle. Log in, in the browser, at\n"
              f"       {course.url}\n"
              f"     The upload will wait for it.\n")
    return 0


def list_targets(targets):
    """Each file, the resource it goes to, and whether students can see it."""
    for t in targets:
        r = t.resource
        print(f"  {t.item.rel}\n      -> '{r.name}', cmid {r.cmid}, {visibility(r)}"
              + ("" if t.held_ok else f"   <- holds {t.held or 'no file'} now"))


def confirm(pending, mode=None, record=None):
    """List where each file is going, by the resource's name on Moodle and
    whether students can see it, and ask first if any resource holds a file
    of a different name now -- the sign of a mistyped cmid -- or after
    showing the diff."""
    print("\nTo upload:")
    list_targets(pending)
    if mode:
        diff(pending, mode, record)
    if not all(t.held_ok for t in pending):
        question = ("Some of these resources hold a file of another name now: check "
                    "they are the right ones. Upload? [Y/n] ")
    elif mode:
        question = "Upload? [Y/n] "
    else:
        question = None
    if question and not yes(question, True):
        raise SystemExit("Not uploading.")
    print()


def diff(pending, mode="code", record=None):
    """Show how each upload would change what Moodle holds.

    First, from `record`, when roodle uploaded what Moodle holds, from which
    commit, and the commits since.  Then the files.  Moodle's copies of the files that differ, and the local ones, are put in
    a temporary directory as moodle/ and local/, each under the item's path
    from the config file, and compared with `git diff --no-index` -- so
    git's pager, colours and diff settings apply -- or `diff -ruN` without
    git.  $ROODLE_DIFF, if set, is a command to run instead: it is given the
    two directories, or they replace {old} and {new} in it.  Its directory
    is kept, in case the command returns before it has read the files
    (`emacsclient`, say).

    Before comparing them, the files are rewritten as `mode` shows them
    (see `diffview.view`): "code" with base64 blobs replaced by their size
    and hash, "text" as only their visible text, or "raw" as they are.
    """
    if record is not None:
        history(pending, record)
    tmp = Path(tempfile.mkdtemp(prefix="roodle-diff-"))
    try:
        old, new = tmp / "moodle", tmp / "local"
        changed = gather_changes(pending, old, new)
        if changed:
            same = prepare(old, new, mode)
            for rel in same:
                print(f"  {rel}: differs only in "
                      + ("what isn't visible" if mode == "text" else "embedded files"))
            changed -= len(same)
        if not changed:
            print("  No differences" + ("" if mode == "raw" else " to show") + ".")
        elif not run_diff(tmp, os.environ.get("ROODLE_DIFF")):
            return  # keep the files
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    shutil.rmtree(tmp, ignore_errors=True)


def gather_changes(pending, old, new):
    """Fetch each target's changes into `old` and `new`; the number of files."""
    print("\nFetching Moodle's copies of what differs...")
    changed = 0
    for t in pending:
        base = Path(t.item.rel) if t.item.path.is_dir() else Path(t.item.rel).parent
        changed += len(t.resource.fetch_changes(t.item.path, old / base, new / base))
        if t.item.path.is_dir():
            print(f"  {t.item.rel}: any files Moodle holds that the build doesn't "
                  "aren't shown (the upload deletes them)")
        if t.status == "unknown":
            print(f"  {t.item.rel}: Moodle filters this resource's files, so what "
                  "it serves, shown here, may not be what it holds")
    old.mkdir(exist_ok=True)
    new.mkdir(exist_ok=True)
    return changed


def history(pending, record, shown=10):
    """For each target, when roodle uploaded what Moodle holds and from
    which commit, by the record -- grouped by commit, with the commits
    since."""
    print("\nWhat Moodle holds now:")
    groups = {}
    for t in pending:
        u = record.get(t.item.rel)
        if u is None:
            print(f"  {t.item.rel}: no record of roodle uploading it")
            continue
        holds = u.cmid == t.resource.cmid and t.resource.holds(u.file, u.sha1)
        if holds is False:
            print(f"  {t.item.rel}: not what roodle last uploaded "
                  f"({u.when}, from {u.source})")
            continue
        groups.setdefault((u.commit, u.dirty), []).append((t, u, holds))
    for (commit, dirty), members in groups.items():
        for t, u, holds in members:
            print(f"  {t.item.rel}: uploaded {u.when}"
                  + ("" if holds else " (probably: Moodle filters it, so can't say)"))
        u = members[0][1]
        if commit is None:
            print("    built outside git")
            continue
        since = commits_since(record.root, commit)
        print(f"    built from {u.source}" + (
            "; git doesn't know that commit" if since is None else
            "; no commits since" if not since else
            f"; {plural(len(since), 'commit')} since:"))
        for line in (since or [])[:shown]:
            print(f"      {line}")
        if since and len(since) > shown:
            print(f"      ...and {len(since) - shown} more")
        if since or dirty:
            print(f"    (git diff {commit[:7]} shows the changes to the sources)")


def run_diff(tmp, tool):
    """Compare tmp/moodle with tmp/local; False if the files should be kept."""
    sys.stdout.flush()
    if tool:
        args = shlex.split(tool)
        if not any("{old}" in a or "{new}" in a for a in args):
            args += ["{old}", "{new}"]
        args = [a.replace("{old}", str(tmp / "moodle")).replace("{new}", str(tmp / "local"))
                for a in args]
        try:
            subprocess.run(args, cwd=tmp, check=False)
        except OSError as e:
            print(f"  Couldn't run $ROODLE_DIFF ({tool}): {e}")
        print(f"  (The files are in {tmp})")
        return False
    for command in (["git", "diff", "--no-index", "--no-prefix", "moodle", "local"],
                    ["diff", "-ruN", "moodle", "local"]):
        try:
            subprocess.run(command, cwd=tmp, check=False)
            return True
        except FileNotFoundError:
            continue
    print(f"  Found neither git nor diff to compare them with: the files are in {tmp}")
    return False


def upload(moodle, course, pending, record=None):
    """Upload each target, asking what to do about any failure, and note
    each upload in `record`.  Returns the paths of those not uploaded."""
    failed = []
    commit, dirty = git_state(record.root) if record is not None else (None, False)
    for t in pending:
        print(f"  uploading {t.item.rel} ({size(t.item.path)})")
        while True:
            try:
                t.resource.replace(t.item.path)
            except TooLarge as e:
                print(f"    {e} -- not uploaded")
                failed.append(t.item.rel)
            except Exception as e:  # anything at all: the choice is the user's
                print(f"\n  The upload of {t.item.rel} failed: {e!r}")
                choice = ask("  [R]etry, [s]kip it, or [q]uit? ").strip().lower()
                if choice.startswith("s"):
                    failed.append(t.item.rel)
                elif choice.startswith("q"):
                    raise SystemExit(STOPPED)
                else:
                    moodle.wait_for_login(course.url, ask=ask)
                    continue
            else:
                if record is not None:
                    note_upload(record, t, commit, dirty)
            break
    return failed


def note_upload(record, target, commit, dirty):
    """Add an upload to the record: the main file Moodle now serves, and
    its content hash."""
    try:
        held = target.resource.held_file()
    except Exception as e:  # the upload is done: don't fail it for this
        print(f"    (not recorded: {e})")
        return
    local = {name: p for p, name in local_files(target.item.path)}
    if held is None or held.name not in local:
        return
    record.add(target.item.rel, target.resource.cmid, held.name,
               sha1_file(local[held.name]), commit, dirty)


# -------------------------------------------------------------- resources

def resources(args):
    config = Config.load(args.config)
    moodle, course = connect(config)
    moodle.wait_for_login(course.url, ask=ask)
    items, missing = config.items()
    targets, found = match(course, config, items)
    with ThreadPoolExecutor(max_workers=8) as ex:
        held = dict(zip(found, ex.map(lambda r: r.held_file(), found)))
    going = {t.resource.cmid: t.item.rel for t in targets if t.resource is not None}

    print(f"{plural(len(found), 'File resource')} in {course.url}:")
    section = object()
    for r in found:
        if r.section != section and r.section is not None:
            section = r.section
            print(f"\n  Section {section}")
        h = held[r]
        print(f"    '{r.name}'  cmid {r.cmid}, {visibility(r)}\n"
              f"        holds {h.name if h else '(no file)'}"
              + (f"  <- {going[r.cmid]}" if r.cmid in going else ""))
    unmatched = [t for t in targets if t.resource is None]
    if unmatched:
        print("\nBuilt files not published:")
        for t in unmatched:
            print(f"  {t.item.rel}: {t.note}")
    if missing:
        print(f"\nNot built yet: {', '.join(missing)}")
    return 0


# ------------------------------------------------------------------- main

def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=DEFAULT_CONFIG,
                        help=f"the config file (default: {DEFAULT_CONFIG})")
    parser = argparse.ArgumentParser(
        prog="roodle", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="\n\n".join(__doc__.strip().split("\n\n")[1:]))
    commands = parser.add_subparsers(dest="command", required=True)

    p = commands.add_parser(
        "publish", parents=[common],
        help="upload the built files that differ from what Moodle holds",
        description="Upload each built file the config lists to its File "
                    "resource, if it differs from what Moodle holds there.")
    p.add_argument("files", nargs="*", metavar="FILE",
                   help="upload only these, if changed: a path, a file "
                        "name, or a file name without its extension")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="only list what would be uploaded")
    p.add_argument("-d", "--diff", action="store_true",
                   help="show how each upload differs from what Moodle holds, "
                        "with git diff or $ROODLE_DIFF, and ask before uploading; "
                        "embedded images, fonts and scripts show as their size and hash")
    view = p.add_mutually_exclusive_group()
    view.add_argument("--text", action="store_true",
                      help="as --diff, comparing only the visible text of HTML "
                           "(and of PDFs, with pdftotext)")
    view.add_argument("--raw", action="store_true",
                      help="as --diff, comparing the files as they are")
    p.add_argument("--force", action="store_true",
                   help="upload everything (or every FILE), changed or not")
    p.add_argument("--check", action="store_true",
                   help="only check the FILE names and the login, e.g. before a build")
    p.set_defaults(func=publish)

    p = commands.add_parser(
        "resources", parents=[common],
        help="list the course's File resources and what goes in each",
        description="List the course's File resources: the file each holds, "
                    "whether students can see it, and which built file goes "
                    "to it.  Changes nothing.")
    p.set_defaults(func=resources)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print()
        raise SystemExit(STOPPED)
    except ConfigError as e:
        raise SystemExit(f"roodle: {e}")
    except (MoodleError, httpx.HTTPError) as e:
        raise SystemExit(f"roodle: {e}")


if __name__ == "__main__":
    sys.exit(main())
