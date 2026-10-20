"""
Script a Moodle course through a logged-in Firefox session.

    from roodle import Moodle

    moodle = Moodle.from_firefox("https://moodle.example.ac.uk")
    course = moodle.course(12345)
    for resource in course.resources():
        print(resource.cmid, resource.name, resource.held_file())
"""

from .client import Moodle, Page
from .course import Course, Module
from .errors import MoodleError, NotAFileResource, NotLoggedIn, TooLarge
from .resource import Comparison, FileResource, HeldFile

__all__ = [
    "Comparison", "Course", "FileResource", "HeldFile", "Module", "Moodle",
    "MoodleError", "NotAFileResource", "NotLoggedIn", "Page", "TooLarge",
]
