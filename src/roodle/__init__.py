import browser_cookie3
import requests
from .moodle_session import MoodleCourse


def connect_via_firefox(server, *args, **kwargs):
    """Connect to Moodle at `server`.  Args passed to `MoodleCourse`."""
    if server.startswith("https://"):
        host = server[8:]
    elif server.startswith("http://"):
        host = server[7:]
    else:
        host = server

    cookies = browser_cookie3.firefox(domain_name=host)
    print("Found cookies:", cookies)
    s = requests.Session()
    print(s.cookies)
    s.cookies = cookies
    print(s.cookies)
    # s = get_session_for_host(host)
    course = MoodleCourse(server, *args, **kwargs)
    course.connect(s)
    return course
