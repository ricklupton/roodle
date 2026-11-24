import base64
import shutil

import pytest

from roodle.diffview import collapse_katex, hide_blobs, prepare, view, visible_text

PNG = base64.b64encode(b"\x89PNG" + bytes(range(256)) * 40).decode()
FONT = base64.b64encode(b"wOF2" + bytes(range(256)) * 4).decode()


def test_hide_blobs():
    html = (f'<img src="data:image/png;base64,{PNG}" alt="x">\n'
            f"@font-face {{ src: url(data:font/woff2;charset=utf-8;base64,{FONT}) }}\n"
            '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">\n')
    out = hide_blobs(html)
    assert PNG not in out and FONT not in out
    assert '<img src="data:image/png;base64,[10 KB, sha1 ' in out
    assert "url(data:font/woff2;charset=utf-8;base64,[1 KB, sha1 " in out
    assert "R0lGODlhAQABAAAAACw=" in out          # too short to bother
    assert hide_blobs(html.replace(PNG, PNG[:-8] + "AAAAAAAA")) != out


SLIDES = f"""<!doctype html>
<html><head><title>Lecture 4</title><style>body {{ color: red }}</style>
<script>var x = 1;</script></head>
<body><div class="reveal"><div class="slides">
<section><h1>Lecture 4</h1><p>Regression, <em>briefly</em>.</p></section>
<section><h2>A model</h2>
<ul><li>First   point</li><li>Second<br>line</li></ul>
<p><img src="data:image/png;base64,{PNG}" alt="A plot"> <img src="figs/b.svg?v=2"></p>
<pre><code>def f(x):
    return x</code></pre>
<!-- a comment -->
<aside class="notes">Say hello</aside>
<span hidden>secret</span><span aria-hidden="true">icon</span>
</section></div></div><script>Reveal.initialize()</script></body></html>
"""


def test_visible_text():
    text = visible_text(SLIDES)
    assert text.startswith("Lecture 4\nRegression, briefly.\n\nA model\nFirst point\n"
                           "Second\nline\n[image: A plot, [10 KB, sha1 ")
    assert "] [image: b.svg]\ndef f(x):\n    return x\nSay hello\n" in text
    for unseen in ("color: red", "var x", "Reveal", "comment", "secret", "icon", PNG):
        assert unseen not in text


def test_prepare(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    for root, png, words in ((old, PNG, "old words"), (new, FONT, "new words")):
        (root / "a").mkdir(parents=True)
        (root / "a/page.html").write_text(f'<p>{words}</p><img src="data:image/png;base64,{PNG}">')
        (root / "a/style.html").write_text(f'<style>x</style><img src="data:i/p;base64,{png}">')
        (root / "a/x.bin").write_bytes(b"\0" + png.encode())
    assert prepare(old, new, "text") == []
    assert (new / "a/page.html").read_text().startswith("new words\n[image: ")
    assert (new / "a/x.bin").read_bytes() == b"\0" + FONT.encode()   # binary: left be


def test_prepare_drops_what_looks_the_same(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    for root, css in ((old, "a"), (new, "b")):
        root.mkdir()
        (root / "page.html").write_text(f"<style>{css}</style><p>Same</p>")
    assert prepare(old, new, "text") == ["page.html"]
    assert not (old / "page.html").exists() and not (new / "page.html").exists()


def test_raw_is_left_be(tmp_path):
    p = tmp_path / "page.html"
    p.write_text(f'<img src="data:image/png;base64,{PNG}">')
    view(p, "raw")
    assert PNG in p.read_text()


def tiny_pdf(text):
    """A one-page PDF showing `text`."""
    stream = f"BT /F1 20 Tf 72 700 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
               b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1, xref)
    return bytes(out)


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="needs pdftotext")
def test_pdf_text(tmp_path):
    (tmp_path / "a.pdf").write_bytes(tiny_pdf("Sheet one"))
    view(tmp_path / "a.pdf", "text")
    assert "Sheet one" in (tmp_path / "a.pdf").read_text()


# What pandoc writes for KaTeX to render in the browser, and what KaTeX
# renders in advance (as in Quarto, pre-rendered).
RAW_MATH = ('<p>Each value of <span class="math inline">X</span>, and</p>'
            '<p><span class="math display">\\sum_x p(x) = 1</span></p>')
KATEX_X = (
    '<span class="katex"><span class="katex-mathml"><math xmlns="http://www.w3.org/1998/'
    'Math/MathML"><semantics><mrow><mi>X</mi></mrow><annotation encoding="application/'
    'x-tex">X</annotation></semantics></math></span><span class="katex-html" aria-hidden='
    '"true"><span class="base"><span class="strut" style="height:0.6833em;"></span><span '
    'class="mord mathnormal" style="margin-right:0.07847em;">X</span></span></span></span>')
KATEX_SUM = (
    '<span class="katex-display"><span class="katex"><span class="katex-mathml"><math '
    'xmlns="http://www.w3.org/1998/Math/MathML" display="block"><semantics><mrow><msub>'
    '<mo>∑</mo><mi>x</mi></msub><mi>p</mi><mo>(</mo><mi>x</mi><mo>)</mo><mo>=</mo><mn>1'
    '</mn></mrow><annotation encoding="application/x-tex">\\sum_x p(x) = 1</annotation>'
    '</semantics></math></span><span class="katex-html" aria-hidden="true"><span class='
    '"base"><span class="mop">∑</span></span></span></span></span>')
RENDERED_MATH = (f'<p>Each value of <span class="math inline">{KATEX_X}</span>, and</p>'
                 f'<p><span class="math display">{KATEX_SUM}</span></p>')


def test_visible_text_of_maths():
    assert visible_text(RAW_MATH) == "Each value of $X$, and\n$$\\sum_x p(x) = 1$$\n"
    assert visible_text(RENDERED_MATH) == visible_text(RAW_MATH)
    mathjax = RAW_MATH.replace(">X<", ">\\(X\\)<").replace(
        "\\sum_x p(x) = 1", "\\[\\sum_x p(x) = 1\\]")
    assert visible_text(mathjax) == visible_text(RAW_MATH)


def test_collapse_katex():
    out = collapse_katex(RENDERED_MATH + "<span>after</span>")
    assert out == ('<p>Each value of <span class="math inline"><span class="katex">'
                   '[KaTeX: X]</span></span>, and</p><p><span class="math display">'
                   '<span class="katex-display">[KaTeX: \\sum_x p(x) = 1]</span></span></p>'
                   "<span>after</span>")


def test_hide_url_encoded_blobs():
    css = "%3Aroot%2C%5Bdata%2Dbs%2Dtheme%3Dlight%5D%20%7B" * 20
    html = (f'<link href="data:text/css,{css}" rel="stylesheet">\n'
            f"<img src='data:image/svg+xml,%3Csvg xmlns=%22a%22%3E{css}%3C/svg%3E'>\n"
            f"a {{ background: url(data:image/svg+xml,{css}) }}\n"
            '<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E">\n')
    out = hide_blobs(html)
    assert css not in out
    assert out.startswith('<link href="data:text/css,[580 bytes, sha1 ')
    assert "\" rel=\"stylesheet\">\n<img src='data:image/svg+xml,[" in out
    assert "url(data:image/svg+xml,[" in out and "]) }" in out
    assert '<img src="data:image/svg+xml,%3Csvg%3E%3C/svg%3E">' in out   # short: kept
