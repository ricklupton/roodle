import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from fake_moodle import FakeMoodle  # noqa: E402


@pytest.fixture
def fake():
    return FakeMoodle()


@pytest.fixture
def moodle(fake):
    return fake.moodle()
