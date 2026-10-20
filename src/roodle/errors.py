"""
The exceptions roodle raises.
"""


class MoodleError(Exception):
    """Moodle refused a request, or answered with something unexpected."""


class NotLoggedIn(MoodleError):
    """The session's cookies don't give a logged-in Moodle session: log in
    in the browser, then `Moodle.refresh_cookies()` (or
    `Moodle.wait_for_login()`) and try again."""


class NotAFileResource(MoodleError):
    """A course module that was expected to be a File resource isn't one."""


class TooLarge(MoodleError):
    """A file is bigger than Moodle will accept in that file area."""
