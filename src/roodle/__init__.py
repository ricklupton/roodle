from .firefox_session import get_session_for_host
from .moodle_session import MoodleCourse


def connect_via_firefox(server, *args, **kwargs):
    """Connect to Moodle at `server`.  Args passed to `MoodleCourse`."""
    if server.startswith("https://"):
        host = server[8:]
    elif server.startswith("http://"):
        host = server[7:]
    else:
        host = server

    s = get_session_for_host(host)
    course = MoodleCourse(server, *args, **kwargs)
    course.connect(s)
    return course
