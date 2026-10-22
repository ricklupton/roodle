"""
A connection to a Moodle site, borrowing a logged-in browser's cookies.

Logging in to a university Moodle usually goes through single sign-on,
which is hard to script.  Instead roodle reads the cookies of a browser that
is already logged in, and makes its requests with them.
"""

import json
import re
import time
from functools import cached_property
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from .errors import MoodleError, NotLoggedIn

# Error codes with which Moodle's AJAX service turns away a request from a
# session that has expired.
LOGIN_ERRORCODES = {"servicerequireslogin", "requireloginerror", "invalidsesskey"}


class Page:
    """An HTML page from Moodle."""

    def __init__(self, response):
        self.url = str(response.url)
        self.html = response.text

    @cached_property
    def soup(self):
        return BeautifulSoup(self.html, "lxml")

    @cached_property
    def cfg(self):
        """The page's `M.cfg`: the sesskey, the page's context id, and so on."""
        # Decode just the object: the same line can carry more script after it.
        m = re.search(r"\bM\.cfg\s*=\s*(?=\{)", self.html)
        if not m:
            return {}
        try:
            return json.JSONDecoder().raw_decode(self.html, m.end())[0]
        except json.JSONDecodeError:
            return {}

    @property
    def logged_in(self):
        """Moodle marks any page seen while logged out with a `notloggedin`
        body class."""
        body = re.search(r'<body\b[^>]*\bclass="([^"]*)"', self.html)
        return body is not None and "notloggedin" not in body.group(1).split()


class Moodle:
    """A Moodle site, as seen by whoever the cookies belong to.

    `cookies` is anything httpx accepts (a `http.cookiejar.CookieJar`, a
    dict), or `cookie_source` a function giving them, called again by
    `refresh_cookies()` -- `from_firefox` sets this up.  `transport` is
    passed to httpx, for testing.
    """

    def __init__(self, server, cookies=None, *, cookie_source=None, timeout=30,
                 transport=None):
        if "://" not in server:
            server = "https://" + server
        self.server = server.rstrip("/")
        url = urlparse(self.server)
        self.host = url.netloc
        self._login_path = url.path.rstrip("/") + "/login/"
        self._cookie_source = cookie_source
        if cookies is None and cookie_source is not None:
            cookies = cookie_source()
        # Moodle pages are always UTF-8: say so for those that don't declare
        # a charset, rather than have them read as Latin-1.
        self.http = httpx.Client(base_url=self.server, cookies=cookies,
                                 timeout=timeout, transport=transport,
                                 default_encoding="utf-8")
        self._sesskey = None

    @classmethod
    def from_firefox(cls, server, **kwargs):
        """Use the cookies of a Firefox logged in to `server`, read afresh by
        each `refresh_cookies()`."""
        host = urlparse(server if "://" in server else "https://" + server).netloc

        def firefox_cookies():
            import browser_cookie3
            try:
                return browser_cookie3.firefox(domain_name=host)
            except browser_cookie3.BrowserCookieError as e:
                raise MoodleError(f"Couldn't read Firefox's cookies: {e}") from e

        return cls(server, cookie_source=firefox_cookies, **kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.http.close()

    def course(self, course_id):
        from .course import Course
        return Course(self, course_id)

    # ----------------------------------------------------------- requests

    def url(self, path):
        """The absolute URL of a path on the site (or of a URL, itself)."""
        if "://" in path:
            return path
        return self.server + "/" + path.lstrip("/")

    def get(self, url, **kwargs):
        return self.http.get(url, **kwargs)

    def post(self, url, **kwargs):
        return self.http.post(url, **kwargs)

    def is_login_redirect(self, response):
        """Whether `response` sends us to log in: to the login page, or on
        to another site (the university's single sign-on)."""
        if not response.is_redirect:
            return False
        target = response.url.join(response.headers.get("Location", ""))
        return target.host != response.url.host or target.path.startswith(self._login_path)

    def check(self, response):
        """Raise NotLoggedIn or MoodleError if `response` isn't a success."""
        if self.is_login_redirect(response):
            raise NotLoggedIn(f"Not logged in to Moodle (at {response.url})")
        if response.is_error:
            raise MoodleError(f"Moodle answered {response.status_code} for {response.url}")
        return response

    def page(self, url, params=None):
        """GET an HTML page, following redirects, as a `Page`.

        Raises NotLoggedIn if it lands on the login page, another site or a
        page marked as seen logged out.
        """
        r = self.http.get(url, params=params, follow_redirects=True)
        for step in (*r.history, r):
            if self.is_login_redirect(step) or step.url.path.startswith(self._login_path):
                raise NotLoggedIn(f"Not logged in to Moodle (at {step.url})")
        self.check(r)
        page = Page(r)
        if not page.logged_in:
            raise NotLoggedIn(f"Not logged in to Moodle (at {page.url})")
        if page.cfg.get("sesskey"):
            self._sesskey = page.cfg["sesskey"]
        return page

    @property
    def sesskey(self):
        """The session key that Moodle wants with every change."""
        if self._sesskey is None:
            self.page("/")
            if self._sesskey is None:
                raise MoodleError("No sesskey in M.cfg on the front page")
        return self._sesskey

    def ajax(self, methodname, args):
        """Call one of Moodle's AJAX web service functions, as its own
        JavaScript does, and give its result."""
        r = self.check(self.http.post(
            "/lib/ajax/service.php",
            params={"sesskey": self.sesskey, "info": methodname},
            json=[{"index": 0, "methodname": methodname, "args": args}],
        ))
        data = r.json()
        result = data[0] if isinstance(data, list) and len(data) == 1 else data
        if not isinstance(result, dict):
            raise MoodleError(f"{methodname}: unexpected answer {data!r:.200}")
        if result.get("error"):
            exc = result.get("exception") or {}
            code = exc.get("errorcode") or result.get("errorcode")
            message = exc.get("message") or result.get("error")
            if code in LOGIN_ERRORCODES:
                raise NotLoggedIn(f"{methodname}: {message}")
            raise MoodleError(f"{methodname}: {code}: {message}")
        return result.get("data")

    # -------------------------------------------------------------- login

    def refresh_cookies(self):
        """Read the cookies afresh, to pick up a login since they were read."""
        if self._cookie_source is not None:
            self.http.cookies = self._cookie_source()
        self._sesskey = None

    def logged_in(self, url="/"):
        """Whether the cookies give a logged-in session."""
        try:
            self.page(url)
        except NotLoggedIn:
            return False
        return True

    def wait_for_login(self, url="/", ask=input, *, tries=6, interval=5.0):
        """Return once logged in, asking whoever is at the terminal to log in
        in the browser until they have.

        Firefox saves its cookies only every 15 s or so, so a login that has
        just happened may not be visible straight away: after each Enter it
        tries `tries` times, `interval` seconds apart.
        """
        self.refresh_cookies()
        if self.logged_in(url):
            return
        print(f"\nNot logged in to Moodle. Log in, in the browser, at\n\n"
              f"    {self.url(url)}\n")
        while True:
            ask("then press Enter to carry on (Ctrl-C to give up) ")
            for attempt in range(tries):
                self.refresh_cookies()
                if self.logged_in(url):
                    print("  Logged in.")
                    return
                if attempt == 0:
                    print("  Not yet -- waiting for the browser to save its cookies...")
                time.sleep(interval)
            print("  Still not logged in.")
