"""
Load current session data from Firefox.
"""

from pathlib import Path
from datetime import datetime
import json
import lz4.block
import requests
from platformdirs import user_data_dir


def find_firefox_session():
    # FIXME not tested on Windows -- but should work?
    firefox_dir = Path(user_data_dir("Firefox", "Mozilla")) / "Profiles"
    for profile_dir in firefox_dir.rglob("*.*/sessionstore-backups/recovery.jsonlz4"):
        return profile_dir


def load_session_cookies(filename):
    with open(filename, mode="rb") as fp:
        if fp.read(8) != b"mozLz40\0":
            raise ValueError("Session file signature not recognised")
        x = lz4.block.decompress(fp.read())
        data = json.loads(x)
    return data["cookies"]


def find_cookies_for_host(cookies, host):
    return {c["name"]: c["value"] for c in cookies if c.get("host", "") == host}


def get_session_for_host(host):
    filename = find_firefox_session()
    cookie_data = load_session_cookies(filename)
    host_cookies = find_cookies_for_host(cookie_data, host)
    s = requests.Session()
    s.cookies.update(host_cookies)
    return s
