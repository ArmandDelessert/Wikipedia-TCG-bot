import base64
import json
import os
import time
from pathlib import Path

import pytest

from wikitcg_bot.config import Settings


def make_jwt(exp: int) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


LOCAL_CHROME = next((p for p in (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
) if Path(p).is_file()), None)

# Les tests qui pilotent un vrai navigateur : le Chrome installé en local, le Chromium de Playwright en CI.
requires_browser = pytest.mark.skipif(
    not (os.environ.get("CHROMIUM_PATH") or os.environ.get("CI") or LOCAL_CHROME),
    reason="ni Chrome local, ni CHROMIUM_PATH, ni CI",
)


@pytest.fixture
def jwt_valid():
    return make_jwt(int(time.time()) + 3600)


@pytest.fixture
def make_settings(tmp_path):
    def factory(**env):
        return Settings(
            env=env, state_dir=tmp_path / "state", cookies_dir=tmp_path / "Cookies", delay=(0, 0),
            max_packs=10,
            # En CI, le Chromium de Playwright ; en local, le vrai Chrome (canal « chrome »).
            browser_channel=None if os.environ.get("CI") else "chrome",
        )
    return factory
