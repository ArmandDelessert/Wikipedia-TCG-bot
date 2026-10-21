import json
import logging
import os
import time

import httpx
import pytest

from tests.conftest import make_jwt
from wikitcg_bot.models import AuthError, SiteError
from wikitcg_bot.sessions import SessionStore
from wikitcg_bot.sites import wikitcg_net
from wikitcg_bot.sites.wikitcg_net import (
    WikiTcgNet, choose_series, error_code, expiry_note, free_stock, is_challenge, jwt_expiry,
    merge_cookies, parse_cards, regen_text, session_expiry, shown_code,
)

DAY = 86400
FORBIDDEN_PATHS = ("/api/packs/regen", "/api/payments", "/api/shop", "/api/marketplace", "/api/cards/recycle")


def fresh_jwt(days=10):
    return make_jwt(int(time.time()) + days * DAY)


def stored_cookie(name, value, domain="wikitcg.net", expires=-1):
    return {"name": name, "value": value, "domain": domain, "path": "/", "expires": expires,
            "httpOnly": True, "secure": True}


def store_of(settings):
    return SessionStore(settings, "wikitcg_net", ("wikitcg.net",))


def seed(settings, token=None, cf=True):
    """Dépose une session locale : `wtcg_session`, un cookie annexe et, comme dans un vrai export, `cf_clearance`
    (que le magasin de sessions écarte : lié à l'IP et au navigateur d'origine)."""
    token = token or fresh_jwt()
    cookies = [stored_cookie("wtcg_session", token, expires=jwt_expiry(token) or -1),
               stored_cookie("theme", "sombre")]
    if cf:
        cookies.append(stored_cookie("cf_clearance", "cf-opaque", ".wikitcg.net", int(time.time()) + 300 * DAY))
    store_of(settings).save(cookies)
    return token


def saved(settings, name):
    return next((c for c in store_of(settings).load() if c["name"] == name), None)


@pytest.fixture
def sleeps(monkeypatch):
    """Les attentes entre essais ne doivent pas ralentir les tests."""
    waited = []
    monkeypatch.setattr(wikitcg_net.time, "sleep", waited.append)
    return waited


class FakeWikiTcg:
    """Simule le sous-ensemble de l'API utilisé par le bot (formes observées sur le vrai site)."""

    def __init__(self, free=3, paid=0, mystery="mystery_not_eligible", completion=(), renew=None,
                 next_regen=None, override=None, echo_stock=True, reported=None):
        self.free, self.paid, self.mystery, self.completion = free, paid, mystery, list(completion)
        self.renew, self.next_regen, self.override = renew, next_regen, override
        # Le client du site relit /api/packs/status après chaque ouverture : sa réponse d'ouverture ne
        # porte vraisemblablement pas le stock (`echo_stock=False`). `reported` : stock que le statut annonce.
        self.echo_stock, self.reported = echo_stock, reported
        self.opened, self.requests, self.cookie_headers = [], [], []

    def paths(self):
        return [path for _, path, _ in self.requests]

    def posts(self, series_id=None):
        return [body for method, path, body in self.requests
                if method == "POST" and path == "/api/packs/open"
                and (series_id is None or body["seriesId"] == series_id)]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, path, body))
        self.cookie_headers.append(request.headers.get("cookie", ""))
        if self.override and (forced := self.override(request, body)) is not None:
            return forced
        if path == "/api/packs/status":
            headers = {"set-cookie": f"wtcg_session={self.renew}; Path=/; Secure; HttpOnly"} if self.renew else {}
            shown = self.free if self.reported is None else self.reported
            return httpx.Response(200, headers=headers, json={
                "freePacks": shown, "paidPacks": self.paid, "totalAvailable": shown + self.paid,
                "nextRegenAt": self.next_regen or int((time.time() + 7500) * 1000), "maxFreePacks": 5,
                "completionPacks": self.completion, "ink": 70, "level": 2})
        if path == "/api/series":
            return httpx.Response(200, json=[
                {"id": "s1", "name": "Un", "cardCount": 20}, {"id": "s2", "name": "Deux", "cardCount": 20}])
        if path == "/api/collection":
            return httpx.Response(200, json=[{"series_id": "s1", "owned": 20, "total_pulls": 20},
                                             {"series_id": "s2", "owned": 5, "total_pulls": 6}])
        if path == "/api/packs/open-completion":
            pending = [c for c in self.completion if (c.get("id") if isinstance(c, dict) else c) == body.get("completionPackId")]
            if not pending:
                return httpx.Response(404, json={"error": "completion_pack_not_found"})
            self.completion.remove(pending[0])
            return httpx.Response(200, json={"cards": [{"id": "k1", "rarity": "R", "article": {"title": "Louvre"}}]})
        if path == "/api/packs/open":
            if body["seriesId"] == "mystery":
                if self.mystery:
                    return httpx.Response(403, json={"error": self.mystery})
                return httpx.Response(200, json={"cards": [{"id": "m1", "rarity": "SR", "article": {"title": "Loab"}}]})
            if body["seriesId"] not in ("s1", "s2"):
                return httpx.Response(404, json={"error": "series_not_found"})
            if self.free <= 0:
                return httpx.Response(400, json={"error": "no_packs_available"})
            self.free -= 1
            self.opened.append(body["seriesId"])
            opened = {"cards": [{"id": "c1", "rarity": "UR", "article": {"title": "Paris"}},
                                {"id": "c2", "rarity": "C", "article": {"title": "Seine"}}], "xpEarned": 10}
            if self.echo_stock:
                opened.update(totalAvailable=self.free + self.paid, freePacks=self.free)
            return httpx.Response(200, json=opened)
        return httpx.Response(404, json={"error": "not_found"})


def run(settings, api, **env):
    settings.env = {**settings.env, **env}
    return WikiTcgNet(transport=httpx.MockTransport(api)).run(settings)


# -- fonctions pures --------------------------------------------------------------------------
def test_jwt_expiry():
    assert jwt_expiry(make_jwt(1234)) == 1234
    assert jwt_expiry("pas-un-jwt") is None
    assert jwt_expiry("") is None


def test_session_expiry_prefers_the_jwt_then_the_cookie():
    assert session_expiry({"value": make_jwt(1234), "expires": 99}) == 1234
    assert session_expiry({"value": "opaque", "expires": 99}) == 99
    assert session_expiry({"value": "opaque", "expires": -1}) is None


def test_expiry_note_warns_only_when_the_end_is_near():
    now = 1_000_000
    assert expiry_note(None, now) == ""
    assert expiry_note(now + 10 * DAY, now) == ""
    assert "à renouveler" in expiry_note(now + 2 * DAY, now) and "login wikitcg_net" in expiry_note(now + DAY, now)
    assert expiry_note(now - 5, now) == "expirée"


def test_is_challenge_detects_cloudflare_without_false_positives():
    assert is_challenge(httpx.Headers({"cf-mitigated": "challenge"}))
    page = "<html><head><title>Just a moment...</title></head></html>"
    assert is_challenge(httpx.Headers({"content-type": "text/html"}), page)
    # Une vraie page contient le script de détection de Cloudflare sans être un défi.
    normal = '<html><title>WikiTCG</title><script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    assert not is_challenge(httpx.Headers({"content-type": "text/html"}), normal)
    assert not is_challenge(httpx.Headers({"content-type": "application/json"}), '{"error": "unauthorized"}')


def test_error_code():
    assert error_code(httpx.Response(401, json={"error": "unauthorized"})) == "unauthorized"
    assert error_code(httpx.Response(403, text="<html>")) == ""
    assert error_code(httpx.Response(200, json=[1, 2])) == ""


def test_free_stock_never_counts_paid_packs():
    assert free_stock({"freePacks": 2, "paidPacks": 3, "totalAvailable": 5}) == 2
    assert free_stock({"freePacks": 0, "paidPacks": 3, "totalAvailable": 3}) == 0
    assert free_stock({"paidPacks": 1, "totalAvailable": 4}) == 3  # sans `freePacks`
    assert free_stock({"freePacks": -2}) == 0  # compteur négatif : jamais de stock fantôme


def test_free_stock_is_unknown_not_zero_when_the_status_does_not_say():
    for status in ({}, {"error": "maintenance"}, {"data": {"freePacks": 3}}, {"freePacks": None},
                   {"freePacks": True}, {"freePacks": "3"}, {"totalAvailable": 3},  # sans paidPacks : peut-être payants
                   {"totalAvailable": 3, "paidPacks": None}, {"freePacks": float("nan")}):
        assert free_stock(status) is None, status


def test_regen_text():
    now = 1_000_000
    assert regen_text((now + 7500) * 1000, now) == "2h 05m"
    assert regen_text((now + 600) * 1000, now) == "10m"
    assert regen_text((now - 1) * 1000, now) == "" and regen_text(None, now) == ""
    assert regen_text(float("inf"), now) == "" and regen_text("bientôt", now) == ""


def test_choose_series_picks_most_missing():
    series = [{"id": "a", "cardCount": 50}, {"id": "b", "cardCount": 50}, {"id": "c", "cardCount": 10}]
    collection = [{"series_id": "a", "owned": 49}, {"series_id": "b", "owned": 10}]
    assert choose_series(series, collection) == "b"
    assert choose_series(series, collection, pinned="c") == "c"
    with pytest.raises(SiteError):
        choose_series([], [])


def test_choose_series_is_stable_and_robust():
    series = [{"id": "a", "cardCount": 200}, {"id": "b", "cardCount": 200}]
    assert choose_series(series, []) == "a"  # égalité : la première de la liste
    # Tout est complet, ou le compteur du site est en retard : on ouvre quand même quelque chose.
    assert choose_series(series, [{"series_id": "a", "owned": 200}, {"series_id": "b", "owned": 200}]) == "a"
    assert choose_series(series, [{"series_id": "a", "owned": 250}, {"series_id": "b", "owned": 260}]) == "a"
    # Le pack mystère n'est pas une série ordinaire, et les entrées sans identifiant sont ignorées.
    assert choose_series([{"id": "mystery", "cardCount": 999}, {"cardCount": 5}, {"id": "z", "cardCount": 3}], []) == "z"
    with pytest.raises(SiteError):
        choose_series([{"id": "mystery", "cardCount": 50}], [])


def test_parse_cards():
    cards = parse_cards({"cards": [{"id": "c1", "rarity": "UR", "article": {"title": "Paris"}},
                                   {"id": "c2"}, "n'importe quoi"]})
    assert [(c.title, c.rarity) for c in cards] == [("Paris", "UR"), ("c2", "?")]
    assert parse_cards({}) == []


def test_parse_cards_survives_an_unexpected_shape():
    # Le pack est déjà ouvert côté site : une forme inattendue ne doit lever aucune exception.
    assert [(c.title, c.rarity) for c in parse_cards({"cards": [{"rarity": "C", "article": "Paris"}]})] == [("?", "C")]
    assert parse_cards({"cards": 3}) == [] and parse_cards({"cards": {"a": 1}}) == []
    assert parse_cards({"cards": [{"id": "x", "article": None}]})[0].title == "x"


def test_shown_code_only_echoes_short_identifiers():
    assert shown_code("mystery_not_eligible") == "mystery_not_eligible"
    for text in ("Not eligible for user someone@example.org", "", None, 42, "x" * 61):
        assert "someone" not in shown_code(text) and "non reconnu" in shown_code(text)


def test_merge_cookies_keeps_identity_and_updates_the_renewed_value():
    old, new = make_jwt(1000), make_jwt(5000)
    stored = [stored_cookie("wtcg_session", old, expires=1000), stored_cookie("cf_clearance", "cf", ".wikitcg.net", 9999)]
    jar = [{"name": "wtcg_session", "value": new, "domain": "wikitcg.net", "path": "/", "expires": -1},
           {"name": "cf_clearance", "value": "cf", "domain": ".wikitcg.net", "path": "/", "expires": -1}]
    merged, changed = merge_cookies(stored, jar)
    assert changed
    session, cf = merged
    assert session["value"] == new and session["expires"] == 5000  # échéance lue dans le JWT
    assert session["httpOnly"] and session["secure"] and session["domain"] == "wikitcg.net"
    assert cf == stored[1]  # inchangé : on n'écrase pas son échéance avec celle, vide, du pot
    assert stored[0]["value"] == old  # l'entrée d'origine n'est pas modifiée


def test_merge_cookies_reports_no_change_and_adds_new_cookies():
    stored = [stored_cookie("wtcg_session", make_jwt(1000), expires=1000)]
    same = [{"name": "wtcg_session", "value": stored[0]["value"], "domain": "wikitcg.net", "path": "/", "expires": -1}]
    assert merge_cookies(stored, same) == (stored, False)
    extra = same + [{"name": "__cf_bm", "value": "x", "domain": ".wikitcg.net", "path": "/", "expires": 12345}]
    merged, changed = merge_cookies(stored, extra)
    assert changed and [c["name"] for c in merged] == ["wtcg_session", "__cf_bm"]


def test_merge_cookies_prefers_the_longest_lived_duplicate():
    stored = [stored_cookie("wtcg_session", make_jwt(1000), expires=1000)]
    older, newer = make_jwt(2000), make_jwt(3000)
    jar = [{"name": "wtcg_session", "value": newer, "domain": ".wikitcg.net", "path": "/", "expires": -1},
           {"name": "wtcg_session", "value": older, "domain": "wikitcg.net", "path": "/", "expires": -1}]
    merged, _ = merge_cookies(stored, jar)
    assert merged[0]["value"] == newer


# -- session ----------------------------------------------------------------------------------
def test_configured_follows_the_session_store(make_settings):
    settings = make_settings()
    site = WikiTcgNet()
    assert not site.configured(settings)
    seed(settings)
    assert site.configured(settings)
    assert (site.hosts, site.login_url) == (("wikitcg.net",), "https://wikitcg.net/")


def test_session_summary_shows_expiry_and_warns_when_close(make_settings):
    settings = make_settings()
    site = WikiTcgNet()
    assert site.session_summary(settings) == "aucun cookie"
    seed(settings, fresh_jwt(days=10))
    summary = site.session_summary(settings)
    assert "wtcg_session (expire dans" in summary and "cf_clearance" not in summary and "à renouveler" not in summary
    seed(settings, fresh_jwt(days=1))
    assert "à renouveler" in site.session_summary(settings)


def test_session_summary_survives_a_corrupted_state_file(make_settings):
    settings = make_settings()
    store_of(settings).state_path.parent.mkdir(parents=True)
    store_of(settings).state_path.write_text("{pas du json", encoding="utf-8")
    assert "illisible" in WikiTcgNet().session_summary(settings)


def test_session_is_imported_from_a_cookie_export(make_settings):
    settings = make_settings()
    settings.cookies_dir.mkdir(parents=True)
    token = fresh_jwt()
    (settings.cookies_dir / "wikitcg.net_cookies.txt").write_text(
        "# Netscape HTTP Cookie File\n"
        f"#HttpOnly_wikitcg.net\tFALSE\t/\tTRUE\t{jwt_expiry(token)}\twtcg_session\t{token}\n"
        f"#HttpOnly_.wikitcg.net\tTRUE\t/\tTRUE\t{int(time.time()) + 300 * DAY}\tcf_clearance\tcf-opaque\n",
        encoding="utf-8")
    api = FakeWikiTcg(free=0)
    assert WikiTcgNet().configured(settings)
    result = run(settings, api)
    assert result.ok and "wtcg_session=" in api.cookie_headers[0]
    assert store_of(settings).state_path.is_file()  # recopié dans l'état local


def test_missing_session_is_an_auth_error(make_settings):
    with pytest.raises(AuthError, match="login wikitcg_net"):
        run(make_settings(), FakeWikiTcg())


def test_expired_jwt_is_an_auth_error_without_any_request(make_settings):
    settings = make_settings()
    # Le cookie se dit « de session » (donc non périmé) mais le JWT qu'il porte l'est.
    store_of(settings).save([stored_cookie("wtcg_session", make_jwt(int(time.time()) - 10))])
    api = FakeWikiTcg()
    with pytest.raises(AuthError, match="expiré"):
        run(settings, api)
    assert api.requests == []


def test_a_session_close_to_its_end_is_logged(make_settings, caplog):
    settings = make_settings()
    seed(settings, fresh_jwt(days=1))
    with caplog.at_level(logging.WARNING, logger="wikitcg_bot.sites.wikitcg_net"):
        run(settings, FakeWikiTcg(free=0))
    assert any("à renouveler" in r.message for r in caplog.records)


def test_only_the_session_cookie_is_sent_never_the_cloudflare_clearance(make_settings):
    settings = make_settings()
    token = seed(settings)
    api = FakeWikiTcg(free=0)
    run(settings, api)
    # cf_clearance a été délivré au Chrome du propriétaire : le rejouer depuis ce client serait
    # emprunter un laissez-passer Cloudflare qui n'est pas le sien. Le magasin ne le garde même plus.
    assert all(h == f"wtcg_session={token}" for h in api.cookie_headers)
    assert saved(settings, "cf_clearance") is None


# -- renouvellement du cookie -----------------------------------------------------------------
def test_renewed_cookie_is_persisted_and_other_cookies_are_left_alone(make_settings):
    settings = make_settings()
    seed(settings)
    renewed = make_jwt(int(time.time()) + 14 * DAY)
    api = FakeWikiTcg(free=0, renew=renewed)
    run(settings, api)
    session = saved(settings, "wtcg_session")
    assert session["value"] == renewed and session["expires"] == jwt_expiry(renewed)
    assert session["domain"] == "wikitcg.net" and session["httpOnly"] and session["secure"]
    assert saved(settings, "theme")["value"] == "sombre"  # les autres cookies restent intacts
    # Les requêtes suivantes utilisent déjà le nouveau cookie.
    assert api.cookie_headers[0].count("wtcg_session=") == 1


def test_renewed_cookie_is_persisted_even_when_a_later_step_fails(make_settings, sleeps):
    settings = make_settings()
    seed(settings)
    renewed = make_jwt(int(time.time()) + 14 * DAY)
    api = FakeWikiTcg(free=2, renew=renewed,
                      override=lambda req, body: httpx.Response(500) if req.url.path == "/api/series" else None)
    with pytest.raises(SiteError):
        run(settings, api)
    assert saved(settings, "wtcg_session")["value"] == renewed


def test_unchanged_session_does_not_rewrite_the_state(make_settings):
    settings = make_settings()
    seed(settings)
    path = store_of(settings).state_path
    # Le contenu écrit serait identique (même secondes, mêmes cookies) : seule la date du fichier trahit une réécriture.
    os.utime(path, (1, 1))
    run(settings, FakeWikiTcg(free=0))
    assert path.stat().st_mtime == 1


# -- ouverture des packs ----------------------------------------------------------------------
def test_opens_all_free_packs_on_the_series_with_most_missing_cards(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=3)
    result = run(settings, api)
    assert result.opened == 3 and result.ok and not result.auth_failed
    assert api.opened == ["s2", "s2", "s2"]
    assert result.packs_before == 3 and result.packs_max == 5
    assert [c.title for c in result.cards] == ["Paris", "Seine"] * 3
    assert [c.rarity for c in result.cards][:2] == ["UR", "C"]
    assert "Tous les packs gratuits" in result.message
    assert api.paths().count("/api/series") == 1  # choisie une fois par run


def test_respects_max_packs(make_settings):
    settings = make_settings()
    seed(settings)
    settings.max_packs = 2
    api = FakeWikiTcg(free=5)
    result = run(settings, api)
    assert result.opened == 2 and "Limite de 2" in result.message and "il en reste 3" in result.message
    assert api.free == 3


def test_a_pinned_series_skips_the_series_lookups(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=2)
    result = run(settings, api, WIKITCG_NET_SERIES="s1")
    assert api.opened == ["s1", "s1"] and result.opened == 2
    assert "/api/series" not in api.paths() and "/api/collection" not in api.paths()


def test_no_free_pack_available(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=0)
    result = run(settings, api)
    assert result.ok and result.opened == 0 and result.packs_before == 0
    assert "Aucun pack gratuit" in result.message and "prochain dans 2h" in result.message
    assert "/api/series" not in api.paths()


def status_only(body):
    """Impose le corps de /api/packs/status ; le reste est servi par le faux site."""
    return lambda req, _body: httpx.Response(200, json=body) if req.url.path == "/api/packs/status" else None


@pytest.mark.parametrize("body", [
    {}, {"error": "maintenance"}, {"data": {"freePacks": 3}}, {"free": 3, "total": 3},
    {"freePacks": None, "nextRegenAt": 1}, {"totalAvailable": 3},
])
def test_a_status_without_a_readable_stock_is_a_failure_not_an_empty_stock(make_settings, body):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=3, override=status_only(body))
    # Sinon le run passerait pour un succès à « 0 pack », le planning attendrait des heures et le stock
    # (plafonné à 5) se perdrait en silence.
    with pytest.raises(SiteError, match="packs/status"):
        run(settings, api)
    assert api.posts() == [] and api.free == 3


def test_a_status_with_a_zero_stock_is_a_normal_empty_stock(make_settings):
    settings = make_settings()
    seed(settings)
    result = run(settings, FakeWikiTcg(override=status_only({"freePacks": 0})))
    assert result.ok and result.packs_before == 0 and result.packs_max is None
    assert "Aucun pack gratuit" in result.message


def test_a_malformed_max_stock_does_not_reach_the_scheduler(make_settings):
    settings = make_settings()
    seed(settings)
    body = {"freePacks": 2, "maxFreePacks": "5", "nextRegenAt": 1}
    assert run(settings, FakeWikiTcg(free=2, override=status_only(body))).packs_max is None  # plafond par défaut
    settings.dry_run = True  # la simulation compare le stock au plafond : une chaîne y ferait lever TypeError
    assert run(settings, FakeWikiTcg(free=2, override=status_only(body))).ok


# La réponse d'ouverture du site ne porte vraisemblablement pas le stock (son client relit
# /api/packs/status) : le décompte local doit tenir seul.
@pytest.mark.parametrize("stock", [1, 3])
def test_without_stock_in_the_open_response_exactly_the_announced_packs_are_opened(make_settings, stock):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=stock, echo_stock=False)
    result = run(settings, api)
    assert len(api.posts("s2")) == stock and result.opened == stock and api.free == 0
    assert result.ok and "Tous les packs gratuits" in result.message


def test_without_stock_in_the_open_response_max_packs_is_honoured(make_settings):
    settings = make_settings()
    seed(settings)
    settings.max_packs = 2
    api = FakeWikiTcg(free=5, echo_stock=False)
    result = run(settings, api)
    assert len(api.posts("s2")) == 2 and result.opened == 2
    assert "Limite de 2" in result.message and "il en reste 3" in result.message


def test_without_stock_in_the_open_response_a_stale_status_ends_on_the_first_refusal(make_settings):
    settings = make_settings()
    seed(settings)
    # Le statut annonce 3 packs mais il n'en reste qu'un (ouvert entre-temps depuis le site, par exemple).
    api = FakeWikiTcg(free=1, reported=3, echo_stock=False)
    result = run(settings, api)
    assert result.ok and result.opened == 1 and len(api.posts("s2")) == 2
    assert "refusée" in result.message and "no_packs_available" in result.message


def test_a_refused_opening_stops_the_loop_without_failing_the_run(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=3)
    served = api.__call__

    def stale_stock(request):  # le site refuse la 2e ouverture alors que le statut en promettait 3
        if request.url.path == "/api/packs/open" and len(api.opened) >= 1 and b"mystery" not in request.content:
            return httpx.Response(400, json={"error": "no_packs_available"})
        return served(request)
    result = run(settings, stale_stock)
    assert result.ok and result.opened == 1
    assert "refusée" in result.message and "no_packs_available" in result.message


def not_mystery(response):
    """Réponse imposée aux seules ouvertures ordinaires, le pack mystère restant servi par le faux site."""
    return lambda req, body: (response if req.url.path == "/api/packs/open" and body["seriesId"] != "mystery" else None)


def test_a_refusal_before_any_opening_is_a_failure_not_a_green_run(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=5)
    # Une faute de frappe dans WIKITCG_NET_SERIES : le site répond 404 `series_not_found` à chaque heure.
    with pytest.raises(SiteError, match="series_not_found") as caught:
        run(settings, api, WIKITCG_NET_SERIES="nope")
    assert "5 pack(s) gratuit(s) étaient annoncés" in str(caught.value)
    assert [b["seriesId"] for b in api.posts()] == ["mystery", "nope"]  # une seule tentative, rien n'est répété


def test_a_refusal_sent_with_a_200_status_before_any_opening_is_a_failure_too(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=2, override=not_mystery(httpx.Response(200, json={"error": "no_packs_available"})))
    with pytest.raises(SiteError, match="no_packs_available"):
        run(settings, api)
    assert len(api.posts("s2")) == 1


def test_a_stale_stock_refusal_after_the_mystery_pack_is_not_a_failure(make_settings):
    settings = make_settings()
    seed(settings)
    # Le pack mystère a peut-être entamé le stock que le statut annonçait : le refus qui suit est normal.
    api = FakeWikiTcg(free=1, mystery=None, override=not_mystery(httpx.Response(400, json={"error": "no_packs_available"})))
    result = run(settings, api)
    assert result.ok and result.opened == 1 and result.cards[0].title == "Loab"
    assert "refusée" in result.message and "no_packs_available" in result.message


def test_a_2xx_without_cards_is_an_unexpected_response_never_replayed(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=3, override=not_mystery(httpx.Response(200, json={"success": True, "pulls": []})))
    with pytest.raises(SiteError, match="inattendue"):
        run(settings, api)
    assert len(api.posts("s2")) == 1  # le pack a peut-être été ouvert : on ne redemande pas


def test_paid_packs_are_left_alone_and_reported(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=0, paid=2)
    result = run(settings, api)
    assert result.opened == 0 and api.posts("s1") == [] and api.posts("s2") == []
    assert result.packs_before == 0 and "2 pack(s) acquis non ouvert(s)" in result.message


def test_completion_packs_are_opened_as_authorized_by_the_owner(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=0, completion=[{"id": "cp1", "seriesId": "s1"}, "cp2"])
    result = run(settings, api)
    assert result.ok and result.opened == 2 and [c.title for c in result.cards] == ["Louvre", "Louvre"]
    assert api.completion == []
    posted = [b for m, path, b in api.requests if path == "/api/packs/open-completion"]
    assert posted == [{"completionPackId": "cp1"}, {"completionPackId": "cp2"}]


def test_a_refused_completion_pack_does_not_stop_the_free_packs(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=1, completion=[{"id": "fantome"}])
    api.completion = [{"id": "fantome"}]
    original = api.__call__

    def refuse(request, body):
        if request.url.path == "/api/packs/open-completion":
            return httpx.Response(403, json={"error": "completion_not_ready"})
    api.override = refuse
    result = run(settings, api)
    assert result.ok and result.opened == 1 and api.opened == ["s2"]


def test_completion_ids_accepts_ids_or_objects_and_ignores_the_rest():
    from wikitcg_bot.sites.wikitcg_net import completion_ids
    status = {"completionPacks": ["a", {"id": "b"}, {"completionPackId": 7}, {"x": 1}, None, True, ""]}
    assert completion_ids(status) == ["a", "b", "7"]
    assert completion_ids({}) == [] and completion_ids({"completionPacks": "oops"}) == []


def test_the_bot_never_calls_a_spending_endpoint(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=2, paid=1, completion=[{"id": "cp1"}], mystery=None)
    run(settings, api)
    assert set(api.paths()) <= {"/api/packs/status", "/api/series", "/api/collection", "/api/packs/open",
                               "/api/packs/open-completion"}
    assert not [p for p in api.paths() if p.startswith(FORBIDDEN_PATHS)]


def test_dry_run_sends_no_post_at_all(make_settings):
    settings = make_settings()
    seed(settings)
    settings.dry_run = True
    api = FakeWikiTcg(free=3)
    result = run(settings, api)
    assert result.opened == 0 and api.free == 3
    assert api.paths() == ["/api/packs/status"]
    assert "Simulation : 3 pack(s) gratuit(s)" in result.message and "Prochain pack gratuit dans" in result.message
    assert "Pack mystère à tenter" in result.message
    assert not (settings.state_dir / "wikitcg_net_mystery.json").exists()
    settings.env = {"WIKITCG_NET_MYSTERY": "0"}
    assert "mystère" not in run(settings, FakeWikiTcg(free=3)).message


# -- pack mystère -----------------------------------------------------------------------------
def test_mystery_refusal_is_normal_and_is_asked_again_next_run(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=1, mystery="mystery_not_eligible")
    result = run(settings, api)
    assert result.ok and result.opened == 1  # le 403 du mystère n'est ni une session refusée, ni une erreur
    assert api.opened == ["s2"] and len(api.posts("mystery")) == 1
    run(settings, api)
    assert len(api.posts("mystery")) == 2  # aucun délai n'est mémorisé tant que le site refuse


def test_mystery_pack_is_opened_then_not_asked_again_for_six_hours(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=1, mystery=None)
    result = run(settings, api)
    assert result.opened == 2 and [c.title for c in result.cards][0] == "Loab"
    assert result.cards[0].rarity == "SR"
    not_before = json.loads((settings.state_dir / "wikitcg_net_mystery.json").read_text())["not_before"]
    assert abs(not_before - (time.time() + 6 * 3600)) < 60
    run(settings, FakeWikiTcg(free=0, mystery=None))
    api2 = FakeWikiTcg(free=0, mystery=None)
    run(settings, api2)
    assert api2.posts("mystery") == []


def test_mystery_is_asked_again_once_the_delay_is_over(make_settings):
    settings = make_settings()
    seed(settings)
    path = settings.state_dir / "wikitcg_net_mystery.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"not_before": time.time() - 1}))
    api = FakeWikiTcg(free=0, mystery=None)
    assert run(settings, api).opened == 1 and len(api.posts("mystery")) == 1


def test_the_mystery_pack_is_kept_even_if_its_delay_cannot_be_saved(make_settings):
    settings = make_settings()
    seed(settings)
    # Un dossier à la place du fichier : l'écriture échoue comme sous un antivirus qui le verrouille.
    (settings.state_dir / "wikitcg_net_mystery.json").mkdir(parents=True)
    result = run(settings, FakeWikiTcg(free=1, mystery=None))
    assert result.ok and result.opened == 2 and result.cards[0].title == "Loab"


@pytest.mark.parametrize("value", ["0", "false", "False", " no ", "non", "OFF"])
def test_mystery_can_be_disabled(make_settings, value):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=1, mystery=None)
    run(settings, api, WIKITCG_NET_MYSTERY=value)
    assert api.posts("mystery") == []


def test_mystery_stays_enabled_by_default_and_with_any_other_value(make_settings):
    for value in ("", "1", "oui"):
        settings = make_settings()
        seed(settings)
        api = FakeWikiTcg(free=0)
        run(settings, api, WIKITCG_NET_MYSTERY=value)
        assert len(api.posts("mystery")) == 1, value


def test_a_corrupted_mystery_state_file_is_ignored(make_settings):
    settings = make_settings()
    seed(settings)
    path = settings.state_dir / "wikitcg_net_mystery.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("n'importe quoi")
    api = FakeWikiTcg(free=0)
    run(settings, api)
    assert len(api.posts("mystery")) == 1


# -- demandes de vérification ----------------------------------------------------------------
CHECK_CODES = ["human_verification_required", "captcha_required", "rate_limited", "too_many_requests",
               "turnstile_failed", "RateLimitExceeded"]


def test_benign_refusal_codes_are_not_taken_for_a_verification():
    for code in ("mystery_not_eligible", "no_packs_available", "series_not_found", "insufficient_ink", "already_full"):
        assert not wikitcg_net.is_check_code(code), code
    for code in CHECK_CODES:
        assert wikitcg_net.is_check_code(code), code


@pytest.mark.parametrize("code", CHECK_CODES)
def test_a_verification_demand_on_the_mystery_request_stops_the_site(make_settings, code):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=3, mystery=code)
    with pytest.raises(SiteError, match="vérification") as caught:
        run(settings, api)
    assert not isinstance(caught.value, AuthError) and code in str(caught.value)
    assert [b["seriesId"] for b in api.posts()] == ["mystery"]  # aucune autre ouverture n'est tentée
    assert "/api/series" not in api.paths() and api.free == 3


def test_a_verification_demand_sent_with_a_200_status_stops_the_site_too(make_settings):
    settings = make_settings()
    seed(settings)
    ask = httpx.Response(200, json={"error": "human_verification_required"})
    api = FakeWikiTcg(free=3, override=lambda req, body: ask if body and body.get("seriesId") == "mystery" else None)
    with pytest.raises(SiteError, match="vérification"):
        run(settings, api)
    assert [b["seriesId"] for b in api.posts()] == ["mystery"]


def test_a_verification_demand_after_some_openings_is_a_failure_not_a_green_run(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=5)
    served = api.__call__
    asked = []

    def asks_after_one_pack(request):
        if request.url.path == "/api/packs/open" and len(api.opened) == 1 and b"mystery" not in request.content:
            asked.append(request)
            return httpx.Response(403, json={"error": "human_verification_required"})
        return served(request)
    result = run(settings, asks_after_one_pack)
    assert not result.ok and not result.auth_failed and result.opened == 1 and len(result.cards) == 2
    assert "vérification" in result.message
    assert len(asked) == 1 and len(api.posts("s2")) == 1  # une seule demande refusée : on n'insiste pas


# -- erreurs ----------------------------------------------------------------------------------
def test_rejected_cookie_is_an_auth_error(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(override=lambda req, body: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(AuthError, match="wtcg_session"):
        run(settings, api)
    assert api.paths() == ["/api/packs/status"]


def test_a_403_that_says_unauthorized_is_an_auth_error(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(override=lambda req, body: httpx.Response(403, json={"error": "unauthorized"}))
    with pytest.raises(AuthError):
        run(settings, api)


def test_cloudflare_challenge_stops_everything_and_is_not_an_auth_error(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(override=lambda req, body: httpx.Response(
        403, headers={"cf-mitigated": "challenge", "content-type": "text/html"}, text="<html>Just a moment...</html>"))
    with pytest.raises(SiteError, match="Cloudflare") as caught:
        run(settings, api)
    assert not isinstance(caught.value, AuthError) and "contourne pas" in str(caught.value)
    assert len(api.requests) == 1  # aucun nouvel essai, aucun contournement


def test_cloudflare_challenge_page_without_the_header_is_detected_too(make_settings):
    settings = make_settings()
    seed(settings)
    page = "<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>_cf_chl_opt</body></html>"
    api = FakeWikiTcg(override=lambda req, body: httpx.Response(
        503, headers={"content-type": "text/html; charset=UTF-8"}, text=page))
    with pytest.raises(SiteError, match="Cloudflare"):
        run(settings, api)
    assert len(api.requests) == 1


def test_a_challenge_during_the_mystery_attempt_is_not_swallowed(make_settings):
    settings = make_settings()
    seed(settings)
    block = httpx.Response(403, headers={"cf-mitigated": "challenge"}, text="")
    api = FakeWikiTcg(free=2, override=lambda req, body: block if body and body.get("seriesId") == "mystery" else None)
    with pytest.raises(SiteError, match="Cloudflare"):
        run(settings, api)
    assert api.opened == []


def test_a_non_json_403_is_a_site_error_not_an_auth_error(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(override=lambda req, body: httpx.Response(
        403, headers={"content-type": "text/html"}, text="<html>Access denied</html>"))
    with pytest.raises(SiteError, match="403") as caught:
        run(settings, api)
    assert not isinstance(caught.value, AuthError)


BLOCKED = httpx.Response(403, headers={"content-type": "text/html"}, text="<html>Attention Required! | Cloudflare</html>")


def test_a_blocked_ordinary_opening_stops_the_site_instead_of_looking_refused(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=3, override=not_mystery(BLOCKED))
    with pytest.raises(SiteError, match="403") as caught:
        run(settings, api)
    assert not isinstance(caught.value, AuthError) and "Cloudflare" in str(caught.value)
    assert [b["seriesId"] for b in api.posts()] == ["mystery", "s2"]


def test_a_blocked_mystery_request_stops_the_site_before_any_other_post(make_settings):
    settings = make_settings()
    seed(settings)
    only_mystery = lambda req, body: BLOCKED if body and body.get("seriesId") == "mystery" else None
    api = FakeWikiTcg(free=3, override=only_mystery)
    with pytest.raises(SiteError, match="403"):
        run(settings, api)
    assert [b["seriesId"] for b in api.posts()] == ["mystery"]  # rien d'autre n'est envoyé au même serveur


def test_a_rate_limit_on_an_opening_is_a_failure_and_the_opening_is_not_replayed(make_settings, sleeps):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=3, override=not_mystery(httpx.Response(429, headers={"retry-after": "2"})))
    with pytest.raises(SiteError, match="429"):
        run(settings, api)
    assert len(api.posts("s2")) == 1 and sleeps == []  # le site demande de ralentir : on s'arrête


def test_a_rate_limit_on_the_mystery_request_is_never_replayed(make_settings, sleeps):
    settings = make_settings()
    seed(settings)
    limited = httpx.Response(429, headers={"retry-after": "1"})
    api = FakeWikiTcg(free=3, override=lambda req, body: limited if body and body.get("seriesId") == "mystery" else None)
    with pytest.raises(SiteError, match="429"):
        run(settings, api)
    assert [b["seriesId"] for b in api.posts()] == ["mystery"] and sleeps == []


@pytest.mark.parametrize("headers", [{"retry-after": "900"}, {}, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}])
def test_a_rate_limit_asking_for_a_long_or_unknown_wait_is_not_cut_short(make_settings, sleeps, headers):
    settings = make_settings()
    seed(settings)
    # Attendre 60 s puis réessayer alors que le site en demande 900, ce serait ignorer sa demande.
    api = FakeWikiTcg(free=3, override=lambda req, body: httpx.Response(429, headers=headers))
    with pytest.raises(SiteError, match="429"):
        run(settings, api)
    assert len(api.requests) == 1 and sleeps == []


def test_error_bodies_are_never_copied_into_messages_or_logs(make_settings, caplog):
    settings = make_settings()
    seed(settings)
    leak = "bad request for someone@example.org"
    # Pas de code d'erreur du site : le corps n'est pas recopié.
    api = FakeWikiTcg(free=2, override=not_mystery(httpx.Response(422, json={"message": leak})))
    with pytest.raises(SiteError) as caught:
        run(settings, api)
    assert "someone@example.org" not in str(caught.value) and "422" in str(caught.value)
    # Un « code » qui est une phrase n'est pas recopié non plus, même quand le refus est toléré (mystère).
    with caplog.at_level(logging.INFO, logger="wikitcg_bot.sites.wikitcg_net"):
        result = run(settings, FakeWikiTcg(free=0, mystery=leak))
    assert result.ok and "mystère" in caplog.text and "someone@example.org" not in caplog.text


def test_an_unexpected_exception_after_an_opening_keeps_the_history(make_settings, monkeypatch):
    settings = make_settings()
    seed(settings)

    def broken_pause(_settings):
        raise OSError("disque verrouillé")
    monkeypatch.setattr(wikitcg_net, "pause", broken_pause)
    result = run(settings, FakeWikiTcg(free=3))
    assert not result.ok and not result.auth_failed and result.opened == 1 and len(result.cards) == 2
    assert "Interrompu après 1 pack" in result.message and "OSError" in result.message


def test_a_card_with_an_unexpected_shape_does_not_lose_the_opened_pack(make_settings):
    settings = make_settings()
    seed(settings)
    weird = httpx.Response(200, json={"cards": [{"rarity": "C", "article": "Paris"}], "freePacks": 0})
    result = run(settings, FakeWikiTcg(free=1, override=not_mystery(weird)))
    assert result.ok and result.opened == 1 and [(c.title, c.rarity) for c in result.cards] == [("?", "C")]


def test_failure_after_some_openings_keeps_what_was_opened(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=3)
    served = api.__call__

    def breaks_on_second_pack(request):
        if request.url.path == "/api/packs/open" and len(api.opened) == 1:
            return httpx.Response(500, json={"error": "boom"})
        return served(request)
    result = run(settings, breaks_on_second_pack)
    assert not result.ok and result.opened == 1 and len(result.cards) == 2
    assert "Interrompu après 1 pack" in result.message and not result.auth_failed


def test_session_lost_after_some_openings_is_flagged_for_renewal(make_settings):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=3)
    served = api.__call__

    def expires_midway(request):
        if request.url.path == "/api/packs/open" and len(api.opened) == 1:
            return httpx.Response(401, json={"error": "unauthorized"})
        return served(request)
    result = run(settings, expires_midway)
    assert result.opened == 1 and not result.ok and result.auth_failed


def test_failure_before_any_opening_raises(make_settings, sleeps):
    settings = make_settings()
    seed(settings)
    with pytest.raises(SiteError, match="500"):
        run(settings, FakeWikiTcg(override=lambda req, body: httpx.Response(500)))


def test_reads_are_retried_on_server_errors_with_backoff(make_settings, sleeps):
    settings = make_settings()
    seed(settings)
    calls = {"n": 0}

    def flaky_status(request, body):
        if request.url.path == "/api/packs/status":
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(502)
        return None
    result = run(settings, FakeWikiTcg(free=0, override=flaky_status))
    assert result.ok and calls["n"] == 3 and sleeps == [2, 4]


def test_rate_limit_honours_retry_after(make_settings, sleeps):
    settings = make_settings()
    seed(settings)
    calls = {"n": 0}

    def limited(request, body):
        if request.url.path == "/api/packs/status":
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "7"})
        return None
    assert run(settings, FakeWikiTcg(free=0, override=limited)).ok
    assert sleeps == [7]


def test_an_opening_is_never_replayed_after_a_server_error(make_settings, sleeps):
    settings = make_settings()
    seed(settings)
    api = FakeWikiTcg(free=2, mystery=None, override=lambda req, body: (
        httpx.Response(500) if req.url.path == "/api/packs/open" else None))
    with pytest.raises(SiteError):
        run(settings, api)
    # Un 5xx peut venir après l'ouverture : ni le mystère ni la série ordinaire ne sont rejoués.
    assert len(api.posts("mystery")) == 1 and len(api.posts("s2")) == 1 and sleeps == []


def test_network_errors_are_retried_for_reads_but_not_for_openings(make_settings, sleeps):
    settings = make_settings()
    seed(settings)

    def down(request):
        raise httpx.ReadTimeout("lent", request=request)
    with pytest.raises(SiteError, match="Réseau indisponible"):
        run(settings, down)
    assert sleeps == [2, 4]

    sleeps.clear()
    served = FakeWikiTcg(free=1)

    def loses_the_open_response(request):
        if request.url.path == "/api/packs/open":
            raise httpx.ReadTimeout("lent", request=request)
        return served(request)
    with pytest.raises(SiteError, match="Réseau indisponible"):
        run(settings, loses_the_open_response)
    assert sleeps == []
