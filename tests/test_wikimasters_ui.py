"""WikiMasters : analyse des réponses (sans navigateur) et pilotage du vrai Chrome contre un faux site.

Le faux site reproduit ce que l'on sait du vrai (docs/exploration.md) : `/pulls` redirige vers `/login`
sans session valide, affiche « N / 10 paquets disponibles », `POST /api/packs/open` répond 401 sans
session et `human_verification_required` quand un Turnstile est exigé, `/api/packs/special` est celui
déduit du JavaScript du site. Aucune valeur ici n'est réelle.
"""
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tests.conftest import requires_browser
from wikitcg_bot.models import AuthError, SiteError
from wikitcg_bot.sessions import SessionStore
from wikitcg_bot.sites import wikimasters
from wikitcg_bot.sites.base import session_store
from wikitcg_bot.sites.wikimasters import (
    ChallengeDetected,
    HumanVerificationRequired,
    Reply,
    StopRun,
    WikiMasters,
    _check,
    as_reply,
    call,
    parse_card,
    parse_special,
    parse_stock,
    special_offered,
    stock_synced,
    wait_text,
)

COOKIE = "sb-fake-auth-token.0"
# Tout ce que le bot n'a pas le droit d'appeler (achat, PRO, « grâce », facturation, Turnstile).
FORBIDDEN = ("/api/packs/pro-daily", "/api/packs/grace", "/api/checkout", "/api/billing/portal",
             "/api/human-check")

LOGIN_PAGE = """<html><head><title>Connexion | WikiMasters</title></head><body>
<input type="email"><input type="password"><button>Connexion</button></body></html>"""

# Rend le DOM du vrai site : un spinner, puis le bouton « Ouvrir » et le bloc « N / 10 paquets
# disponibles ». À 0 paquet, le vrai site propose d'acheter une recharge : le bouton est là pour qu'un
# clic malheureux (il n'y en a aucun) appelle /api/checkout et se voie dans les appels enregistrés.
PULLS_PAGE = """<html><head><meta charset="utf-8"></head><body>
<div id="app"><div class="spinner"></div></div><script>
%(client_redirect)s
fetch('/api/state').then(r => r.json()).then(s => setTimeout(() => {
  document.getElementById('app').innerHTML =
    (s.suspended ? '<p>Compte suspendu</p>' : '') +
    '<button ' + (s.available <= 0 ? 'disabled' : '') + '>Ouvrir</button>' +
    '<div><span>' + s.available + '</span><span> / ' + s.max + '</span></div>' +
    '<div>paquets disponibles</div>' +
    (s.available < s.max && !s.unsynced ? '<div>Prochain dans <span>' + s.next + '</span></div>' : '') +
    (s.available <= 0 ? '<button onclick="fetch(\\'/api/checkout\\', {method: \\'POST\\'})">' +
                        'Rechargez 10 paquets à 1,99 $ CAD</button>' : '') +
    (s.special ? '<h2>Packs Spéciaux</h2><span>Gratuit · 1 / 6h</span>' : '');
}, 150));
</script></body></html>"""


def fake_cards(prefix, count=5):
    """Cinq cartes, la dernière imbriquée sous `card` comme certains endpoints le font."""
    cards = [{"wikipedia_title": f"{prefix} {i}", "rarity": "sr"} for i in range(count - 1)]
    return cards + [{"card": {"wikipedia_title": f"{prefix} imbriquée", "rarity": "C"}}]


class FakeWikiMasters:
    def __init__(self):
        self.session_value = "good"      # valeur du cookie que le faux site accepte
        self.renew_to = None             # nouvelle valeur posée à la visite de /pulls (jeton tournant)
        self.packs = 3
        self.redirect = "server"         # sans session : 307 côté serveur, ou JavaScript côté client
        self.challenge = False           # /pulls répond par un défi Cloudflare
        self.api_challenge_after = None  # un défi Vercel frappe l'API après N ouvertures
        self.suspended = False
        self.human_after = None          # exige un Turnstile après N ouvertures
        self.revoke_after = None         # la session est refusée après N ouvertures
        self.special_enabled = True      # sinon l'API des packs spéciaux répond 404, que la page les affiche ou non
        self.special_in_ui = True        # /pulls affiche la section « Packs Spéciaux »
        self.special_available = True
        self.unsynced = False            # /pulls affiche « 0 / 10 » sans avoir lu le profil
        self.refuse_open = False         # l'ouverture est refusée par un 403 ordinaire
        self.drop_after = None           # la connexion est coupée à l'ouverture suivante, après N ouvertures
        self.hang_after = None           # l'ouverture suivante, après N ouvertures, ne reçoit jamais de réponse
        self.hold = threading.Event()    # libère les requêtes retenues (fin du test)
        self.special_opened = []
        self.opened = 0
        self.calls = []                  # (méthode, chemin) de chaque requête reçue

    def api_calls(self):
        return [c for c in self.calls if c[1].startswith("/api/")]

    def handler(self):
        site = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, ctype="application/json", headers=()):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for key, value in headers:
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

            def _json(self, status, obj):
                self._send(status, json.dumps(obj))

            @property
            def logged_in(self):
                return f"{COOKIE}={site.session_value}" in (self.headers.get("Cookie") or "")

            def do_GET(self):
                path = self.path.split("?")[0]
                site.calls.append(("GET", path))
                if path == "/login":
                    self._send(200, LOGIN_PAGE, "text/html")
                elif path == "/pulls":
                    self._pulls()
                elif path == "/api/state":
                    self._json(200, {"available": 0 if site.unsynced else site.packs, "max": 10,
                                     "next": "4:37", "suspended": site.suspended,
                                     "unsynced": site.unsynced,
                                     "special": site.special_in_ui})
                elif path == "/api/packs/special":
                    self._special_state()
                else:
                    self._send(404, "{}")

            def _pulls(self):
                if site.challenge:
                    self._send(403, "<html>Just a moment...</html>", "text/html",
                               [("cf-mitigated", "challenge")])
                elif not self.logged_in:
                    if site.redirect == "server":
                        self._send(307, "", headers=[("Location", "/login")])
                    else:
                        self._send(200, PULLS_PAGE % {"client_redirect": "location.href = '/login';"},
                                   "text/html")
                else:
                    headers = []
                    if site.renew_to:  # le jeton de rafraîchissement tourne à chaque visite
                        site.session_value = site.renew_to
                        headers.append(("Set-Cookie", f"{COOKIE}={site.renew_to}; Path=/; Max-Age=3456000"))
                    self._send(200, PULLS_PAGE % {"client_redirect": ""}, "text/html", headers)

            def _special_state(self):
                if not self.logged_in:
                    self._json(401, {"error": "Non autorisé"})
                elif not site.special_enabled:
                    self._send(404, "<html>404</html>", "text/html")
                else:
                    in_six_hours = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).isoformat()
                    self._json(200, {
                        "packs": [{"id": "animaux", "name": "Animaux", "description": "…"},
                                  {"id": "disney", "name": "Disney", "description": "…"}],
                        "available": site.special_available, "is_vip": False,
                        "next_available_at": None if site.special_available else in_six_hours,
                    })

            def do_POST(self):
                path = self.path.split("?")[0]
                site.calls.append(("POST", path))
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length)) if length else {}
                if path == "/api/packs/open":
                    self._open()
                elif path == "/api/packs/special":
                    self._open_special(payload)
                else:
                    self._json(404, {})

            def _open(self):
                if not self.logged_in or (site.revoke_after is not None
                                          and site.opened >= site.revoke_after):
                    self._json(401, {"error": "Non autorisé"})
                elif site.drop_after is not None and site.opened >= site.drop_after:
                    self.close_connection = True  # rien n'est envoyé : fetch échoue (« Failed to fetch »)
                elif site.hang_after is not None and site.opened >= site.hang_after:
                    site.hold.wait(30)            # la requête reste en suspens jusqu'à la fin du test
                    self.close_connection = True
                elif site.refuse_open:
                    self._json(403, {"error": "Compte bloqué"})
                elif site.api_challenge_after is not None and site.opened >= site.api_challenge_after:
                    self._send(429, "<html>Vercel Security Checkpoint</html>", "text/html",
                               [("x-vercel-mitigated", "challenge")])
                elif site.human_after is not None and site.opened >= site.human_after:
                    self._json(403, {"error": "Vérification requise",
                                     "human_verification_required": True})
                elif site.packs <= 0:
                    self._json(403, {"error": "Aucun paquet disponible", "packs_remaining": 0})
                else:
                    site.packs -= 1
                    site.opened += 1
                    self._json(200, {"cards": fake_cards(f"Carte {site.opened}"),
                                     "packs_remaining": site.packs,
                                     "packs_last_regen_at": "2026-10-20T12:00:00+00:00",
                                     "owned_copies": {}})

            def _open_special(self, payload):
                if not self.logged_in:
                    self._json(401, {"error": "Non autorisé"})
                elif not site.special_enabled:
                    self._send(404, "<html>404</html>", "text/html")
                elif not site.special_available:
                    self._json(429, {"error": "Pas encore", "next_available_at": "2099-01-01T00:00:00Z"})
                else:
                    site.special_available = False
                    site.special_opened.append(payload.get("packId"))
                    self._json(200, {"cards": fake_cards("Spéciale"),
                                     "next_available_at": "2099-01-01T00:00:00Z"})

        return Handler


@pytest.fixture
def fake(monkeypatch):
    site = FakeWikiMasters()
    server = ThreadingHTTPServer(("127.0.0.1", 0), site.handler())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(wikimasters, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    # Le faux site affiche tout d'un coup : inutile d'attendre des secondes ce qui ne viendra pas.
    monkeypatch.setattr(wikimasters, "SPECIAL_WAIT_S", 0.6)
    yield site
    site.hold.set()
    server.shutdown()
    server.server_close()


def seed_cookie(settings, name=COOKIE, value="good", host="127.0.0.1", days=400):
    """Dépose un cookie de session synthétique dans l'état de session du bot (comme `login`)."""
    SessionStore(settings, "wikimasters", (host,), import_export=False).save([{
        "name": name, "value": value, "domain": host, "path": "/", "secure": False, "httpOnly": False,
        "expires": int(time.time()) + days * 86400,
    }])


def site_on_fake(settings, value="good"):
    """Le site WikiMasters, mais dont l'hôte est celui du faux serveur, avec une session déjà ouverte."""
    site = WikiMasters()
    site.hosts = ("127.0.0.1",)
    seed_cookie(settings, value=value)
    return site


def posts(fake):
    return [path for method, path in fake.api_calls() if method == "POST"]


def assert_nothing_forbidden(fake):
    assert not [c for c in fake.calls if c[1] in FORBIDDEN]
    assert set(posts(fake)) <= {"/api/packs/open", "/api/packs/special"}


# -- analyse des réponses, sans navigateur ----------------------------------------------------------


def test_parse_card_handles_flat_and_nested():
    assert parse_card({"wikipedia_title": "A", "rarity": "sr"}).rarity == "SR"
    assert parse_card({"card": {"wikipedia_title": "B", "rarity": "L"}}).title == "B"
    assert parse_card({}).title == "?"


def test_parse_stock_reads_the_text_of_the_pulls_page():
    page = "Ouvrir un paquet\nOuvrir\n3 / 10\npaquets disponibles\nProchain dans 4:37\nRechargez"
    stock = parse_stock(page)
    assert (stock.available, stock.maximum, stock.next_in) == (3, 10, "4:37")
    full = parse_stock("10 / 10\npaquets disponibles")
    assert (full.available, full.next_in) == (10, None)
    assert parse_stock("1 / 10 paquet disponible").available == 1
    assert parse_stock("Connexion\nMot de passe") is None


def test_a_zero_stock_without_a_countdown_label_is_a_page_that_did_not_sync():
    # Le vrai site écrit « 0 / 10 » aussi quand la lecture du profil a échoué, mais sans « Prochain dans ».
    unsynced = "Ouvrir\n0 / 10\npaquets disponibles"
    assert not stock_synced(unsynced, parse_stock(unsynced))
    empty = "Ouvrir\n0 / 10\npaquets disponibles\nProchain dans 4:37"
    assert stock_synced(empty, parse_stock(empty))
    # Le libellé suffit, même si le compte à rebours n'est pas (encore) lisible ; le stock plein n'en a pas.
    ready = "3 / 10\npaquets disponibles\nProchain dans Prêt !"
    assert stock_synced(ready, parse_stock(ready))
    full = "10 / 10\npaquets disponibles"
    assert stock_synced(full, parse_stock(full))
    # Le « prochain pack spécial » n'est pas le compte à rebours des paquets.
    special = "0 / 10\npaquets disponibles\nProchain pack spécial dans 5:00"
    assert not stock_synced(special, parse_stock(special))


def test_only_a_zero_stock_needs_the_countdown_label_to_be_trusted():
    # Le seul repli de la page est « 0 » : un stock de 1 à 9 a donc été lu, même si la formulation de
    # « Prochain dans » change. Ne rejeter que le zéro évite de bloquer tous les runs pour un libellé.
    reworded = "5 / 10\npaquets disponibles\nProchain paquet dans 4:37"
    assert stock_synced(reworded, parse_stock(reworded))
    bare = "1 / 10\npaquets disponibles"
    assert stock_synced(bare, parse_stock(bare))
    # Le zéro, lui, reste suspect tant que le libellé manque, quelle que soit la formulation.
    zero = "0 / 10\npaquets disponibles\nProchain paquet dans 4:37"
    assert not stock_synced(zero, parse_stock(zero))


def test_special_packs_are_only_attempted_when_the_page_offers_them():
    assert special_offered("Packs Spéciaux\nGratuit · 1 / 6h\nAnimaux")
    assert special_offered("Packs Spéciaux Illimité")
    assert special_offered("packs spéciaux\ngratuit · 1 / 6h")
    assert special_offered("Packs Spéciaux\nGratuit")  # é décomposé
    assert not special_offered("3 / 10\npaquets disponibles\nProchain dans 4:37")
    # Un « gratuit » ailleurs sur la page ne suffit pas : il doit accompagner l'en-tête de la section.
    assert not special_offered("Packs Spéciaux\nAnimaux\nSR+\nUn essai gratuit")
    assert not special_offered("Offre gratuite\nPacks standards")


def test_reply_flags_a_refused_session_a_turnstile_and_cards():
    assert as_reply({"status": 401, "url": "/api/packs/open", "body": {"error": "Non autorisé"}}).auth_refused
    # fetch suit la redirection vers /login et rend du HTML : pas de JSON, mais l'URL finale parle.
    assert as_reply({"status": 200, "url": "https://x.test/login", "body": None}).auth_refused
    assert not as_reply({"status": 403, "url": "https://x.test/api/packs/open", "body": {}}).auth_refused

    human = as_reply({"status": 403, "body": {"human_verification_required": True, "error": "x"}})
    assert human.human_check and not human.cards

    ok = as_reply({"status": 200, "body": {"cards": [{"a": 1}, "bruit"], "packs_remaining": 4}})
    assert ok.cards == [{"a": 1}] and ok.packs_remaining == 4
    # Des cartes dans une réponse en échec ne comptent pas ; un booléen n'est pas un nombre de packs.
    assert as_reply({"status": 500, "body": {"cards": [{"a": 1}]}}).cards == []
    assert as_reply({"status": 200, "body": {"packs_remaining": True}}).packs_remaining is None
    assert as_reply(None) == Reply(0, {}, "") and as_reply({"status": 200, "body": [1]}).body == {}
    assert as_reply({"status": 403, "body": {}}).error == "HTTP 403"


def test_parse_special_is_defensive():
    special = parse_special({"packs": [{"id": "animaux", "name": "A"}, {"name": "sans id"}, "disney", 3],
                             "available": True, "is_vip": False,
                             "next_available_at": "2026-10-21T00:00:00Z"})
    assert special.pack_ids == ("animaux", "disney") and special.available
    assert special.next_available_at == "2026-10-21T00:00:00Z"
    # Une valeur « presque vraie » n'ouvre rien : seul `true` compte.
    assert not parse_special({"packs": ["a"], "available": "false"}).available
    assert not parse_special({"packs": ["a"], "available": 1}).available
    empty = parse_special({"packs": "n'importe quoi"})
    assert empty.pack_ids == () and not empty.available and empty.next_available_at is None
    assert parse_special({}).pack_ids == ()


def test_wait_text():
    now = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
    assert wait_text("2026-10-20T17:12:30Z", now) == "5 h 12 min"
    assert wait_text("2026-10-20T12:07:00+00:00", now) == "7 min"
    assert wait_text("2026-10-20T11:00:00Z", now) is None
    assert wait_text("pas une date", now) is None and wait_text(None, now) is None


@pytest.mark.parametrize("method, path", [
    ("POST", "/api/packs/pro-daily"), ("GET", "/api/packs/pro-daily"), ("POST", "/api/packs/grace"),
    ("POST", "/api/checkout"), ("POST", "/api/billing/portal"), ("POST", "/api/human-check"),
    ("GET", "/api/packs/open"),
])
def test_calls_outside_the_allow_list_are_refused_before_touching_the_page(method, path):
    with pytest.raises(ValueError):
        call(None, method, path)


def test_site_declares_what_login_needs():
    site = WikiMasters()
    assert site.hosts == ("www.wiki-masters.com",)
    assert site.login_url == "https://www.wiki-masters.com/login"
    assert site.import_export is False


def test_a_cookie_export_is_never_imported(make_settings):
    settings = make_settings()
    settings.cookies_dir.mkdir(parents=True)
    export = settings.cookies_dir / "www.wiki-masters.com_cookies.txt"
    export.write_text(f"www.wiki-masters.com\tFALSE\t/\tTRUE\t{int(time.time()) + 86400}\t"
                      "sb-abc-auth-token.0\tbase64-synthetique\n", encoding="utf-8")
    site = WikiMasters()
    assert session_store(settings, site).export_path() is None
    assert not site.configured(settings)
    assert "aucune session" in site.session_summary(settings)
    with pytest.raises(AuthError, match="login wikimasters"):
        site.run(settings)  # sans session ni profil, le navigateur n'est même pas lancé
    assert not settings.sessions_dir.exists()  # et rien n'a été importé


def test_configured_and_summary_come_from_the_stored_session_and_never_show_values(make_settings):
    settings = make_settings()
    site = WikiMasters()
    store = SessionStore(settings, "wikimasters", site.hosts, import_export=False)
    tracking = {"name": "autre", "value": "x", "domain": "www.wiki-masters.com", "path": "/", "expires": -1}
    session = {"name": "sb-abc-auth-token.0", "value": "valeur-secrete-synthetique", "path": "/",
               "domain": "www.wiki-masters.com", "expires": int(time.time()) + 100 * 86400}
    store.save([tracking, session])
    assert site.configured(settings)
    summary = site.session_summary(settings)
    assert "99 j" in summary or "100 j" in summary
    assert "valeur-secrete" not in summary and "sb-abc" not in summary

    # Un état qui ne contient plus le cookie d'authentification (le site l'a effacé) : plus de session.
    store.save([tracking])
    assert not site.configured(settings)

    # Mais le profil existe : le site reste dans les runs planifiés (comme `run`), qui rappelleront
    # « login » par le code de sortie 2 au lieu de l'abandonner en silence.
    (settings.profiles_dir / "wikimasters").mkdir(parents=True)
    assert site.configured(settings)
    assert "aucune session" in site.session_summary(settings)


# -- navigateur réel contre le faux site -----------------------------------------------------------


@requires_browser
@pytest.mark.parametrize("redirect", ["server", "client"])
def test_an_expired_session_is_an_auth_error(fake, make_settings, redirect):
    fake.redirect = redirect
    settings = make_settings()
    site = site_on_fake(settings, value="perimee")  # le faux site ne la reconnaît plus
    with pytest.raises(AuthError, match="login wikimasters"):
        site.run(settings)
    assert posts(fake) == []


@requires_browser
def test_a_challenge_page_is_reported_and_never_bypassed(fake, make_settings):
    fake.challenge = True
    settings = make_settings()
    site = site_on_fake(settings)
    with pytest.raises(SiteError, match="ne le contourne pas") as raised:
        site.run(settings)
    assert not isinstance(raised.value, AuthError) and "cf-mitigated: challenge" in str(raised.value)
    assert posts(fake) == []


@requires_browser
def test_a_challenge_on_the_api_stops_the_run_and_keeps_what_was_opened(fake, make_settings):
    fake.packs = 5
    fake.api_challenge_after = 1
    settings = make_settings()
    result = site_on_fake(settings).run(settings)

    assert not result.ok and not result.auth_failed and result.opened == 1
    assert "Défi anti-robot sur /api/packs/open (HTTP 429, x-vercel-mitigated: challenge)" in result.message
    assert "ne le contourne pas" in result.message
    assert posts(fake) == ["/api/packs/open"] * 2  # aucune nouvelle tentative, aucun pack spécial
    assert_nothing_forbidden(fake)


@requires_browser
def test_opens_every_pack_then_the_special_pack_and_keeps_the_renewed_session(fake, make_settings):
    fake.renew_to = "renouvele"
    settings = make_settings()
    site = site_on_fake(settings)
    result = site.run(settings)

    assert result.ok and result.opened == 4 and (result.packs_before, result.packs_max) == (3, 10)
    assert len(result.cards) == 20 and result.cards[4].title == "Carte 1 imbriquée"
    assert result.cards[-5].title == "Spéciale 0"
    assert "Tous les packs disponibles" in result.message and "Pack spécial ouvert" in result.message
    assert fake.packs == 0 and fake.special_opened == ["animaux"]
    assert posts(fake) == ["/api/packs/open"] * 3 + ["/api/packs/special"]
    assert_nothing_forbidden(fake)
    # Le site a fait tourner le jeton dans le profil : c'est la nouvelle valeur qui est conservée.
    assert SessionStore(settings, "wikimasters", ("127.0.0.1",), import_export=False).value(COOKIE) == "renouvele"

    # Le run suivant repart de la session renouvelée (le profil et l'état concordent).
    fake.packs = 1
    again = site.run(settings)
    assert again.ok and again.opened == 1 and "Pack spécial : prochain dans" in again.message


@requires_browser
def test_no_stock_skips_the_open_call_and_a_special_cooldown_is_not_an_error(fake, make_settings):
    fake.packs = 0
    fake.special_available = False
    settings = make_settings()
    result = site_on_fake(settings).run(settings)

    assert result.ok and result.opened == 0 and result.packs_before == 0
    assert "Aucun pack disponible (prochain dans 4:37)" in result.message
    assert "Pack spécial : prochain dans 5 h" in result.message
    assert posts(fake) == []
    assert_nothing_forbidden(fake)


@requires_browser
def test_a_turnstile_stops_cleanly_and_keeps_what_was_opened(fake, make_settings):
    fake.packs = 5
    fake.human_after = 2
    settings = make_settings()
    result = site_on_fake(settings).run(settings)

    assert not result.ok and not result.auth_failed
    assert result.opened == 2 and len(result.cards) == 10 and fake.packs == 3
    assert "vérification humaine" in result.message and "login wikimasters" in result.message
    # On s'arrête net : ni nouvelle tentative, ni pack spécial, ni appel à /api/human-check.
    assert posts(fake) == ["/api/packs/open"] * 3
    assert_nothing_forbidden(fake)


@requires_browser
def test_a_session_lost_midway_is_an_auth_failure_that_keeps_the_opened_packs(fake, make_settings):
    fake.packs = 5
    fake.revoke_after = 1
    settings = make_settings()
    result = site_on_fake(settings).run(settings)

    assert not result.ok and result.auth_failed and result.opened == 1 and len(result.cards) == 5
    assert "login wikimasters" in result.message


@requires_browser
def test_dry_run_max_packs_and_a_missing_special_feature(fake, make_settings):
    settings = make_settings()
    site = site_on_fake(settings)

    settings.dry_run = True
    simulated = site.run(settings)
    assert simulated.opened == 0 and fake.packs == 3 and posts(fake) == []
    assert simulated.packs_before == 3
    assert "Simulation" in simulated.message and "Prochain pack dans 4:37" in simulated.message
    assert "Pack spécial disponible" in simulated.message

    settings.dry_run = False
    settings.max_packs = 1
    limited = site.run(settings)
    assert limited.ok and limited.opened == 1 and fake.packs == 2
    assert "Limite de 1 packs atteinte, il en reste 2" in limited.message
    assert "spécial" not in limited.message
    assert_nothing_forbidden(fake)


@requires_browser
def test_a_special_api_that_answers_404_is_a_missing_feature_not_an_error(fake, make_settings):
    # La page affiche la section (elle est donc tentée), mais l'API répond 404 : indicateur de build éteint.
    fake.special_enabled = False
    settings = make_settings()
    result = site_on_fake(settings).run(settings)

    assert result.ok and result.opened == 3 and fake.special_opened == []
    assert ("GET", "/api/packs/special") in fake.calls  # le 404 a bien été essuyé
    assert posts(fake) == ["/api/packs/open"] * 3
    assert "spécial" not in result.message
    assert_nothing_forbidden(fake)


@requires_browser
def test_a_suspended_account_opens_nothing(fake, make_settings):
    fake.suspended = True
    settings = make_settings()
    result = site_on_fake(settings).run(settings)
    assert not result.ok and result.opened == 0 and "suspendu" in result.message
    assert posts(fake) == []


def special_calls(fake):
    return [c for c in fake.calls if c[1] == "/api/packs/special"]


@requires_browser
def test_a_page_that_did_not_sync_is_an_error_not_an_empty_stock(fake, make_settings, monkeypatch):
    monkeypatch.setattr(wikimasters, "PULLS_TIMEOUT_S", 2.5)
    fake.unsynced = True
    settings = make_settings()
    with pytest.raises(SiteError, match="non synchronisé") as raised:
        site_on_fake(settings).run(settings)
    assert not isinstance(raised.value, AuthError)
    assert fake.api_calls() == [("GET", "/api/state")]  # la page seule : le bot n'a rien appelé


@requires_browser
def test_a_network_failure_midway_keeps_the_opened_packs(fake, make_settings):
    fake.packs = 5
    fake.drop_after = 2
    settings = make_settings()
    result = site_on_fake(settings).run(settings)  # ne lève pas : sinon l'historique perdrait les 2 packs

    assert not result.ok and not result.auth_failed
    assert result.opened == 2 and len(result.cards) == 10 and fake.packs == 3
    assert "Interrompu" in result.message and "impossible depuis la page" in result.message
    assert special_calls(fake) == []  # on ne continue pas sur un compte qui ne répond plus
    assert_nothing_forbidden(fake)


@requires_browser
def test_a_request_that_never_answers_is_cut_and_keeps_the_opened_packs(fake, make_settings, monkeypatch):
    # `page.evaluate` ignore le délai de Playwright : c'est le signal du `fetch` qui coupe l'attente.
    monkeypatch.setattr(wikimasters, "CALL_TIMEOUT_S", 1.0)
    fake.packs = 5
    fake.hang_after = 2
    settings = make_settings()
    started = time.monotonic()
    result = site_on_fake(settings).run(settings)  # sans coupure, ce run resterait suspendu 30 s

    assert time.monotonic() - started < 25
    assert not result.ok and not result.auth_failed
    assert result.opened == 2 and len(result.cards) == 10
    assert "Interrompu" in result.message and "sans réponse après 1 s" in result.message
    assert special_calls(fake) == []
    assert_nothing_forbidden(fake)


@requires_browser
def test_a_failure_before_any_opening_still_raises(fake, make_settings):
    fake.drop_after = 0
    settings = make_settings()
    with pytest.raises(SiteError, match="impossible depuis la page"):
        site_on_fake(settings).run(settings)


@requires_browser
def test_an_unexpected_refusal_stops_the_run_before_the_special_pack(fake, make_settings):
    fake.refuse_open = True
    settings = make_settings()
    result = site_on_fake(settings).run(settings)

    assert not result.ok and result.opened == 0 and "Ouverture refusée (Compte bloqué)" in result.message
    assert posts(fake) == ["/api/packs/open"]  # pas de nouvelle tentative
    assert special_calls(fake) == [] and fake.special_opened == []
    assert_nothing_forbidden(fake)


@requires_browser
def test_the_special_pack_counts_in_the_run_limit(fake, make_settings):
    fake.packs = 3
    settings = make_settings()
    settings.max_packs = 1
    result = site_on_fake(settings).run(settings)

    assert result.ok and result.opened == 1 and fake.packs == 2
    assert posts(fake) == ["/api/packs/open"]
    assert special_calls(fake) == [] and fake.special_opened == []

    # Avec une place de plus dans la limite, le pack spécial (le deuxième ouvert) est tenté.
    settings.max_packs = 2
    fake.packs = 1
    again = site_on_fake(settings).run(settings)
    assert again.opened == 2 and fake.special_opened == ["animaux"]


@requires_browser
def test_the_special_pack_is_not_touched_when_the_page_does_not_offer_it(fake, make_settings):
    fake.special_in_ui = False  # l'API répond pourtant : l'interface, elle, ne propose rien
    settings = make_settings()
    result = site_on_fake(settings).run(settings)

    assert result.ok and result.opened == 3
    assert special_calls(fake) == [] and fake.special_opened == []
    assert "spécial" not in result.message
    assert_nothing_forbidden(fake)


def test_each_refusal_of_the_site_stops_the_run_with_the_right_error():
    assert issubclass(StopRun, SiteError) and not issubclass(StopRun, AuthError)
    with pytest.raises(AuthError):
        _check(as_reply({"status": 401, "body": {"error": "Non autorisé"}}))
    with pytest.raises(HumanVerificationRequired, match="Turnstile"):
        _check(as_reply({"status": 403, "body": {"human_verification_required": True}}))
    challenge = as_reply({"status": 429, "url": "https://x.test/api/packs/open",
                          "mitigated": "x-vercel-mitigated: challenge"})
    with pytest.raises(ChallengeDetected, match="x-vercel-mitigated: challenge"):
        _check(challenge)
    with pytest.raises(ChallengeDetected):  # un défi n'est jamais ignoré, même pour une simple lecture
        _check(challenge, session=False)
    _check(as_reply({"status": 200, "body": {"cards": []}}))
    # Lecture optionnelle : un 401 y signifie « fonctionnalité absente », pas « session perdue ».
    _check(as_reply({"status": 401, "body": {"error": "Non autorisé"}}), session=False)
