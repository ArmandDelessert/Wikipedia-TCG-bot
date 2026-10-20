import base64
import json
import time

import pytest

from wikitcg_bot.config import Settings


def make_jwt(exp: int) -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


@pytest.fixture
def jwt_valid():
    return make_jwt(int(time.time()) + 3600)


@pytest.fixture
def make_settings(tmp_path):
    def factory(**env):
        return Settings(env=env, state_dir=tmp_path / "state", delay=(0, 0), max_packs=10)
    return factory
