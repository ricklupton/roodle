"""
Ways of looking at built files that make a diff of them readable -- what
`roodle publish --diff` does to Moodle's copies and the local ones before
comparing them.

A self-contained HTML page carries its images, fonts and scripts inside it,
as base64 `data:` URIs, so a plain diff of two versions is mostly base64.
`hide_blobs` replaces each with its size and hash, which still change when
the blob does.  `visible_text` goes further, and keeps only what a reader
sees: the text, one block (paragraph, heading, list item...) per line, and
a line for each image.  `pdf_text` does the same for a PDF, with pdftotext.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PreformattedString, Tag

DATA_URI = re.compile(
    r"data:(?P<type>[\w.+-]+/[\w.+-]+)?(?P<params>(?:;[\w.+-]+=[^;,\s\"'()]*)*);base64,"
    r"(?P<data>[A-Za-z0-9+/]{64,}={0,2})")

HTML = {".html", ".htm", ".xhtml"}

# Elements whose content starts on a line of its own.
BLOCKS = {
    "address", "article", "aside", "blockquote", "body", "caption", "dd", "details",
    "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol",
    "p", "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr",
    "ul",
}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
UNSEEN = {"head", "script", "style", "noscript", "template", "button"}


def short_size(n):
    if n < 1024:
        return f"{n} bytes"
    if n < 2**20:
        return f"{n / 1024:.0f} KB"
    return f"{n / 2**20:.1f} MB"


def blob_summary(data):
    """The size and a short hash of base64 `data`, as text."""
    n = len(data) * 3 // 4 - data.count("=")
    return f"[{short_size(n)}, sha1 {hashlib.sha1(data.encode()).hexdigest()[:7]}]"


def hide_blobs(text):
    """`text` with each base64 data: URI's data replaced by its size and
    a short hash: `data:image/png;base64,[48 KB, sha1 3f2a9c1]`."""
    def replace(m):
        return m.group(0)[:m.start("data") - m.start()] + blob_summary(m.group("data"))
    return DATA_URI.sub(replace, text)


def visible_text(html):
    """What a reader sees of an HTML page, as text: each block on a line of
    its own, a blank line before each heading and each slide (<section>),
    preformatted text as it is, and `[image: ALT, ...]` for each image."""
    soup = BeautifulSoup(html, "lxml")
    lines, words = [], []

    def end_line():
        line = " ".join("".join(words).split())
        if line:
            lines.append(line)
        words.clear()

    def gap():
        end_line()
        if lines and lines[-1] != "":
            lines.append("")

    def image(tag):
        src = tag.get("src") or tag.get("data-src") or ""
        m = DATA_URI.match(src)
        what = blob_summary(m.group("data")) if m else Path(src.split("?")[0]).name
        alt = (tag.get("alt") or "").strip()
        return f"[image: {alt + ', ' if alt else ''}{what}]"

    def walk(node):
        for child in node.children:
            if isinstance(child, PreformattedString):   # comments, doctype...
                continue
            if isinstance(child, NavigableString):
                words.append(str(child))
                continue
            if not isinstance(child, Tag) or child.name in UNSEEN:
                continue
            if child.has_attr("hidden") or child.get("aria-hidden") == "true":
                continue
            name = child.name
            if name == "br":
                end_line()
            elif name == "img":
                words.append(" " + image(child) + " ")
            elif name == "pre":
                end_line()
                lines.extend(hide_blobs(child.get_text()).rstrip("\n").split("\n"))
            elif name in HEADINGS or name == "section":
                gap()
                walk(child)
                end_line()
            elif name in BLOCKS:
                end_line()
                walk(child)
                end_line()
            else:
                walk(child)

    walk(soup)
    end_line()
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


def pdf_text(path):
    """The text of a PDF, laid out as on the page, or None without
    pdftotext (from poppler) to read it."""
    if not shutil.which("pdftotext"):
        return None
    r = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                       capture_output=True, check=False)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def as_text(data):
    """`data` as text, if it is UTF-8 and not binary; else None."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if "\0" in text else text


def view(path, mode):
    """Rewrite the file at `path` as `mode` shows it: "raw" leaves it be,
    "code" hides its blobs, "text" keeps only its visible text (for HTML
    and, with pdftotext, PDF; other files as for "code")."""
    if mode == "raw":
        return
    if mode == "text" and path.suffix.lower() == ".pdf":
        text = pdf_text(path)
        if text is not None:
            path.write_text(text, encoding="utf-8")
        return
    text = as_text(path.read_bytes())
    if text is None:
        return
    if mode == "text" and path.suffix.lower() in HTML:
        path.write_text(visible_text(text), encoding="utf-8")
    else:
        path.write_text(hide_blobs(text), encoding="utf-8")


def prepare(old, new, mode):
    """Rewrite the files in directories `old` and `new` (Moodle's copies
    and the local ones) as `mode` shows them, and remove any pair that then
    looks the same.  Returns the paths, relative to the directories, of the
    pairs removed."""
    for root in (old, new):
        for p in root.rglob("*"):
            if p.is_file():
                view(p, mode)
    same = []
    for p in sorted(new.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(new)
        q = old / rel
        if q.is_file() and q.read_bytes() == p.read_bytes():
            p.unlink()
            q.unlink()
            same.append(rel.as_posix())
    return same
