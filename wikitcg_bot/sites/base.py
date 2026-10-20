from __future__ import annotations

import random
import time
from typing import Protocol

from ..config import Settings
from ..models import SiteResult


class Site(Protocol):
    name: str
    label: str

    def configured(self, settings: Settings) -> bool: ...

    def run(self, settings: Settings) -> SiteResult: ...


def pause(settings: Settings) -> None:
    low, high = settings.delay
    if high > 0:
        time.sleep(random.uniform(low, high))
