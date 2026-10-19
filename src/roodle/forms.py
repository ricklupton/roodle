"""
Generic helpers for scraping and resubmitting Moodle "mform" pages.

Moodle's own file manager / edit-module pages are ordinary HTML forms with
no public API of their own.  The approach here is the same one a browser
uses: load the page, read off the form's current field values, change the
one or two fields you actually mean to change, and POST the rest back
unchanged.  That avoids having to know (and keep up to date) every hidden
field a particular mform happens to render.
"""


def find_moodleform(soup):
    """Find the <form> on a page that is a Moodle quickform (mform).

    Moodle quickforms always carry a hidden `_qf__<formclass>` marker input;
    this is a more reliable way to find the form than guessing an id (which
    can vary, and a page may contain other forms, e.g. a search box).
    """
    marker = soup.find("input", attrs={"name": lambda n: n and n.startswith("_qf__")})
    if marker is None:
        raise ValueError("No moodle form (_qf__ marker) found on page")
    form = marker.find_parent("form")
    if form is None:
        raise ValueError("_qf__ marker input is not inside a <form>")
    return form


def serialize_form(form):
    """Serialise a Moodle mform the way a browser would submit it.

    Hidden/text/number/etc. inputs, selects and textareas contribute their
    current value. Checked checkboxes/radios contribute their value;
    unchecked ones are skipped -- Moodle's "advcheckbox" elements render an
    off-value hidden input immediately before the checkbox sharing the same
    name, so processing elements in document order and skipping unchecked
    boxes naturally reproduces the value a real submit would send. Submit,
    button, image, reset and file inputs are skipped entirely (the caller
    sets `submitbutton` itself, and file uploads are handled separately via
    the file manager's draft area, not a raw file input).
    """
    fields = {}
    for el in form.find_all(["input", "select", "textarea"]):
        name = el.get("name")
        if not name or el.get("disabled") is not None:
            continue

        if el.name == "select":
            selected = el.find("option", selected=True) or el.find("option")
            fields[name] = selected.get("value", selected.text) if selected else ""
            continue

        if el.name == "textarea":
            fields[name] = el.text
            continue

        input_type = (el.get("type") or "text").lower()
        if input_type in ("submit", "button", "image", "file", "reset"):
            continue
        if input_type in ("checkbox", "radio"):
            if el.get("checked") is not None:
                fields[name] = el.get("value", "1")
            continue
        fields[name] = el.get("value", "")

    return fields
