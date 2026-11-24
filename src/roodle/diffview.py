"""
Ways of looking at built files that make a diff of them readable -- what
`roodle publish --diff` does to Moodle's copies and the local ones before
comparing them.

A self-contained HTML page carries its images, fonts, styles and scripts
inside it, as `data:` URIs (base64, or URL-encoded), so a plain diff of two
versions is mostly blobs.  `hide_blobs` replaces each with its size and hash,
which still change when the blob does.  Maths that KaTeX has rendered in
advance is a screenful of markup per symbol: `collapse_katex` replaces it
with its TeX.  `visible_text` goes further, and keeps only what a reader
sees: the text, one block (paragraph, heading, list item...) per line, each
formula as its TeX, and a line for each image.  `notebook_text` does the
same for a Jupyter notebook: each cell's source, and what its outputs show.
`pdf_text` does the same for a PDF, with pdftotext.
"""

import base64 as b64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup
from bs4.element import NavigableString, PreformattedString, Tag

DATA_URI = re.compile(
    r"data:(?P<type>[\w.+-]+/[\w.+-]+)?(?P<params>(?:;[\w.+-]+=[^;,\s\"'()]*)*);base64,"
    r"(?P<data>[A-Za-z0-9+/]{64,}={0,2})")

# A data: URI that isn't base64, after the quote or bracket it starts with.
PLAIN_DATA_URI = re.compile(r"""(?P<open>["'(])\s*data:(?P<head>[^,"'()\s<>]*),""")
CLOSE = {'"': '"', "'": "'", "(": ")"}
LONG = 200      # shorter URL-encoded data is left be: it may be worth reading

KATEX = re.compile(r'<span class="katex(?:-display)?(?:\s[^"]*)?">')
SPAN = re.compile(r"<span\b[^>]*>|</span>")
TEX = re.compile(r'<annotation encoding="application/x-tex">(.*?)</annotation>', re.S)

# What MathJax, or a page's own script, may put around TeX in the page.
DELIMITERS = [("\\(", "\\)"), ("\\[", "\\]"), ("$$", "$$"), ("$", "$")]

# In a Jupyter notebook, the base64 data of an output (or an attachment)
# for a binary MIME type: "image/png": "iVBORw0KGgo...\n"
NOTEBOOK_BLOB = re.compile(
    r'(?P<key>"[\w.+-]+/[\w.+-]+"\s*:\s*)"(?P<data>(?:[A-Za-z0-9+/]|\\n){64,}=*(?:\\n)?)"')

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


def blob_summary(data, base64=True):
    """The size and a short hash of a data: URI's `data`, as text."""
    if base64:
        n = len(data) * 3 // 4 - data.count("=")
    else:
        n = len(unquote(data).encode())
    return f"[{short_size(n)}, sha1 {hashlib.sha1(data.encode()).hexdigest()[:7]}]"


def hide_blobs(text):
    """`text` with each data: URI's data replaced by its size and a short
    hash: `data:image/png;base64,[48 KB, sha1 3f2a9c1]`.  Base64 data is
    replaced from 64 characters, URL-encoded data (`data:text/css,%3Aroot...`)
    from 200, and only when it is quoted or in CSS's url(...), to know
    where it ends."""
    def replace(m):
        return m.group(0)[:m.start("data") - m.start()] + blob_summary(m.group("data"))
    text = DATA_URI.sub(replace, text)

    out, pos = [], 0
    for m in PLAIN_DATA_URI.finditer(text):
        if m.start() < pos or ";base64" in m.group("head"):
            continue
        end = text.find(CLOSE[m.group("open")], m.end())
        if end == -1 or end - m.end() < LONG:
            continue
        out += [text[pos:m.end()], blob_summary(text[m.end():end], base64=False)]
        pos = end
    return "".join(out) + text[pos:]


def hide_notebook_blobs(text):
    """A Jupyter notebook's text, with the base64 data of each output (or
    attachment) replaced by its size and a short hash, still in quotes:
    `"image/png": "[48 KB, sha1 3f2a9c1]"`."""
    def replace(m):
        return f'{m.group("key")}"{blob_summary(m.group("data").replace(chr(92) + "n", ""))}"'
    return NOTEBOOK_BLOB.sub(replace, text)


def collapse_katex(text):
    """`text` with the markup of each formula KaTeX has rendered replaced
    by its TeX: `<span class="katex">[KaTeX: x^2]</span>`."""
    out, pos = [], 0
    for m in KATEX.finditer(text):
        if m.start() < pos:
            continue            # inside the last one (katex-display holds katex)
        depth, end = 0, None
        for tag in SPAN.finditer(text, m.start()):
            depth += -1 if tag.group().startswith("</") else 1
            if depth == 0:
                end = tag.end()
                break
        if end is None:
            break
        tex = TEX.search(text, m.start(), end)
        out += [text[pos:m.start()], m.group(),
                f"[KaTeX: {tex.group(1).strip() if tex else '?'}]</span>"]
        pos = end
    return "".join(out) + text[pos:]


def tex_of(tag):
    """The TeX of a formula: from the annotation KaTeX (or MathJax) puts in
    its MathML, or else its text, without delimiters around it."""
    annotation = tag.find("annotation", encoding="application/x-tex")
    if annotation is not None:
        return annotation.get_text().strip()
    if tag.name == "math":
        return " ".join(tag.get_text(" ").split())
    tex = tag.get_text().strip()
    for start, end in DELIMITERS:
        if tex.startswith(start) and tex.endswith(end) and len(tex) >= len(start + end):
            return tex[len(start):len(tex) - len(end)].strip()
    return tex


def visible_text(html):
    """What a reader sees of an HTML page, as text: each block on a line of
    its own, a blank line before each heading and each slide (<section>),
    preformatted text as it is, each formula as its TeX between $s (or $$s,
    for display maths), whether it is TeX in the page or rendered already,
    and `[image: ALT, ...]` for each image."""
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
        if m:
            what = blob_summary(m.group("data"))
        elif src.startswith("data:"):
            what = blob_summary(src.partition(",")[2], base64=False)
        else:
            what = Path(src.split("?")[0]).name
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
            classes = child.get("class") or []
            if name == "span" and "math" in classes or name == "math":
                display = "display" in classes or child.get("display") == "block"
                words.append(f"$${tex_of(child)}$$" if display else f"${tex_of(child)}$")
            elif name == "br":
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


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def joined(value):
    """A notebook's multi-line string: a string, or a list of lines."""
    return "".join(value) if isinstance(value, list) else str(value or "")


def notebook_text(text):
    """What a reader sees of a Jupyter notebook, as text: for each cell, a
    `## code` (or markdown, raw) line, its source, and what each output
    shows, under `[output]` (or `[stdout]`, `[stderr]`, `[error]`): an image
    as a line with its type, size and hash, HTML as its visible text (see
    `visible_text`), or else Markdown, LaTeX or plain text.  Execution
    counts, ids and metadata, which change on every run, are left out.

    None if `text` isn't a notebook."""
    try:
        cells = json.loads(text).get("cells")
    except (ValueError, AttributeError):
        return None
    if not isinstance(cells, list):
        return None
    lines = []

    def block(label, body):
        body = ANSI.sub("", body).rstrip("\n")
        if body.strip():
            lines.append(label)
            lines.extend(body.split("\n"))

    def blob(mime, data):
        data = joined(data).replace("\n", "")
        return f"[{mime}: {blob_summary(data)[1:-1]}]"

    for cell in cells:
        if not isinstance(cell, dict):
            continue
        if lines:
            lines.append("")
        lines.append(f"## {cell.get('cell_type', 'cell')}")
        source = joined(cell.get("source")).rstrip("\n")
        if source:
            lines.extend(source.split("\n"))
        for name, bundle in sorted((cell.get("attachments") or {}).items()):
            for mime, data in sorted(bundle.items()):
                lines.append(f"[attachment {name}: {blob(mime, data)[1:]}")
        for out in cell.get("outputs") or []:
            kind = out.get("output_type")
            if kind == "stream":
                block(f"[{out.get('name', 'stream')}]", joined(out.get("text")))
            elif kind == "error":
                block("[error]", f"{out.get('ename', '')}: {out.get('evalue', '')}")
            elif kind in ("execute_result", "display_data"):
                data = out.get("data") or {}
                shown = [blob(mime, value) for mime, value in sorted(data.items())
                         if mime.startswith("image/") and mime != "image/svg+xml"]
                if "image/svg+xml" in data:
                    shown.append(blob("image/svg+xml", b64.b64encode(
                        joined(data["image/svg+xml"]).encode()).decode()))
                if "text/html" in data:
                    shown.append(visible_text(joined(data["text/html"])))
                elif "text/markdown" in data:
                    shown.append(joined(data["text/markdown"]))
                elif "text/latex" in data:
                    shown.append(joined(data["text/latex"]))
                elif "text/plain" in data and not shown:
                    shown.append(joined(data["text/plain"]))
                elif not shown:
                    shown.append(f"[{', '.join(sorted(data)) or 'nothing'}]")
                block("[output]", "\n".join(s.rstrip("\n") for s in shown))
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
    "code" hides its blobs, "text" keeps only its visible text (for HTML,
    notebooks and, with pdftotext, PDF; other files as for "code")."""
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
        return
    if path.suffix.lower() == ".ipynb":
        if mode == "text":
            shown = notebook_text(text)
            if shown is not None:
                path.write_text(shown, encoding="utf-8")
                return
        text = hide_notebook_blobs(text)
    path.write_text(hide_blobs(collapse_katex(text)), encoding="utf-8")


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
