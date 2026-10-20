import pytest

from wikitcg_bot import schedule
from wikitcg_bot.config import Settings
from wikitcg_bot.models import SiteResult


@pytest.fixture
def settings(tmp_path):
    return Settings(env={}, state_dir=tmp_path / "state")


def result(site, before, opened=0, maximum=None, ok=True):
    return SiteResult(site, site, ok=ok, opened=opened, packs_before=before, packs_max=maximum)


def test_fast_site_comes_back_when_stock_nears_full():
    # Wikipedia Gacha vidé : 10 packs × 60 s × 0,8
    assert schedule.due_in(result("wikigacha", 10, opened=10)) == pytest.approx(480)


def test_slow_site_waits_longer():
    assert schedule.due_in(result("wikimasters", 10, opened=10)) == pytest.approx(4800)
    assert schedule.due_in(result("wiki_tcg_com", 10, opened=10)) == pytest.approx(7200)


def test_wikitcg_net_never_waits_more_than_three_hours(monkeypatch):
    # 5 × 9600 s × 0,8 = 10,7 h, ramené à 3 h (pack mystère à la cadence inconnue)
    assert schedule.due_in(result("wikitcg_net", 5, opened=5)) == 3 * 3600
    assert schedule.due_in(result("wikitcg_net", 4, opened=0)) == pytest.approx(9600 * 0.8)
    monkeypatch.setattr(schedule, "MAX_WAIT", 600.0)  # le plafond général l'emporte s'il est plus bas
    assert schedule.due_in(result("wikitcg_net", 5, opened=5)) == 600.0


def test_run_stopped_by_the_cap_comes_back_quickly():
    # 10 en stock, 2 ouverts seulement (plafond de run) : il en reste 8, on repasse dans ~1,5 min
    assert schedule.due_in(result("wikigacha", 10, opened=2)) == pytest.approx(96)
    # 9 restants sur 10 : 48 s calculées, mais jamais moins d'une minute
    assert schedule.due_in(result("wikigacha", 10, opened=1)) == schedule.MIN_WAIT


def test_failure_backs_off_and_unknown_stock_is_not_gated():
    assert schedule.due_in(result("wikigacha", 5, ok=False)) == schedule.RETRY_AFTER_FAILURE
    assert schedule.due_in(result("wikigacha", None)) is None
    assert schedule.due_in(result("inconnu", 3)) is None


def test_record_and_is_due(settings):
    assert schedule.is_due(settings, "wikigacha", now=1000)
    schedule.record(settings, result("wikigacha", 10, opened=10), now=1000)
    assert not schedule.is_due(settings, "wikigacha", now=1100)
    assert schedule.seconds_left(settings, "wikigacha", now=1100) == pytest.approx(380)
    assert schedule.is_due(settings, "wikigacha", now=1481)
    schedule.record(settings, result("wikigacha", None), now=1000)  # stock inconnu : relancé à chaque tour
    assert schedule.is_due(settings, "wikigacha", now=1001)


def test_clear_and_corrupted_state(settings):
    schedule.record(settings, result("wikimasters", 10, opened=10), now=1000)
    schedule.clear(settings, "wikimasters")
    assert schedule.is_due(settings, "wikimasters", now=1001)
    (settings.state_dir / "schedule.json").write_text("pas du json", encoding="utf-8")
    assert schedule.is_due(settings, "wikimasters", now=1001)
