import time

import httpx
import pytest

from tests.conftest import make_jwt
from wikitcg_bot.models import AuthError, SiteError
from wikitcg_bot.sites.wikitcg_net import WikiTcgNet, choose_series, jwt_expiry, pick_session


def test_jwt_expiry():
    assert jwt_expiry(make_jwt(1234)) == 1234
    assert jwt_expiry("pas-un-jwt") is None


def test_pick_session_prefers_freshest():
    older, newer = make_jwt(int(time.time()) + 100), make_jwt(int(time.time()) + 900)
    assert pick_session([older, newer]) == newer
    assert pick_session([newer, older]) == newer


def test_pick_session_ties_go_to_first_candidate():
    assert pick_session(["opaque-a", "opaque-b"]) == "opaque-a"


def test_pick_session_rejects_expired():
    with pytest.raises(AuthError, match="expiré"):
        pick_session([make_jwt(int(time.time()) - 10)])


def test_choose_series_picks_most_missing():
    series = [{"id": "a", "cardCount": 50}, {"id": "b", "cardCount": 50}, {"id": "c", "cardCount": 10}]
    collection = [{"series_id": "a", "owned": 49}, {"series_id": "b", "owned": 10}]
    assert choose_series(series, collection) == "b"
    assert choose_series(series, collection, pinned="c") == "c"
    with pytest.raises(SiteError):
        choose_series([], [])


class FakeWikiTcg:
    """Simule le sous-ensemble de l'API utilisé par le bot."""

    def __init__(self, packs=3, mystery_error="cooldown"):
        self.packs, self.opened, self.mystery_error = packs, [], mystery_error
        self.cookies_seen = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.cookies_seen.append(request.headers.get("cookie", ""))
        path = request.url.path
        if path == "/api/nav/status":
            return httpx.Response(200, json={"totalAvailable": self.packs, "freePacks": self.packs,
                                             "maxFreePacks": 5, "nextRegenAt": "demain"},
                                  headers={"set-cookie": "wtcg_session=rotated; Domain=wikitcg.net; Path=/"})
        if path == "/api/series":
            return httpx.Response(200, json={"series": [{"id": "s1", "cardCount": 20},
                                                        {"id": "s2", "cardCount": 20}]})
        if path == "/api/collection":
            return httpx.Response(200, json=[{"series_id": "s1", "owned": 20},
                                             {"series_id": "s2", "owned": 5}])
        if path == "/api/packs/open":
            sid = __import__("json").loads(request.content)["seriesId"]
            if sid == "mystery":
                return httpx.Response(200, json={"error": self.mystery_error})
            if self.packs <= 0:
                return httpx.Response(200, json={"error": "no packs"})
            self.packs -= 1
            self.opened.append(sid)
            return httpx.Response(200, json={
                "cards": [{"id": "c1", "rarity": "UR", "article": {"title": "Paris"}},
                          {"id": "c2", "rarity": "C", "article": {"title": "Seine"}}],
                "totalAvailable": self.packs})
        return httpx.Response(404)


def test_opens_all_packs_on_the_series_with_most_missing_cards(make_settings, jwt_valid):
    api = FakeWikiTcg(packs=3)
    settings = make_settings(WIKITCG_NET_SESSION=jwt_valid)
    result = WikiTcgNet(transport=httpx.MockTransport(api)).run(settings)
    assert result.opened == 3 and result.ok
    assert api.opened == ["s2", "s2", "s2"]
    assert result.packs_before == 3 and result.packs_max == 5
    assert {c.title for c in result.cards} == {"Paris", "Seine"}
    assert "Tous les packs" in result.message


def test_respects_max_packs(make_settings, jwt_valid):
    settings = make_settings(WIKITCG_NET_SESSION=jwt_valid)
    settings.max_packs = 2
    result = WikiTcgNet(transport=httpx.MockTransport(FakeWikiTcg(packs=5))).run(settings)
    assert result.opened == 2 and "Limite" in result.message


def test_persists_cookie_rotated_by_server(make_settings, jwt_valid):
    settings = make_settings(WIKITCG_NET_SESSION=jwt_valid)
    WikiTcgNet(transport=httpx.MockTransport(FakeWikiTcg(packs=0))).run(settings)
    assert (settings.state_dir / "wikitcg_net_session.txt").read_text() == "rotated"


def test_dry_run_opens_nothing(make_settings, jwt_valid):
    api = FakeWikiTcg(packs=3)
    settings = make_settings(WIKITCG_NET_SESSION=jwt_valid)
    settings.dry_run = True
    result = WikiTcgNet(transport=httpx.MockTransport(api)).run(settings)
    assert result.opened == 0 and api.opened == [] and api.packs == 3


def test_no_pack_available(make_settings, jwt_valid):
    result = WikiTcgNet(transport=httpx.MockTransport(FakeWikiTcg(packs=0))).run(
        make_settings(WIKITCG_NET_SESSION=jwt_valid))
    assert result.opened == 0 and "Aucun pack" in result.message


def test_rejected_cookie_is_an_auth_error(make_settings, jwt_valid):
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    with pytest.raises(AuthError):
        WikiTcgNet(transport=transport).run(make_settings(WIKITCG_NET_SESSION=jwt_valid))
