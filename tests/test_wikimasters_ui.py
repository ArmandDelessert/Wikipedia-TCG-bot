"""Teste le pilotage Playwright de WikiMasters contre un faux site local.

Nécessite un Chromium : en local définir CHROMIUM_PATH ; en CI (variable CI) celui de Playwright est utilisé.
Le faux site reproduit le comportement décrit par les projets de référence :
redirection vers /login sans session, « X/Y paquets disponibles », POST /api/packs/open.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from wikitcg_bot.models import AuthError
from wikitcg_bot.sites import wikimasters
from wikitcg_bot.sites.wikimasters import WikiMasters, parse_card

CHROMIUM = os.environ.get("CHROMIUM_PATH")
pytestmark = pytest.mark.skipif(
    not (CHROMIUM or os.environ.get("CI")), reason="ni CHROMIUM_PATH ni CI définis"
)

LOGIN_PAGE = """<html><body><h1>Connexion</h1>
<label>Adresse courriel <input type="email" id="e"></label>
<label>Mot de passe <input type="password" id="p"></label>
<button type="submit" onclick="go()">Connexion</button>
<script>
async function go(){
  const r = await fetch('/api/login',{method:'POST',body:JSON.stringify({email:e.value,password:p.value})});
  if(r.ok) location.href='/pulls'; else document.body.append('Invalid login credentials');
}
</script></body></html>"""

PULLS_PAGE = """<html><body><script>
if(!document.cookie.includes('sid=ok')) location.href='/login';
else fetch('/api/status').then(r=>r.json()).then(s=>{
  document.body.innerText = s.available+'/'+s.max+' paquets disponibles';});
</script></body></html>"""


class FakeWikiMasters(BaseHTTPRequestHandler):
    packs = 3
    password = "secret"

    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="application/json", headers=()):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _logged_in(self):
        return "sid=ok" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        if self.path == "/login":
            self._send(200, LOGIN_PAGE, "text/html")
        elif self.path == "/pulls":
            self._send(200, PULLS_PAGE, "text/html")
        elif self.path == "/api/status":
            self._send(200, json.dumps({"available": type(self).packs, "max": 5}))
        else:
            self._send(404, "{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        if self.path == "/api/login":
            creds = json.loads(body)
            if creds["password"] == self.password:
                self._send(200, "{}", headers=[("Set-Cookie", "sid=ok; Path=/")])
            else:
                self._send(400, "{}")
        elif self.path == "/api/packs/open":
            cls = type(self)
            if not self._logged_in() or cls.packs <= 0:
                self._send(403, json.dumps({"error": "cooldown"}))
                return
            cls.packs -= 1
            self._send(200, json.dumps({"cards": [
                {"wikipedia_title": f"Carte {cls.packs}", "rarity": "sr"},
                {"card": {"wikipedia_title": "Imbriquée", "rarity": "C"}},
            ]}))
        else:
            self._send(404, "{}")


@pytest.fixture
def fake_site(monkeypatch):
    FakeWikiMasters.packs = 3
    FakeWikiMasters.password = "secret"
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWikiMasters)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setattr(wikimasters, "BASE_URL", base)
    monkeypatch.setattr(wikimasters, "PULLS_URL", f"{base}/pulls")
    yield FakeWikiMasters
    server.shutdown()


def settings_for(make_settings, **env):
    settings = make_settings(**env)
    settings.chromium_path = CHROMIUM or None
    return settings


def test_parse_card_handles_flat_and_nested():
    assert parse_card({"wikipedia_title": "A", "rarity": "sr"}).rarity == "SR"
    assert parse_card({"card": {"wikipedia_title": "B", "rarity": "L"}}).title == "B"


def test_logs_in_and_opens_every_pack(fake_site, make_settings):
    settings = settings_for(make_settings, WIKIMASTERS_EMAIL="a@b.c", WIKIMASTERS_PASSWORD="secret")
    result = WikiMasters().run(settings)
    assert result.opened == 3 and result.ok
    assert (result.packs_before, result.packs_max) == (3, 5)
    assert len(result.cards) == 6 and result.cards[1].title == "Imbriquée"
    assert "Plus de pack" in result.message


def test_wrong_password_is_an_auth_error(fake_site, make_settings):
    settings = settings_for(make_settings, WIKIMASTERS_EMAIL="a@b.c", WIKIMASTERS_PASSWORD="nope")
    with pytest.raises(AuthError):
        WikiMasters().run(settings)


def test_respects_max_packs_and_dry_run(fake_site, make_settings):
    settings = settings_for(make_settings, WIKIMASTERS_EMAIL="a@b.c", WIKIMASTERS_PASSWORD="secret")
    settings.max_packs = 1
    assert WikiMasters().run(settings).opened == 1
    settings.dry_run = True
    assert WikiMasters().run(settings).opened == 0
    assert fake_site.packs == 2
