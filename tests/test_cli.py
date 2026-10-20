"""Point d'entrée : planning, codes de sortie, verrou, historique (sites factices, aucun navigateur)."""
import json

import pytest

from wikitcg_bot import __main__ as cli
from wikitcg_bot.models import AuthError, Card, SiteError, SiteResult


class FakeSite:
    hosts = ("exemple.test",)
    login_url = "https://exemple.test/login"

    def __init__(self, name, result=None, error=None, configured=True):
        self.name, self.label = name, name.upper()
        self.result, self.error, self._configured, self.calls = result, error, configured, 0

    def configured(self, settings):
        return self._configured

    def session_summary(self, settings):
        return "session de test"

    def run(self, settings):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("COOKIES_DIR", str(tmp_path / "Cookies"))
    monkeypatch.setattr(cli, "setup_logging", lambda settings: None)
    return tmp_path


def use_sites(monkeypatch, *sites):
    monkeypatch.setattr(cli, "SITES", {s.name: s for s in sites})
    return sites


def opened(site, n=3, stock=10):
    return SiteResult(site, site.upper(), opened=n, packs_before=stock, packs_max=10,
                      cards=[Card("Paris", "UR")] * n)


def test_planning_skips_a_site_until_it_is_due(env, monkeypatch, capsys):
    site, = use_sites(monkeypatch, FakeSite("wikigacha", result=opened("wikigacha", n=10)))
    assert cli.main(["run"]) == 0 and site.calls == 1
    assert cli.main(["run"]) == 0 and site.calls == 1  # stock vidé : pas avant ~8 min
    assert cli.main(["run", "--force"]) == 0 and site.calls == 2
    assert cli.main(["run", "--sites", "wikigacha"]) == 0 and site.calls == 3  # nommer un site ignore le planning
    assert cli.main([]) == 0 and site.calls == 3  # sans argument : « run »


def test_history_and_state_are_written_only_for_real_runs(env, monkeypatch):
    use_sites(monkeypatch, FakeSite("wikitcg_net", result=opened("wikitcg_net", n=2, stock=2)))
    assert cli.main(["run", "--dry-run"]) == 0
    assert not (env / "state" / "history.jsonl").exists() and not (env / "state" / "schedule.json").exists()
    assert cli.main(["run"]) == 0
    entry = json.loads((env / "state" / "history.jsonl").read_text(encoding="utf-8"))
    assert entry["site"] == "wikitcg_net" and entry["opened"] == 2


def test_exit_codes(env, monkeypatch):
    auth, broken, fine = use_sites(
        monkeypatch,
        FakeSite("wikimasters", error=AuthError("jeton refusé")),
        FakeSite("wiki_tcg_com", error=SiteError("réseau")),
        FakeSite("wikigacha", result=opened("wikigacha")),
    )
    assert cli.main(["run", "--sites", "wikigacha"]) == cli.EXIT_OK
    assert cli.main(["run", "--sites", "wiki_tcg_com"]) == cli.EXIT_ERROR
    assert cli.main(["run", "--sites", "wikimasters"]) == cli.EXIT_AUTH
    assert cli.main(["run", "--sites", "wiki_tcg_com,wikimasters"]) == cli.EXIT_AUTH  # l'authentification l'emporte
    with pytest.raises(SystemExit) as usage:
        cli.main(["run", "--sites", "inconnu"])
    assert usage.value.code == cli.EXIT_USAGE  # pas 2 : ce serait lu comme « session expirée »


def test_a_failure_does_not_block_the_other_sites_and_is_logged_on_one_line(env, monkeypatch, caplog):
    error = SiteError("Call log:\n  - navigating to https://exemple.test\n  - waiting for selector")
    broken, fine = use_sites(monkeypatch, FakeSite("wiki_tcg_com", error=error),
                             FakeSite("wikigacha", result=opened("wikigacha")))
    with caplog.at_level("INFO", logger="wikitcg_bot"):
        assert cli.main(["run", "--force"]) == cli.EXIT_ERROR
    assert fine.calls == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("WIKI_TCG_COM : ÉCHEC" in m and "\n" not in m and "navigating" not in m for m in messages)
    assert any("WIKIGACHA : ok, 3 pack(s)" in m for m in messages)


def test_a_clean_stop_without_exception_is_still_logged(env, monkeypatch, caplog):
    stop = SiteResult("wikimasters", "WIKIMASTERS", ok=False, opened=1, message="Vérification humaine demandée")
    use_sites(monkeypatch, FakeSite("wikimasters", result=stop))
    with caplog.at_level("INFO", logger="wikitcg_bot"):
        assert cli.main(["run", "--force"]) == cli.EXIT_ERROR
    assert any("Vérification humaine demandée" in r.getMessage() for r in caplog.records)


def test_a_run_in_progress_makes_this_one_exit_quietly(env, monkeypatch):
    site, = use_sites(monkeypatch, FakeSite("wikigacha", result=opened("wikigacha")))
    (env / "state").mkdir()
    (env / "state" / "run.lock").write_text("123")
    assert cli.main(["run", "--force"]) == cli.EXIT_BUSY and site.calls == 0
    assert cli.main(["status"]) == cli.EXIT_BUSY and site.calls == 0
    cli_login = cli.login  # `login` partage le verrou : un profil Chrome ne s'ouvre pas deux fois
    assert cli_login(cli.Settings.from_env({"STATE_DIR": str(env / "state")}), site) == 3


def test_status_lists_sessions_hints_and_never_records_a_schedule(env, monkeypatch, capsys):
    ready = FakeSite("wikigacha", result=opened("wikigacha", n=0))
    absent = FakeSite("wikimasters", configured=False)
    use_sites(monkeypatch, ready, absent)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "WIKIGACHA : session de test" in out
    assert "login wikimasters" in out and absent.calls == 0
    assert ready.calls == 1
    assert not (env / "state" / "schedule.json").exists()


def test_one_line_collapses_and_truncates():
    assert cli.one_line("première\ndeuxième") == "première"
    assert cli.one_line("x" * 500).endswith("…") and len(cli.one_line("x" * 500)) == 160
    assert cli.one_line("") == ""
