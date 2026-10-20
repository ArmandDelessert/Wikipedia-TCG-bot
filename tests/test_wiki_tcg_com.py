"""wiki-tcg.com : analyse des réponses (pur), état par HTTP (faux transport) et pilotage de l'interface
(faux site local reprenant le DOM observé en vrai : `.opener[data-phase]`, `.flipper__slot[data-rarity]`,
`.tcg__name`, `.opener__bar`…, avec la fenêtre « Récompense de connexion » par-dessus).
"""
import contextlib
import dataclasses
import json
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from tests.conftest import requires_browser
from wikitcg_bot.cookies import is_expired
from wikitcg_bot.models import AuthError, Card, SiteError
from wikitcg_bot.sessions import SessionStore
from wikitcg_bot.sites import wiki_tcg_com
from wikitcg_bot.sites.wiki_tcg_com import (
    Status, WikiTcgCom, looks_like_challenge, merge_cookies, parse_cards, parse_set_cookies,
    parse_speciaux, parse_status,
)

FUTURE = int(time.time()) + 86400 * 20


def compte(player, statut=200):
    return {"parts": {"compte": {"statut": statut, "corps": {"player": player}}}}


# -- analyse pure ----------------------------------------------------------------------------------
def test_parse_status_reads_stock_dust_and_specials():
    player = {"packsStored": 7, "dust": 800, "speciaux": [{"sorte": "legendaire", "nombre": 2}], "invite": False}
    assert parse_status(compte(player)) == Status(7, 800, {"legendaire": 2})
    assert parse_status(compte(player)).total == 9
    assert parse_status(compte({"packsStored": 0, "speciaux": []})) == Status(0, 0, {})


def test_parse_status_null_player_means_refused_session():
    with pytest.raises(AuthError, match="refusé ou expiré"):
        parse_status(compte(None))
    for statut in (401, 403):
        with pytest.raises(AuthError):
            parse_status(compte(None, statut=statut))


def test_parse_status_part_failure_is_not_a_refused_session():
    # Le client du site fabrique `{statut: 500, corps: {error: "absent"}}` pour une partie en panne.
    down = {"parts": {"compte": {"statut": 500, "corps": {"error": "absent"}}}}
    with pytest.raises(SiteError, match="statut 500") as info:
        parse_status(down)
    assert not isinstance(info.value, AuthError)
    with pytest.raises(SiteError) as info:
        parse_status(compte(None, statut=502))
    assert not isinstance(info.value, AuthError)


def test_parse_status_rejects_unexpected_shapes():
    for payload in ({}, {"parts": {}}, [], None, compte({"dust": 3}), compte({"packsStored": "3"}),
                    compte(["x"]), compte("x"), compte({})):
        with pytest.raises(SiteError):
            parse_status(payload)


def test_parse_speciaux_is_tolerant():
    assert parse_speciaux([{"sorte": "epique", "nombre": 1}, {"sorte": "epique", "nombre": 2},
                           {"sorte": "prisme", "nombre": 0}, {"nombre": 4}, "?"]) == {"epique": 3}
    assert parse_speciaux({"legendaire": 1, "x": "2"}) == {"legendaire": 1}
    assert parse_speciaux(None) == {} and parse_speciaux([]) == {}


def test_parse_cards_keeps_order_and_codes():
    raw = [{"rarity": "c", "name": " Paris "}, {"rarity": "FA", "name": "Seine"}, {"rarity": "", "name": ""}]
    assert parse_cards(raw) == [Card("Paris", "C"), Card("Seine", "FA"), Card("?", "?")]


def test_parse_set_cookies_and_merge():
    now = 1_000_000
    cookies = parse_set_cookies([
        "wikitcg_session=abc.def; Path=/; Max-Age=2592000; HttpOnly; Secure; SameSite=Lax",
        "autre=1; Domain=wiki-tcg.com; Path=/x; Expires=Wed, 21 Oct 2037 07:28:00 GMT",
        "volatil=2",
        "vide=; Max-Age=0; Path=/",
    ], "wiki-tcg.com", now)
    by_name = {c["name"]: c for c in cookies}
    session = by_name["wikitcg_session"]
    assert session["value"] == "abc.def" and session["domain"] == "wiki-tcg.com"
    assert session["expires"] == now + 2592000 and session["httpOnly"] and session["secure"]
    assert by_name["autre"]["domain"] == ".wiki-tcg.com" and by_name["autre"]["path"] == "/x"
    assert by_name["autre"]["expires"] > now
    assert by_name["volatil"]["expires"] == -1
    assert by_name["vide"]["expires"] == now  # Max-Age=0 : échu, écarté à la sauvegarde

    current = [{"name": "wikitcg_session", "value": "ancien", "domain": "wiki-tcg.com", "path": "/"},
               {"name": "wikitcg_v", "value": "v", "domain": "wiki-tcg.com", "path": "/"}]
    merged = {c["name"]: c["value"] for c in merge_cookies(current, [session])}
    assert merged == {"wikitcg_v": "v", "wikitcg_session": "abc.def"}


def test_parse_set_cookies_ignores_garbage():
    assert parse_set_cookies(["", "pas un cookie", "=;;;", "mauvais nom=1"], "wiki-tcg.com") == []


def test_parse_set_cookies_deletion_with_a_past_expires_is_expired():
    # Next.js supprime un cookie avec `Expires=<1970>` sans Max-Age : l'échéance ne doit pas retomber à 0
    # (que `is_expired` prend pour un cookie de session) sous peine de garder un cookie vide.
    for date in ("Thu, 01 Jan 1970 00:00:00 GMT", "Wed, 31 Dec 1969 23:59:59 GMT", "Thu, 01 Jan 1970 00:00:00"):
        (gone,) = parse_set_cookies([f"wikitcg_session=; Path=/; Expires={date}"], "wiki-tcg.com")
        assert gone["expires"] > 0 and is_expired(gone), date


def test_parse_set_cookies_unknown_attributes_neither_invent_nor_drop_cookies():
    cookies = parse_set_cookies(["wikitcg_session=abc; Path=/; Max-Age=60; Priority=High; Partitioned"],
                                "wiki-tcg.com", 1000)
    assert [(c["name"], c["value"], c["expires"]) for c in cookies] == [("wikitcg_session", "abc", 1060)]
    (quoted,) = parse_set_cookies(['v="a=b"; SAMESITE=Lax; httponly'], "wiki-tcg.com")
    assert quoted["value"] == '"a=b"' and quoted["httpOnly"] and not quoted["secure"]


def test_looks_like_challenge():
    assert looks_like_challenge({"CF-Mitigated": "challenge"}, "")
    assert looks_like_challenge({}, "Just a moment...")
    assert not looks_like_challenge({"server": "nginx"}, "Boosters · WikiTCG")


# -- état par HTTP ---------------------------------------------------------------------------------
@pytest.fixture
def real_host_session(make_settings):
    """Réglages avec un export de cookies pour le vrai hôte (le transport est simulé : pas de réseau)."""
    settings = make_settings()
    settings.cookies_dir.mkdir(parents=True)
    (settings.cookies_dir / "wiki-tcg.com_cookies.txt").write_text(
        f"wiki-tcg.com\tFALSE\t/\tTRUE\t{FUTURE}\twikitcg_session\ttoken-test\n", encoding="utf-8")
    return settings


def api(player, **kwargs):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=compte(player), **kwargs)
    return handler, seen


def no_browser(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("aucun navigateur ne doit être lancé")
    monkeypatch.setattr(wiki_tcg_com, "browser_page", forbidden)


def test_dry_run_reads_status_without_a_browser(real_host_session, monkeypatch):
    no_browser(monkeypatch)
    handler, seen = api({"packsStored": 4, "dust": 10, "speciaux": [{"sorte": "legendaire", "nombre": 1}]})
    real_host_session.dry_run = True
    result = WikiTcgCom(transport=httpx.MockTransport(handler)).run(real_host_session)
    assert result.ok and result.opened == 0 and (result.packs_before, result.packs_max) == (4, 10)
    assert "Simulation" in result.message and "5 booster" in result.message and "1 spécial" in result.message
    request = seen[0]
    assert request.url.path == "/api/v1/battement" and request.url.params["parts"] == "compte"
    assert request.headers["cookie"] == "wikitcg_session=token-test"


def test_empty_stock_skips_the_browser(real_host_session, monkeypatch):
    no_browser(monkeypatch)
    handler, _ = api({"packsStored": 0, "speciaux": []})
    result = WikiTcgCom(transport=httpx.MockTransport(handler)).run(real_host_session)
    assert result.ok and result.opened == 0 and "Aucun booster" in result.message


def test_null_player_is_an_auth_error(real_host_session, monkeypatch):
    no_browser(monkeypatch)
    handler, _ = api(None)
    with pytest.raises(AuthError):
        WikiTcgCom(transport=httpx.MockTransport(handler)).run(real_host_session)


def test_missing_session_is_an_auth_error(make_settings):
    with pytest.raises(AuthError, match="Aucune session"):
        WikiTcgCom(transport=httpx.MockTransport(lambda r: httpx.Response(500))).run(make_settings())


def test_status_retries_server_errors_then_gives_up(real_host_session, monkeypatch):
    monkeypatch.setattr(wiki_tcg_com, "time", types.SimpleNamespace(sleep=lambda s: None, time=time.time))
    calls = []

    def flaky(request):
        calls.append(request)
        return httpx.Response(502) if len(calls) < 3 else httpx.Response(200, json=compte({"packsStored": 2}))
    real_host_session.dry_run = True
    assert WikiTcgCom(transport=httpx.MockTransport(flaky)).run(real_host_session).packs_before == 2
    assert len(calls) == 3

    calls.clear()
    with pytest.raises(SiteError, match="HTTP 503"):
        WikiTcgCom(transport=httpx.MockTransport(
            lambda r: calls.append(r) or httpx.Response(503))).run(real_host_session)
    assert len(calls) == 3


def test_http_401_and_challenge_are_distinguished(real_host_session):
    with pytest.raises(AuthError):
        WikiTcgCom(transport=httpx.MockTransport(lambda r: httpx.Response(401))).run(real_host_session)
    blocked = httpx.Response(403, headers={"cf-mitigated": "challenge"})
    with pytest.raises(SiteError, match="anti-bot") as info:
        WikiTcgCom(transport=httpx.MockTransport(lambda r: blocked)).run(real_host_session)
    assert not isinstance(info.value, AuthError)


def test_challenge_on_a_5xx_is_not_retried(real_host_session, monkeypatch):
    monkeypatch.setattr(wiki_tcg_com, "time", types.SimpleNamespace(sleep=lambda s: None, time=time.time))
    calls = []

    def blocked(request):
        calls.append(request)
        return httpx.Response(503, headers={"cf-mitigated": "challenge"})
    with pytest.raises(SiteError, match="anti-bot") as info:
        WikiTcgCom(transport=httpx.MockTransport(blocked)).run(real_host_session)
    assert not isinstance(info.value, AuthError) and len(calls) == 1  # une seule requête, pas de rejeu


def test_part_failure_in_a_200_response_is_a_site_error_not_an_auth_error(real_host_session):
    down = {"parts": {"compte": {"statut": 500, "corps": {"error": "absent"}}}}
    with pytest.raises(SiteError) as info:
        WikiTcgCom(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=down))).run(real_host_session)
    assert not isinstance(info.value, AuthError)


def test_unreadable_final_stock_is_not_reported_as_everything_opened(real_host_session, monkeypatch):
    fake_open_all(monkeypatch, opened=2)
    answers = iter([httpx.Response(200, json=compte({"packsStored": 4})), httpx.Response(502)])
    result = WikiTcgCom(transport=httpx.MockTransport(lambda r: next(answers, httpx.Response(502)))).run(
        real_host_session)
    assert result.ok and result.opened == 2
    assert "illisible" in result.message and "Tous les boosters" not in result.message


def fake_open_all(monkeypatch, opened):
    """Remplace le navigateur et l'ouverture : seul le bilan final (stock relu par l'API) est exercé."""
    @contextlib.contextmanager
    def fake_browser(settings, store=None, **kwargs):
        yield None
    monkeypatch.setattr(wiki_tcg_com, "browser_page", fake_browser)
    monkeypatch.setattr(wiki_tcg_com, "time", types.SimpleNamespace(sleep=lambda s: None, time=time.time))
    monkeypatch.setattr(WikiTcgCom, "_open_all",
                        lambda self, page, settings, result: setattr(result, "opened", opened))


def run_with_final_stock(settings, before, after):
    answers = iter([httpx.Response(200, json=compte({"packsStored": before})),
                    httpx.Response(200, json=compte({"packsStored": after}))])
    return WikiTcgCom(transport=httpx.MockTransport(lambda r: next(answers))).run(settings)


def test_stopping_early_with_stock_left_is_a_failure(real_host_session, monkeypatch):
    # La boucle d'ouverture s'est arrêtée (sélecteur périmé…) alors que le site annonce encore des boosters :
    # un faux succès laisserait le stock monter jusqu'au plafond sans que personne ne le voie.
    fake_open_all(monkeypatch, opened=1)
    result = run_with_final_stock(real_host_session, before=5, after=4)
    assert not result.ok and not result.auth_failed and result.opened == 1
    assert "Arrêt prématuré" in result.message and "encore 4" in result.message


def test_one_regenerated_booster_during_the_run_is_not_a_failure(real_host_session, monkeypatch):
    fake_open_all(monkeypatch, opened=3)
    result = run_with_final_stock(real_host_session, before=3, after=1)
    assert result.ok and "il en reste 1" in result.message


def test_reaching_the_cap_with_stock_left_is_not_a_failure(real_host_session, monkeypatch):
    fake_open_all(monkeypatch, opened=real_host_session.max_packs)
    result = run_with_final_stock(real_host_session, before=10, after=6)
    assert result.ok and "Limite de" in result.message


def test_cookie_deleted_by_the_status_call_is_not_kept_as_an_empty_cookie(real_host_session):
    real_host_session.dry_run = True
    handler, _ = api({"packsStored": 1}, headers={
        "set-cookie": "wikitcg_session=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT"})
    WikiTcgCom(transport=httpx.MockTransport(handler)).run(real_host_session)
    store = SessionStore(real_host_session, "wiki_tcg_com", ("wiki-tcg.com",))
    assert "wikitcg_session" not in {c["name"] for c in store.load()}


def test_cookie_renewed_by_the_status_call_is_persisted(real_host_session):
    real_host_session.dry_run = True
    handler, _ = api({"packsStored": 1}, headers={
        "set-cookie": "wikitcg_session=renouvele; Path=/; Max-Age=2592000; Secure; HttpOnly"})
    WikiTcgCom(transport=httpx.MockTransport(handler)).run(real_host_session)
    store = SessionStore(real_host_session, "wiki_tcg_com", ("wiki-tcg.com",))
    assert store.value("wikitcg_session") == "renouvele"


def test_site_declares_what_the_cli_needs(real_host_session, tmp_path):
    site = WikiTcgCom()
    assert site.name == "wiki_tcg_com" and site.hosts == ("wiki-tcg.com",)
    assert site.login_url == "https://wiki-tcg.com/fr/connexion"
    nothing = dataclasses.replace(real_host_session, state_dir=tmp_path / "vide", cookies_dir=tmp_path / "vide")
    assert site.configured(real_host_session) and not site.configured(nothing)
    summary = site.session_summary(real_host_session)
    assert "wikitcg_session" in summary and "token-test" not in summary


# -- faux site ---------------------------------------------------------------------------------------
PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Boosters · WikiTCG</title></head><body>
<div class="cnx" role="presentation" style="position:fixed;inset:0;z-index:99;background:#000c;display:flex">
  <div class="cnx__carte" role="dialog" aria-modal="true"><h2>Récompense de connexion</h2>
  <button type="button" class="btn cnx__bouton" onclick="fetch('/api/v1/connexion',{method:'POST'})">Récupérer</button></div></div>
<div id="app"></div>
<script>
const RANG = {C: 0, PC: 1, R: 2, E: 3, L: 4};
const S = {phase: 'idle', ignorer: %(ignorer)d, stock: %(stock)d, speciaux: %(speciaux)s, choisi: null, cartes: [], ouvertes: new Set(), erreur: false};
const prive = () => S.choisi ? (S.speciaux.find(x => x.sorte === S.choisi) || {nombre: 0}).nombre : S.stock;
const fini = () => S.cartes.length > 0 && S.ouvertes.size === S.cartes.length;
function render() {
  if (S.choisi && !S.speciaux.some(x => x.sorte === S.choisi) && S.phase === 'idle') S.choisi = null;
  const o = [];
  if (S.phase !== 'reveal') {
    o.push('<section class="speciaux">' + (S.speciaux.length
      ? '<ul class="speciaux__rayon">' + S.speciaux.map(x => `<li><button type="button" class="special" data-sorte="${x.sorte}"
          aria-pressed="${S.choisi === x.sorte}">${x.sorte} ×${x.nombre}</button></li>`).join('') + '</ul>'
      : '<div class="speciaux__invitation"><a class="btn" href="/fake/tutoriel">Jouer le tutoriel</a></div>') + '</section>');
  }
  o.push(`<div class="opener" data-phase="${S.phase}" ${fini() ? 'data-fini="1"' : ''}>`);
  if (S.phase === 'reveal') {
    o.push('<div class="reveal">' + S.cartes.map((c, i) => `<div class="flipper__slot" data-rarity="${c.rarity}">
      <button type="button" class="flipper" data-flipped="${S.ouvertes.has(i)}" data-i="${i}">
      <span class="flipper__face flipper__front"><div class="tcg" data-rarity="${c.rarity}"><h3 class="tcg__name">${c.name}</h3></div></span>
      </button></div>`).join('') + '</div>');
    o.push('<div class="opener__bar">' + (fini()
      ? `<span class="bilan"><b>${S.cartes.length}</b> nouvelles</span>
         <button type="button" class="btn" data-act="again" ${S.stock + S.speciaux.reduce((n, x) => n + x.nombre, 0) <= 0 ? 'disabled' : ''}>Ouvrir un autre booster</button>
         <button type="button" class="btn btn--ghost" data-act="collection">Voir ma collection</button>`
      : '<p class="opener__hint">Toucher pour révéler</p><button type="button" class="btn btn--ghost" data-act="all">Tout révéler</button>') + '</div>');
  } else {
    o.push(`<div class="sachet-scene"><button type="button" class="sachet" aria-label="Ouvrir" ${S.choisi ? `data-sorte="${S.choisi}"` : ''}
      ${prive() <= 0 || S.phase === 'tearing' ? 'disabled' : ''}><span class="sr-only">${prive()} disponible(s)</span></button>
      <p class="reserve__ligne"><b>${S.stock}</b><span class="reserve__sur">/ 10</span></p>
      ${S.erreur ? '<p class="auth__error" role="alert">Aucun booster en réserve.</p>' : ''}</div>`);
  }
  o.push('</div>');
  document.getElementById('app').innerHTML = o.join('');
}
async function ouvrir() {
  S.phase = 'tearing'; S.erreur = false; render();
  const r = await fetch('/fake/open', {method: 'POST', body: JSON.stringify({sorte: S.choisi})});
  const d = await r.json();
  if (!d.ok) { S.phase = 'idle'; S.erreur = true; render(); return; }
  S.cartes = d.cartes.sort((a, b) => RANG[a.rarity] - RANG[b.rarity]); S.stock = d.stock; S.speciaux = d.speciaux; S.ouvertes = new Set();
  setTimeout(() => { S.phase = 'reveal'; render(); }, 120);
}
document.getElementById('app').addEventListener('click', e => {
  const sachet = e.target.closest('button.sachet'), special = e.target.closest('button.special');
  const flip = e.target.closest('button.flipper'), act = e.target.closest('[data-act]');
  if (sachet && S.ignorer > 0) S.ignorer--;  // page pas encore « hydratée » : le clic est perdu
  else if (sachet && !sachet.disabled) ouvrir();
  else if (special) { S.choisi = S.choisi === special.dataset.sorte ? null : special.dataset.sorte; render(); }
  else if (flip && !S.ouvertes.has(+flip.dataset.i)) { S.ouvertes.add(+flip.dataset.i); render(); }
  else if (flip) document.title = 'zoom';  // un clic sur une carte déjà retournée ouvre le zoom : à éviter
  else if (act && act.dataset.act === 'all') S.cartes.forEach((c, i) => setTimeout(() => { S.ouvertes.add(i); render(); }, 30 * i));
  else if (act && act.dataset.act === 'again' && !act.disabled) { S.phase = 'idle'; S.cartes = []; S.ouvertes = new Set(); render(); }
  else if (act && act.dataset.act === 'collection') location.href = '/fake/collection';
});
render();
</script></body></html>"""


class FakeWikiTcg(BaseHTTPRequestHandler):
    # État du « serveur », remis à zéro par la fixture.
    stock = 3
    speciaux: list = []
    mode = "normal"  # normal | challenge | redirect | refuse
    opened: list = []
    claimed = False
    collection_hits = 0
    renew_cookie = False
    ignored_clicks = 0

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

    def _logged_in(self):
        cookie = self.headers.get("Cookie") or ""
        return "wikitcg_session=good" in cookie or "wikitcg_session=renewed" in cookie

    def do_GET(self):
        cls = type(self)
        path = self.path.split("?")[0]
        if path == "/api/v1/battement":
            player = {"packsStored": cls.stock, "dust": 0, "speciaux": cls.speciaux} if self._logged_in() else None
            self._send(200, json.dumps(compte(player)))
        elif path == "/fr/boosters":
            if cls.mode == "challenge":
                self._send(403, "<html><head><title>Just a moment...</title></head></html>", "text/html",
                           [("cf-mitigated", "challenge")])
            elif cls.mode == "redirect" or not self._logged_in():
                self._send(302, "", "text/html", [("Location", "/fr/connexion")])
            else:
                page = PAGE % {"stock": cls.stock, "speciaux": json.dumps(cls.speciaux), "ignorer": cls.ignored_clicks}
                extra = [("Set-Cookie", "wikitcg_session=renewed; Path=/; Max-Age=2592000")] if cls.renew_cookie else []
                self._send(200, page, "text/html", extra)
        elif path == "/fr/connexion":
            self._send(200, "<html><head><title>Connexion</title></head><body>connexion</body></html>", "text/html")
        elif path == "/fake/collection":
            cls.collection_hits += 1
            self._send(200, "<html><title>Collection</title></html>", "text/html")
        else:
            self._send(404, "{}")

    def do_POST(self):
        cls = type(self)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode() or "{}") if length else {}
        if self.path == "/api/v1/connexion":
            cls.claimed = True
            self._send(200, "{}")
        elif self.path == "/fake/open" and self._logged_in():
            sorte = body.get("sorte")
            special = next((x for x in cls.speciaux if x["sorte"] == sorte), None) if sorte else None
            if cls.mode == "refuse" or (special is None and (sorte or cls.stock <= 0)) or (
                    special is not None and special["nombre"] <= 0):
                self._send(200, json.dumps({"ok": False}))
                return
            if special is not None:
                special["nombre"] -= 1
                cls.speciaux = [x for x in cls.speciaux if x["nombre"] > 0]
            else:
                cls.stock -= 1
            n = len(cls.opened) + 1
            cls.opened.append(sorte or "")
            rarities = ["C", "C", "PC", "R", "L" if sorte else "C"]
            self._send(200, json.dumps({"ok": True, "stock": cls.stock, "speciaux": cls.speciaux,
                                        "cartes": [{"name": f"Carte {n}.{i}", "rarity": r}
                                                   for i, r in enumerate(rarities)]}))
        else:
            self._send(401 if not self._logged_in() else 404, "{}")


@pytest.fixture
def fake_site(monkeypatch, make_settings):
    for key, value in dict(stock=3, speciaux=[], mode="normal", opened=[], claimed=False, collection_hits=0,
                           renew_cookie=False, ignored_clicks=0).items():
        setattr(FakeWikiTcg, key, value)
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWikiTcg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(wiki_tcg_com, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(WikiTcgCom, "hosts", ("127.0.0.1",))  # les cookies de test sont ceux de l'hôte local
    settings = make_settings()
    settings.cookies_dir.mkdir(parents=True)
    (settings.cookies_dir / "127.0.0.1_cookies.txt").write_text(
        f"127.0.0.1\tFALSE\t/\tFALSE\t{FUTURE}\twikitcg_session\tgood\n", encoding="utf-8")
    FakeWikiTcg.settings = settings
    yield FakeWikiTcg
    server.shutdown()


# -- pilotage de l'interface -------------------------------------------------------------------------
@requires_browser
def test_opens_every_pack_and_reads_the_drawn_cards(fake_site):
    fake_site.renew_cookie = True
    result = WikiTcgCom().run(fake_site.settings)
    assert result.ok and result.opened == 3 and fake_site.stock == 0
    assert (result.packs_before, result.packs_max) == (3, 10)
    assert [c.title for c in result.cards[:5]] == [f"Carte 1.{i}" for i in (0, 1, 4, 2, 3)]  # ordre du site : par rareté
    assert sorted(c.rarity for c in result.cards[:5]) == ["C", "C", "C", "PC", "R"]
    assert len(result.cards) == 15 and "Tous les boosters" in result.message
    # « Voir ma collection » quitte la page : jamais cliqué ; la récompense de connexion, qui recouvre
    # la page, n'est pas réclamée (le bot n'a pas cette autorisation).
    assert fake_site.collection_hits == 0 and fake_site.claimed is False
    # Le cookie renouvelé par le site pendant le run est conservé pour le suivant.
    store = SessionStore(fake_site.settings, "wiki_tcg_com", ("127.0.0.1",))
    assert store.value("wikitcg_session") == "renewed"


@requires_browser
def test_respects_max_packs(fake_site):
    fake_site.stock = 5
    fake_site.settings.max_packs = 2
    result = WikiTcgCom().run(fake_site.settings)
    assert result.opened == 2 and fake_site.stock == 3
    assert "Limite de 2" in result.message and "il en reste 3" in result.message


@requires_browser
def test_a_click_lost_before_hydration_is_retried(fake_site):
    fake_site.stock = 1
    fake_site.ignored_clicks = 2
    result = WikiTcgCom().run(fake_site.settings)
    assert result.opened == 1 and fake_site.opened == [""]


@requires_browser
def test_opens_special_boosters_after_the_normal_ones(fake_site):
    fake_site.stock = 1
    fake_site.speciaux = [{"sorte": "legendaire", "nombre": 2}]
    result = WikiTcgCom().run(fake_site.settings)
    assert result.opened == 3 and fake_site.opened == ["", "legendaire", "legendaire"]
    assert fake_site.stock == 0 and fake_site.speciaux == []
    assert sum(c.rarity == "L" for c in result.cards) == 2


@requires_browser
def test_only_special_boosters_left(fake_site):
    fake_site.stock = 0
    fake_site.speciaux = [{"sorte": "epique", "nombre": 1}]
    result = WikiTcgCom().run(fake_site.settings)
    assert result.opened == 1 and fake_site.opened == ["epique"]


@requires_browser
def test_dry_run_and_empty_stock_open_nothing(fake_site):
    fake_site.settings.dry_run = True
    assert WikiTcgCom().run(fake_site.settings).opened == 0
    fake_site.settings.dry_run = False
    fake_site.stock = 0
    assert WikiTcgCom().run(fake_site.settings).opened == 0
    assert fake_site.opened == []


@requires_browser
def test_refused_opening_is_an_error_when_nothing_was_opened(fake_site):
    fake_site.mode = "refuse"
    with pytest.raises(SiteError, match="refusé"):
        WikiTcgCom().run(fake_site.settings)


@requires_browser
def test_interruption_after_some_openings_keeps_them(fake_site, monkeypatch):
    fake_site.stock = 3
    original = WikiTcgCom._again
    calls = []

    def again_then_refuse(page):
        original(page)
        calls.append(1)
        fake_site.mode = "refuse"  # le site se met à refuser à partir du 2e booster
    monkeypatch.setattr(WikiTcgCom, "_again", staticmethod(again_then_refuse))
    result = WikiTcgCom().run(fake_site.settings)
    assert result.opened == 1 and not result.ok and not result.auth_failed
    assert "Interrompu après 1" in result.message and len(result.cards) == 5


@requires_browser
def test_missing_again_button_is_an_error_not_a_normal_end(fake_site, monkeypatch):
    fake_site.stock = 3
    monkeypatch.setattr(wiki_tcg_com, "_AGAIN", ".opener__bar:has(.bilan-disparu) button")
    result = WikiTcgCom().run(fake_site.settings)
    assert not result.ok and result.opened == 1 and len(result.cards) == 5 and fake_site.stock == 2
    assert "Interrompu après 1" in result.message and "introuvable" in result.message


@requires_browser
def test_unusable_sachet_while_the_api_announces_boosters_is_an_error(fake_site, monkeypatch):
    fake_site.stock = 0
    fake_site.speciaux = [{"sorte": "epique", "nombre": 2}]
    monkeypatch.setattr(wiki_tcg_com, "_SPECIAUX", ".speciaux button.introuvable")
    with pytest.raises(SiteError, match="Aucun sachet ouvrable"):
        WikiTcgCom().run(fake_site.settings)
    assert fake_site.opened == []


@requires_browser
def test_expired_session_redirected_to_login_is_an_auth_error(fake_site):
    fake_site.mode = "redirect"
    with pytest.raises(AuthError, match="connexion"):
        WikiTcgCom().run(fake_site.settings)
    assert fake_site.opened == []


@requires_browser
def test_challenge_page_stops_without_any_bypass(fake_site):
    fake_site.mode = "challenge"
    with pytest.raises(SiteError, match="anti-bot") as info:
        WikiTcgCom().run(fake_site.settings)
    assert not isinstance(info.value, AuthError) and fake_site.opened == []
