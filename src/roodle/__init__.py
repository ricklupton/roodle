"""
Script a Moodle course through a logged-in Firefox session.
"""

from .client import Moodle, Page
from .course import Course, Module
from .errors import MoodleError, NotAFileResource, NotLoggedIn, TooLarge
from .resource import Comparison, FileResource, HeldFile

__all__ = [
    "Comparison", "Course", "FileResource", "HeldFile", "Module", "Moodle",
    "MoodleError", "NotAFileResource", "NotLoggedIn", "Page", "TooLarge",
]
