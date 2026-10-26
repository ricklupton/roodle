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
from .errors import MoodleError, TooLarge
from .publish import DEFAULT_CONFIG, Config, ConfigError, match, plan

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
    return {True: "visible to students", False: "hidden"}.get(
        resource.visible, "visibility unknown")


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
              "there's no telling whether they changed -- is 'Use filters on "
              "file content' on for them?")
    failed_matches = 1 if only is not None and p.unmatched else 0

    pending = p.pending(force=args.force or only is not None)
    if not pending:
        if p.unchanged:
            print(f"Moodle is up to date: {plural(len(p.unchanged), 'resource')} "
                  "hold the same as the build.")
        return failed_matches
    if args.dry_run:
        print("Would upload" + ("" if only else f" ({len(p.unchanged)} unchanged)") + ":")
        for t in pending:
            print(f"  {t.item.rel}")
        if args.diff:
            diff(pending)
        return failed_matches

    confirm(pending, args.diff)
    failed = upload(moodle, course, pending)
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


def confirm(pending, show_diff=False):
    """List where each file is going, by the resource's name on Moodle and
    whether students can see it, and ask first if any resource holds a file
    of a different name now -- the sign of a mistyped cmid -- or after
    showing the diff."""
    print("\nTo upload:")
    for t in pending:
        r = t.resource
        print(f"  {t.item.rel}\n      -> '{r.name}', cmid {r.cmid}, {visibility(r)}"
              + ("" if t.held_ok else f"   <- holds {t.held or 'no file'} now"))
    if show_diff:
        diff(pending)
    if not all(t.held_ok for t in pending):
        question = ("Some of these resources hold a file of another name now: check "
                    "they are the right ones. Upload? [Y/n] ")
    elif show_diff:
        question = "Upload? [Y/n] "
    else:
        question = None
    if question and not yes(question, True):
        raise SystemExit("Not uploading.")
    print()


def diff(pending):
    """Show how each upload would change what Moodle holds.

    Moodle's copies of the files that differ, and the local ones, are put in
    a temporary directory as moodle/ and local/, each under the item's path
    from the config file, and compared with `git diff --no-index` -- so
    git's pager, colours and diff settings apply -- or `diff -ruN` without
    git.  $ROODLE_DIFF, if set, is a command to run instead: it is given the
    two directories, or they replace {old} and {new} in it.  Its directory
    is kept, in case the command returns before it has read the files
    (`emacsclient`, say).
    """
    tmp = Path(tempfile.mkdtemp(prefix="roodle-diff-"))
    try:
        if not gather_changes(pending, tmp / "moodle", tmp / "local"):
            print("  No differences: Moodle holds the same files.")
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
    old.mkdir(exist_ok=True)
    new.mkdir(exist_ok=True)
    return changed


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


def upload(moodle, course, pending):
    """Upload each target, asking what to do about any failure.  Returns
    the paths of those not uploaded."""
    failed = []
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
            break
    return failed


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
                   help="upload only these, changed or not: a path, a file "
                        "name, or a file name without its extension")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="only list what would be uploaded")
    p.add_argument("-d", "--diff", action="store_true",
                   help="show how each upload differs from what Moodle holds, "
                        "with git diff or $ROODLE_DIFF, and ask before uploading")
    p.add_argument("--force", action="store_true",
                   help="upload everything, changed or not")
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
