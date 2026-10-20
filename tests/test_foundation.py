"""Socle local : cookies Netscape, session persistante, verrou, réglages."""
import json
import os
import time

import pytest

from wikitcg_bot import cookies as ck
from wikitcg_bot.config import Settings, load_env_file
from wikitcg_bot.history import record
from wikitcg_bot.locking import AlreadyRunning, run_lock
from wikitcg_bot.models import Card, SiteResult
from wikitcg_bot.sessions import SessionStore

FUTURE = int(time.time()) + 86400 * 20
EXPORT = "\n".join([
    "# Netscape HTTP Cookie File",
    "# This is a generated file! Do not edit.",
    "",
    f"wikitcg.net\tFALSE\t/\tTRUE\t{FUTURE}\twtcg_session\tabc.def.ghi",
    f".wikitcg.net\tTRUE\t/\tTRUE\t{FUTURE}\tcf_clearance\tcfvalue",
    "wikitcg.net\tFALSE\t/\tTRUE\t0\tsessiononly\tx",
    f"#HttpOnly_wikitcg.net\tFALSE\t/\tTRUE\t{FUTURE}\thidden\th",
    f"wikitcg.net\tFALSE\t/\tTRUE\t{int(time.time()) - 10}\tpérimé\tx",
    f"autre.example\tFALSE\t/\tTRUE\t{FUTURE}\tailleurs\tz",
    "ligne cassée sans tabulation",
]) + "\n"


def test_parse_netscape_reads_the_seven_columns():
    cookies = {c["name"]: c for c in ck.parse_netscape(EXPORT)}
    assert set(cookies) == {"wtcg_session", "cf_clearance", "sessiononly", "hidden", "périmé", "ailleurs"}
    session = cookies["wtcg_session"]
    assert session["domain"] == "wikitcg.net" and session["secure"] is True
    assert session["expires"] == FUTURE and session["httpOnly"] is False
    assert cookies["cf_clearance"]["domain"] == ".wikitcg.net"
    assert cookies["sessiononly"]["expires"] == -1  # 0 = cookie de session
    assert cookies["hidden"]["httpOnly"] is True


def test_expiry_and_host_matching():
    cookies = {c["name"]: c for c in ck.parse_netscape(EXPORT)}
    assert ck.is_expired(cookies["périmé"]) and not ck.is_expired(cookies["wtcg_session"])
    assert not ck.is_expired(cookies["sessiononly"])
    assert ck.covers(".wikitcg.net", "www.wikitcg.net")
    assert not ck.covers("wikitcg.net", "www.wikitcg.net")  # cookie propre à l'hôte
    assert not ck.covers(".wikitcg.net", "evilwikitcg.net")
    assert [c["name"] for c in ck.for_hosts(list(cookies.values()), ("wikitcg.net",))
            if c["name"] == "ailleurs"] == []


def test_header_only_includes_valid_cookies_for_the_host():
    header = ck.header(ck.parse_netscape(EXPORT), "wikitcg.net")
    assert "wtcg_session=abc.def.ghi" in header and "cf_clearance=cfvalue" in header
    assert "périmé" not in header and "ailleurs" not in header


@pytest.fixture
def settings(tmp_path):
    return Settings(env={}, state_dir=tmp_path / "state", cookies_dir=tmp_path / "Cookies", delay=(0, 0))


def _write_export(settings, text=EXPORT):
    settings.cookies_dir.mkdir(parents=True, exist_ok=True)
    path = settings.cookies_dir / "wikitcg.net_cookies.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_store_imports_export_then_keeps_renewed_state(settings):
    store = SessionStore(settings, "wikitcg_net", ("wikitcg.net",))
    assert not store.configured() and store.load() == []
    export = _write_export(settings)
    assert store.configured()
    names = {c["name"] for c in store.load()}
    assert names == {"wtcg_session", "sessiononly", "hidden"}  # ni périmé, ni étranger, ni cf_clearance (lié à l'IP)

    renewed = store.load()
    for c in renewed:
        if c["name"] == "wtcg_session":
            c["value"] = "nouveau.jeton.renouvelé"
    store.save(renewed)
    # Le même export plus ancien que l'état ne l'écrase pas.
    os.utime(export, (time.time() - 3600, time.time() - 3600))
    assert store.value("wtcg_session") == "nouveau.jeton.renouvelé"
    # Un export refait plus tard reprend le dessus.
    _write_export(settings)
    os.utime(export, (time.time() + 60, time.time() + 60))
    assert store.value("wtcg_session") == "abc.def.ghi"


def test_store_save_drops_expired_foreign_and_cloudflare_cookies(settings):
    store = SessionStore(settings, "wikitcg_net", ("wikitcg.net",))
    store.save(ck.parse_netscape(EXPORT))
    data = json.loads(store.state_path.read_text(encoding="utf-8"))
    assert {c["name"] for c in data["cookies"]} == {"wtcg_session", "sessiononly", "hidden"}


def test_describe_never_shows_values(settings):
    _write_export(settings)
    text = SessionStore(settings, "wikitcg_net", ("wikitcg.net",)).describe(("wtcg_session",))
    assert "wtcg_session" in text and "abc.def.ghi" not in text and "expire dans 20.0 j" in text


def test_run_lock_is_exclusive_and_released(tmp_path):
    lock = tmp_path / "run.lock"
    with run_lock(lock):
        with pytest.raises(AlreadyRunning):
            with run_lock(lock):
                pass
    assert not lock.exists()
    with run_lock(lock):  # de nouveau disponible
        pass


def test_run_lock_ignores_a_stale_lock(tmp_path):
    lock = tmp_path / "run.lock"
    lock.write_text("123")
    os.utime(lock, (time.time() - 7200, time.time() - 7200))
    with run_lock(lock, stale_after=1800):
        pass


def test_settings_from_env(tmp_path):
    s = Settings.from_env({"BROWSER_CHANNEL": "msedge", "HEADLESS": "0", "COOKIES_DIR": str(tmp_path)})
    assert s.browser_channel == "msedge" and s.headless is False and s.cookies_dir == tmp_path
    default = Settings.from_env({})
    assert default.browser_channel == "chrome" and default.headless is True
    assert Settings.from_env({"BROWSER_CHANNEL": ""}).browser_channel is None


def test_env_file(tmp_path):
    path = tmp_path / ".env"
    path.write_text("# commentaire\nMAX_PACKS_PER_RUN=5\nHEADLESS='0'\n\nbad line\n", encoding="utf-8")
    assert load_env_file(path) == {"MAX_PACKS_PER_RUN": "5", "HEADLESS": "0"}
    assert load_env_file(tmp_path / "absent") == {}


def test_history_records_only_real_openings(settings):
    result = SiteResult("wikitcg_net", "wikitcg.net", opened=2, packs_before=3,
                        cards=[Card("Whale shark", "UR")])
    settings.dry_run = True
    record(settings, result)
    assert not settings.history_file.exists()
    settings.dry_run = False
    record(settings, SiteResult("x", "x"))
    assert not settings.history_file.exists()
    record(settings, result)
    entry = json.loads(settings.history_file.read_text(encoding="utf-8"))
    assert entry["site"] == "wikitcg_net" and entry["cards"] == [{"title": "Whale shark", "rarity": "UR"}]
