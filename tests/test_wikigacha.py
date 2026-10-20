"""Wikipedia Gacha : fonctions pures (aucun réseau) puis pilotage d'un faux site local avec un vrai navigateur.

Le faux site reproduit ce que le vrai fait pour le module : bandeau de consentement et fenêtres
d'annonce (avec des leurres à ne JAMAIS cliquer), base locale IndexedDB `wiki-gacha-db`,
`/api/user/progress`, `POST /api/gacha`, synchronisation différée par `/api/user/progress/replay`.
Toutes les données sont synthétiques.
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tests.conftest import requires_browser
from wikitcg_bot.models import AuthError, Card, SiteError
from wikitcg_bot.sites import wikigacha as w
from wikitcg_bot.sites.wikigacha import Collection, WikiGacha


# -- fonctions pures ---------------------------------------------------------------------------------
def test_parse_stock_french_and_english():
    assert w.parse_stock("PAQUETS QUOTIDIENS: 7 / 10\nProchain pack dans: 0:18") == (7, 10)
    assert w.parse_stock("PAQUETS QUOTIDIENS : 11 / 10") == (11, 10)  # une mission peut dépasser le plafond
    assert w.parse_stock("DAILY PACKS: 3 / 10") == (3, 10)
    assert w.parse_stock("Plus de packs") is None


def test_parse_countdown_and_empty_state():
    assert w.parse_countdown("Prochain pack dans: 0:18") == 18
    assert w.parse_countdown("Next pack in: 12:05") == 725
    assert w.parse_countdown("rien") is None
    assert w.no_packs_left("Plus de packs\nProchain pack dans: 0:16") and w.no_packs_left("No Packs Left")
    assert not w.no_packs_left("PAQUETS QUOTIDIENS: 7 / 10")


def test_effective_stock_refills_one_pack_per_minute_up_to_the_cap():
    base = 1_000_000
    assert w.effective_stock(0, base, base + 59_000) == 0
    assert w.effective_stock(0, base, base + 60_000) == 1
    assert w.effective_stock(4, base, base + 10 * 60_000) == 10  # plafonné
    assert w.effective_stock(11, base, base + 10 * 60_000) == 11  # un stock de mission n'est pas rogné
    assert w.effective_stock(3, base + 5_000, base) == 3  # horloge en retard : pas de gain négatif


def test_parse_cloud_summary_and_pack_state():
    payload = {"exists": True, "cardSummary": {"cardCount": 1279, "copyCount": 1280, "authoritativeCardsReady": True},
               "state": {"meta": {"packState": {"balance": 0, "lastRefillAt": 1792527736838, "sig": "x"}}}}
    assert w.parse_cloud_summary(payload) == Collection(1279, 1280)
    assert w.parse_pack_state(payload) == (0, 1792527736838)
    assert w.parse_cloud_summary({"cardSummary": {"cardCount": 3}}) is None
    assert w.parse_cloud_summary(None) is None and w.parse_pack_state({}) is None
    assert w.parse_cloud_summary({"cardSummary": {"cardCount": "x", "copyCount": True}}) == Collection(0, 0)


def test_parse_cards_reads_title_and_rarity_rank():
    payload = {"cards": [{"id": 1, "title": "Baleine", "rarity_rank": "ssr"}, {"id": 2, "title": "", "rarity_rank": "C"},
                         {"id": 3}, "bruit"], "packKind": "rainbow"}
    assert w.parse_cards(payload) == [Card("Baleine", "SSR"), Card("2", "C"), Card("3", "?")]
    assert w.parse_cards({"error": "x"}) == [] and w.parse_cards(None) == []


def test_signed_in_and_challenge_detection():
    assert w.signed_in({"user": {"name": "x"}, "expires": "2026-11-19"})
    assert not w.signed_in({}) and not w.signed_in(None) and not w.signed_in({"user": None})
    assert w.is_challenge("Un instant…", None, 403)
    assert w.is_challenge("Just a moment...", "challenge", 403)
    assert w.is_challenge("Wikipedia Gacha", "Challenge")  # l'en-tête suffit
    assert not w.is_challenge("Wikipedia Gacha", None, 200)
    assert not w.is_challenge("Un instant de lecture", None, 200)  # un titre sans statut d'échec n'est pas un défi


def test_collection_gate_refuses_every_doubtful_state():
    cloud = Collection(1279, 1280)
    assert w.collection_gate(cloud, Collection(1279, 1280), 0) is None
    assert "vide" in w.collection_gate(cloud, Collection(0, 0), 0)
    assert "incomplète" in w.collection_gate(cloud, Collection(1200, 1201), 0)
    assert "incomplète" in w.collection_gate(cloud, Collection(1279, 1279), 0)
    assert "en avance" in w.collection_gate(cloud, Collection(1284, 1285), 0)
    assert "en attente" in w.collection_gate(cloud, Collection(1279, 1280), 2)
    assert w.collection_gate(Collection(0, 0), Collection(0, 0), 0) is None  # compte neuf : rien à protéger


def test_judge_growth_flags_any_drop():
    before = Collection(1279, 1280)
    assert w.judge_growth(before, Collection(1284, 1285), 5) == "ok"
    assert w.judge_growth(before, Collection(1279, 1285), 5) == "ok"  # que des doublons
    assert w.judge_growth(before, Collection(1279, 1280), 5) == "pending"  # synchronisation différée
    assert w.judge_growth(before, Collection(1284, 1279), 5) == "dropped"
    assert w.judge_growth(before, Collection(1278, 1290), 5) == "dropped"
    assert w.judge_growth(before, Collection(1279, 1280), 0) == "ok"


def test_backup_age_and_pruning(tmp_path):
    directory = tmp_path / "backups"
    assert w.latest_backup_age(directory, time.time()) is None
    directory.mkdir()
    now = time.time()
    for day in range(5):
        path = directory / f"wikigacha_FR_2026102{day}-120000.json"
        path.write_text("{}")
        os.utime(path, (now - 3600 * (5 - day), now - 3600 * (5 - day)))
    other = directory / "notes.txt"
    other.write_text("à garder")
    assert round(w.latest_backup_age(directory, now)) == 3600
    w.prune_backups(directory, keep=2)
    assert sorted(p.name for p in directory.iterdir()) == [
        "notes.txt", "wikigacha_FR_20261023-120000.json", "wikigacha_FR_20261024-120000.json"]


def test_site_declares_what_the_foundation_needs(make_settings):
    site = WikiGacha()
    assert site.hosts == ("wikigacha.com",) and site.login_url == "https://wikigacha.com/?lang=FR"
    settings = make_settings()
    assert not site.configured(settings)
    settings.sessions_dir.mkdir(parents=True)
    cookie = {"name": w.SESSION_COOKIE, "value": "secret-token", "domain": "wikigacha.com", "path": "/",
              "expires": int(time.time()) + 86400 * 30, "httpOnly": True, "secure": True}
    (settings.sessions_dir / "wikigacha.json").write_text(json.dumps({"saved_at": 0, "cookies": [cookie]}))
    assert site.configured(settings)
    summary = site.session_summary(settings)
    assert w.SESSION_COOKIE in summary and "expire dans 30.0 j" in summary and "secret-token" not in summary


# -- faux site -----------------------------------------------------------------------------------------
PAGE = """<!doctype html><html lang="fr-FR"><head><title>Wikipedia Gacha</title></head><body>
<button aria-label="Select Language"><span>🌐</span><span>FR</span></button>
<button class="GachaEffects_toggle__x" id="fx" aria-pressed="true" aria-label="Mode effets renforcés">Mode effets renforcés <b>ON</b></button>
<div id="app"></div>
<div id="modals"></div>
<script>
const $ = (id) => document.getElementById(id);
const post = (path, body) => fetch(path, {method: 'POST', body: JSON.stringify(body || {})});
const openDb = () => new Promise((res, rej) => {
  const r = indexedDB.open('wiki-gacha-db', 8);
  r.onupgradeneeded = () => { r.result.createObjectStore('cards_fr', {keyPath: 'id'}); r.result.createObjectStore('sync_queue', {keyPath: 'id'}); };
  r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
});
const write = (db, store, fn) => new Promise((res, rej) => {
  const t = db.transaction([store], 'readwrite'); fn(t.objectStore(store)); t.oncomplete = res; t.onerror = () => rej(t.error);
});
let db, stock = 0, results = false;

$('fx').onclick = () => { $('fx').setAttribute('aria-pressed', 'false'); post('/__clicked', {name: 'effects-toggle'}); };

function render() {
  if (results) return;
  $('app').innerHTML = stock <= 0
    ? '<h2>Plus de packs</h2><p>Prochain pack dans: 0:30</p><button id="ad">📺 Regarder une pub pour recharger</button>'
    : '<span>PAQUETS QUOTIDIENS: <b>' + stock + '</b> / 10</span><div id="gacha-pack-container" data-open-stage="idle" style="width:200px;height:200px;background:#ccc">▲ TOUCHEZ POUR OUVRIR ▲</div>';
  if ($('ad')) $('ad').onclick = () => post('/__clicked', {name: 'ad'});
  if ($('gacha-pack-container')) $('gacha-pack-container').onclick = open;
}

async function open() {
  const r = await fetch('/api/gacha', {method: 'POST', body: JSON.stringify({lang: 'FR'})});
  const data = await r.json();
  if (!r.ok) return;
  stock = data.packState.balance;
  await write(db, 'cards_fr', s => data.cards.forEach(c => s.put({...c, count: 1})));
  const entry = {id: 'r' + Date.now(), kind: 'gacha_pull_v1', lang: 'FR', cards: data.cards};
  await write(db, 'sync_queue', s => s.put(entry));
  setTimeout(async () => {  // synchronisation différée
    await post('/api/user/progress/replay', {entries: [entry]});
    await write(db, 'sync_queue', s => s.delete(entry.id));
  }, 1200);
  results = true;
  $('app').innerHTML = '<ul>' + data.cards.map(c => '<li>' + c.title + '</li>').join('') + '</ul><button id="back">RETOUR AUX PACKS</button>';
  $('back').onclick = () => { results = false; render(); };
}

function modal(html, then) { $('modals').innerHTML = html; then(); }
function showCampaign() {
  modal('<div><h3>Campagne anniversaire</h3><button id="share">Partager la campagne</button><button id="got">Compris</button></div>', () => {
    $('share').onclick = () => post('/__clicked', {name: 'share'});
    $('got').onclick = () => { $('modals').innerHTML = ''; };
  });
}
function showNotes() {
  modal('<div><h3>Notes de mise à jour</h3><label><input type="checkbox" id="never"> Ne plus afficher</label><button id="ok">OK</button></div>', () => {
    $('ok').onclick = showCampaign;
    $('never').onclick = () => post('/__clicked', {name: 'never-show'});
  });
}
function showConsent() {
  modal('<div role="dialog"><h3>Wikipedia Gacha demande votre consentement</h3><button id="allow">Autoriser</button><button id="manage">Gérer les options</button></div>', () => {
    $('allow').onclick = () => post('/__clicked', {name: 'consent-allow'});
    $('manage').onclick = () => {
      modal('<div role="dialog"><h3>Gérer vos données</h3>'
        + '<input type="checkbox" aria-label="Consentement (72 fournisseurs), Mesurer la performance">'
        + '<input type="checkbox" checked aria-label="Intérêt légitime (32 fournisseurs), Mesurer la performance">'
        + '<input type="checkbox" checked aria-label="Intérêt légitime (4 fournisseurs), Développer les services">'
        + '<button id="all">Tout accepter</button><button id="confirm">Confirmer les choix</button></div>', () => {
        $('all').onclick = () => post('/__clicked', {name: 'consent-all'});
        $('confirm').onclick = () => {
          post('/__consent', {checked: Array.from(document.querySelectorAll('input[type=checkbox]')).filter(i => i.checked).length});
          showNotes();
        };
      });
    };
  });
}

(async () => {
  db = await openDb();
  const flags = await (await fetch('/__flags')).json();
  const prog = await (await fetch('/api/user/progress?lang=FR&includeCards=1')).json();
  stock = prog.state.meta.packState.balance;
  if (!flags.noimport) await write(db, 'cards_fr', s => prog.state.cards.forEach(c => s.put(c)));
  render();
  setTimeout(showConsent, 150);  // le bandeau arrive en différé, comme le vrai
})();
</script></body></html>"""


class FakeState:
    def __init__(self, stock=3, cards=6):
        self.stock = stock
        self.cloud = {i: 1 for i in range(1, cards)} | {cards: 2}
        self.signed_in = True
        self.noimport = False
        self.drop = False
        self.challenge = False
        self.gacha_calls = 0
        self.full_reads = 0
        self.decoys: list[str] = []
        self.consent = None
        self.next_id = 100

    def summary(self):
        cards, copies = len(self.cloud), sum(self.cloud.values())
        if self.drop and self.gacha_calls:  # le cloud « perd » des cartes après la première ouverture
            cards, copies = cards - 8, copies - 8
        return {"cardCount": cards, "copyCount": copies, "authoritativeCardsReady": True}


class FakeGachaHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json", headers=()):
        data = (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    @property
    def fake(self) -> FakeState:
        return self.server.fake

    def do_GET(self):
        url = urlparse(self.path)
        fake = self.fake
        if url.path == "/":
            if fake.challenge:
                self._send(403, "<html><head><title>Un instant…</title></head><body>challenge</body></html>",
                           "text/html", [("cf-mitigated", "challenge")])
            else:
                self._send(200, PAGE, "text/html; charset=utf-8")
        elif url.path == "/api/auth/session":
            self._send(200, {"user": {"name": "joueur"}, "expires": "2099-01-01"} if fake.signed_in else {})
        elif url.path == "/__flags":
            self._send(200, {"noimport": fake.noimport})
        elif url.path == "/api/user/progress":
            full = parse_qs(url.query).get("includeCards") == ["1"]
            fake.full_reads += full
            cards = [{"id": i, "title": f"Carte {i}", "rarity_rank": "C", "count": n} for i, n in fake.cloud.items()]
            self._send(200, {"exists": True, "cardsIncluded": full, "cardSummary": fake.summary(),
                             "state": {"cards": cards if full else [],
                                       "meta": {"packState": {"balance": fake.stock, "lastRefillAt": 0}}}})
        else:
            self._send(404, {})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        path, fake = urlparse(self.path).path, self.fake
        if path == "/api/gacha":
            if fake.stock <= 0:
                return self._send(400, {"error": "insufficient packs"})
            fake.stock -= 1
            fake.gacha_calls += 1
            rarities = ["C", "UC", "R", "SR", "SSR"]
            cards = [{"id": fake.next_id + k, "title": f"Tirage {fake.next_id + k}", "rarity_rank": rarities[k]}
                     for k in range(5)]
            fake.next_id += 5
            self._send(200, {"cards": cards, "packState": {"balance": fake.stock}, "syncDeferred": True})
        elif path == "/api/user/progress/replay":
            for entry in body.get("entries", []):
                for card in entry["cards"]:
                    fake.cloud[card["id"]] = fake.cloud.get(card["id"], 0) + 1
            self._send(200, {"ok": True})
        elif path == "/__clicked":
            fake.decoys.append(body["name"])
            self._send(200, {})
        elif path == "/__consent":
            fake.consent = body
            self._send(200, {})
        else:
            self._send(404, {})


@pytest.fixture
def fake_site(monkeypatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGachaHandler)
    server.fake = FakeState()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(w, "BASE_URL", f"http://127.0.0.1:{server.server_port}")
    # Les attentes réelles (minutes) sont raccourcies pour garder les tests rapides.
    for name, value in {"GATE_WAIT_S": 1.5, "GATE_POLL_MS": 300, "SYNC_WAIT_S": 12, "SYNC_POLL_MS": 400,
                        "OVERLAY_PATIENCE_S": 0.8, "SETTLE_MS": 200}.items():
        monkeypatch.setattr(w, name, value)
    yield server.fake
    server.shutdown()


def backups(settings) -> list[Path]:
    return sorted((settings.state_dir / "backups").glob("wikigacha_FR_*.json"))


@requires_browser
def test_opens_packs_through_the_ui_and_checks_the_cloud(fake_site, make_settings):
    settings = make_settings()
    settings.max_packs = 2
    result = WikiGacha().run(settings)
    assert result.ok and result.opened == 2 and (result.packs_before, result.packs_max) == (3, 10)
    assert [c.rarity for c in result.cards] == ["C", "UC", "R", "SR", "SSR"] * 2
    assert "Limite de 2 packs" in result.message and "il en reste 1" in result.message
    assert "6 cartes / 7 exemplaires → 16 cartes / 17 exemplaires" in result.message
    assert fake_site.gacha_calls == 2 and sum(fake_site.cloud.values()) == 17
    # Sauvegarde intégrale du cloud avant la première ouverture.
    (backup,) = backups(settings)
    assert len(json.loads(backup.read_text(encoding="utf-8"))["state"]["cards"]) == 6
    # Consentement refusé (aucune case cochée) et aucun leurre cliqué.
    assert fake_site.consent == {"checked": 0}
    assert fake_site.decoys == ["effects-toggle"]  # le mode classique est choisi ; le reste n'est jamais touché


@requires_browser
def test_reuses_the_backup_of_the_day_and_stops_on_empty_stock(fake_site, make_settings):
    settings = make_settings()
    settings.max_packs = 10
    first = WikiGacha().run(settings)
    assert first.opened == 3 and "Tous les packs disponibles ont été ouverts" in first.message
    assert len(backups(settings)) == 1 and fake_site.full_reads == 2  # import du client + sauvegarde
    second = WikiGacha().run(settings)  # même profil : la base locale persiste, rien à ouvrir
    assert second.opened == 0 and second.packs_before == 0 and "Plus de pack" in second.message
    assert "ignorée" in second.message and "ad" not in fake_site.decoys
    assert len(backups(settings)) == 1 and fake_site.full_reads == 3  # seul le client a relu le cloud : une sauvegarde par jour


@requires_browser
def test_dry_run_opens_nothing_and_reports_the_collection(fake_site, make_settings):
    settings = make_settings()
    settings.dry_run = True
    result = WikiGacha().run(settings)
    assert result.opened == 0 and result.packs_before == 3 and fake_site.gacha_calls == 0
    assert "6 cartes / 7 exemplaires, identique au cloud" in result.message
    assert not backups(settings) and fake_site.decoys == []  # lecture seule : pas de clic, pas de sauvegarde


@requires_browser
def test_refuses_to_open_when_the_browser_starts_from_an_empty_collection(fake_site, make_settings):
    fake_site.noimport = True  # le navigateur n'importe pas le cloud : sa base locale reste vide
    settings = make_settings()
    with pytest.raises(SiteError, match="Ouverture refusée.*collection locale vide"):
        WikiGacha().run(settings)
    assert fake_site.gacha_calls == 0 and fake_site.stock == 3
    assert len(backups(settings)) == 1  # la sauvegarde précède la décision


@requires_browser
def test_a_cloud_drop_stops_everything_immediately(fake_site, make_settings):
    fake_site.drop = True
    settings = make_settings()
    result = WikiGacha().run(settings)
    assert not result.ok and result.opened == 1 and fake_site.gacha_calls == 1
    assert "ATTENTION" in result.message and "PERDU" in result.message
    assert len(result.cards) == 5  # ce qui a été tiré reste consigné


@requires_browser
def test_signed_out_session_is_an_auth_error(fake_site, make_settings):
    fake_site.signed_in = False
    with pytest.raises(AuthError, match="expirée"):
        WikiGacha().run(make_settings())
    assert fake_site.gacha_calls == 0


@requires_browser
def test_cloudflare_challenge_is_reported_not_bypassed(fake_site, make_settings):
    fake_site.challenge = True
    with pytest.raises(SiteError, match=re.compile("défi Cloudflare.*cf-mitigated: challenge")):
        WikiGacha().run(make_settings())
    assert fake_site.gacha_calls == 0
