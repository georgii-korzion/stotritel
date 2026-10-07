from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from fimex_monitor.parse import Offer
from fimex_monitor.rules import (
    RULE_DROP,
    RULE_GAP,
    PrevPosition,
    Thresholds,
    apply_filters,
    evaluate,
    find_gaps,
    group,
    next_state,
    position_key,
    second_offer,
    split_by_cap,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
TH = Thresholds()
_row = iter(range(1, 10**6))


def offer(title, region, price, qty=1, brand="Apple", eta="08 Oct", article="ART", eta_date=date(2026, 10, 8)):
    return Offer(brand, title, article, eta, eta_date, region, Decimal(price), qty, next(_row))


def key(title, region, brand="Apple"):
    return position_key(offer(title, region, 1, brand=brand))


def prev(price, ago=timedelta(minutes=30), had_gap=False):
    return PrevPosition(Decimal(price), NOW - ago, had_gap)


def run(offers, prev_state=None, alerted=None, first_run=False, th=TH):
    return evaluate(offers, prev_state or {}, alerted or {}, NOW, th, first_run)


# --- правило A -------------------------------------------------------------------

@pytest.mark.parametrize("was, now, alert", [
    (1000, 950, True),  # ровно 5 %
    (1000, 951, False),  # 4,9 %, $49
    (2500, 2400, True),  # $100, хотя это 4 %
    (2500, 2401, False),  # $99, 3,96 %
    (1000, 1100, False),  # рост
    (1000, 1000, False),
])
def test_rule_a_table(was, now, alert):
    ev = run([offer("X", "🇺🇸 US", now)], {key("X", "🇺🇸 US"): prev(was)})
    assert [a.rules for a in ev.alerts] == ([(RULE_DROP,)] if alert else [])


def test_rule_a_alert_details():
    ev = run([offer("X", "🇺🇸 US", 950)], {key("X", "🇺🇸 US"): prev(1000)})
    (a,) = ev.alerts
    assert a.prev.price == Decimal(1000) and a.price == Decimal(950) and a.strength == Decimal(50)


def test_new_position_gives_no_rule_a():
    ev = run([offer("X", "🇺🇸 US", 500)], {key("Y", "🇺🇸 US"): prev(1000)})
    assert ev.alerts == []


def test_disappeared_and_returned_cheaper():
    # позиции не было в прошлом цикле, последняя известная цена — 3 часа назад
    ev = run([offer("X", "🇺🇸 US", 900)], {key("X", "🇺🇸 US"): prev(1000, ago=timedelta(hours=3))})
    assert [a.rules for a in ev.alerts] == [(RULE_DROP,)]


def test_stale_baseline_is_treated_as_new():
    ev = run([offer("X", "🇺🇸 US", 500)], {key("X", "🇺🇸 US"): prev(1000, ago=timedelta(hours=25))})
    assert ev.alerts == []
    fresh = run([offer("X", "🇺🇸 US", 500)], {key("X", "🇺🇸 US"): prev(1000, ago=timedelta(hours=24))})
    assert len(fresh.alerts) == 1  # ровно 24 часа — ещё не устарела


# --- правило B -------------------------------------------------------------------

@pytest.mark.parametrize("prices, gap_at, gap_abs", [
    ({"🇯🇵 JP": 900, "🇺🇸 US": 1000}, "🇯🇵 JP", 100),
    ({"🇯🇵 JP": 960, "🇺🇸 US": 1000}, None, None),
    ({"🇯🇵 JP": 2400, "🇺🇸 US": 2500}, "🇯🇵 JP", 100),
    ({"🇯🇵 JP": 900, "🇭🇰 HK": 910, "🇺🇸 US": 1000}, None, None),  # ближайший — HK, $10
    ({"🇯🇵 JP": 900}, None, None),
    ({"🇯🇵 JP": 900, "🇺🇸 US": 900}, None, None),  # равные цены — разрыва нет
])
def test_rule_b_table(prices, gap_at, gap_abs):
    gaps = find_gaps(group([offer("X", r, p) for r, p in prices.items()]), TH)
    if gap_at is None:
        assert gaps == {}
    else:
        (g,) = gaps.values()
        assert g.position.region == gap_at and g.gap_abs == Decimal(gap_abs)


def test_gap_pct_is_relative_to_second_price():
    (g,) = find_gaps(group([offer("X", "🇯🇵 JP", 900), offer("X", "🇺🇸 US", 1000)]), TH).values()
    assert g.gap_pct == Decimal(10) and g.nearest.region == "🇺🇸 US"


def test_gap_from_own_price_drop_alerts_b_only():
    offers = [offer("X", "🇯🇵 JP", 950), offer("X", "🇺🇸 US", 1000)]
    state = {key("X", "🇯🇵 JP"): prev(970), key("X", "🇺🇸 US"): prev(1000)}  # было $30, стало $50 = 5 %
    (a,) = run(offers, state).alerts
    assert a.rules == (RULE_GAP,) and a.position.region == "🇯🇵 JP"
    assert a.gap.gap_abs == Decimal(50) and a.strength == Decimal(50)


def test_new_position_with_gap_alerts_b():
    offers = [offer("X", "🇯🇵 JP", 900), offer("X", "🇺🇸 US", 1000)]
    (a,) = run(offers, {key("X", "🇺🇸 US"): prev(1000)}).alerts
    assert a.rules == (RULE_GAP,) and a.prev is None


def test_gap_from_other_region_price_rise_does_not_alert():
    offers = [offer("X", "🇯🇵 JP", 950), offer("X", "🇺🇸 US", 1100)]
    state = {key("X", "🇯🇵 JP"): prev(950), key("X", "🇺🇸 US"): prev(980)}
    ev = run(offers, state)
    assert ev.alerts == []
    assert key("X", "🇯🇵 JP") in ev.gaps  # разрыв запомнили
    updates, _ = next_state(state, ev, set(), NOW)
    assert {u.region: u.had_gap for u in updates} == {"🇯🇵 JP": True, "🇺🇸 US": False}


def test_gap_from_other_region_sold_out_does_not_alert():
    offers = [offer("X", "🇯🇵 JP", 950), offer("X", "🇺🇸 US", 1100)]
    state = {key("X", "🇯🇵 JP"): prev(950), key("X", "🇭🇰 HK"): prev(960), key("X", "🇺🇸 US"): prev(1100)}
    assert run(offers, state).alerts == []


def test_gap_persists_and_price_drops_slightly_no_repeat_b():
    offers = [offer("X", "🇯🇵 JP", 890), offer("X", "🇺🇸 US", 1000)]
    state = {key("X", "🇯🇵 JP"): prev(900, had_gap=True), key("X", "🇺🇸 US"): prev(1000)}
    assert run(offers, state).alerts == []


def test_gap_persists_and_big_drop_alerts_a_with_gap_line():
    offers = [offer("X", "🇯🇵 JP", 800), offer("X", "🇺🇸 US", 1000)]
    state = {key("X", "🇯🇵 JP"): prev(900, had_gap=True), key("X", "🇺🇸 US"): prev(1000)}
    (a,) = run(offers, state).alerts
    assert a.rules == (RULE_DROP,) and a.gap is not None and a.gap.gap_abs == Decimal(200)


def test_both_rules_in_one_alert():
    offers = [offer("X", "🇯🇵 JP", 1494, qty=19), offer("X", "🇦🇺 AU", 1805, qty=2)]
    state = {key("X", "🇯🇵 JP"): prev(1580), key("X", "🇦🇺 AU"): prev(1805)}
    (a,) = run(offers, state).alerts
    assert a.rules == (RULE_DROP, RULE_GAP)
    assert a.strength == Decimal(311)  # большее из падения ($86) и разрыва ($311)


def test_absent_position_gap_flag_is_cleared():
    state = {key("X", "🇯🇵 JP"): prev(900, had_gap=True), key("X", "🇺🇸 US"): prev(1000)}
    ev = run([offer("X", "🇺🇸 US", 1000)], state)
    updates, clear = next_state(state, ev, set(), NOW)
    assert clear == [key("X", "🇯🇵 JP")]
    assert [u.region for u in updates] == ["🇺🇸 US"]


# --- общее ------------------------------------------------------------------------

def test_cooldown_suppresses_same_or_higher_price():
    offers = [offer("X", "🇺🇸 US", 950)]
    state = {key("X", "🇺🇸 US"): prev(1000)}
    ev = run(offers, state, alerted={(key("X", "🇺🇸 US"), RULE_DROP): Decimal(950)})
    assert ev.alerts == [] and ev.suppressed == [(key("X", "🇺🇸 US"), RULE_DROP)]
    ev = run(offers, state, alerted={(key("X", "🇺🇸 US"), RULE_DROP): Decimal(960)})
    assert len(ev.alerts) == 1  # цена ниже той, о которой писали


def test_cooldown_is_per_rule():
    offers = [offer("X", "🇯🇵 JP", 1494), offer("X", "🇦🇺 AU", 1805)]
    state = {key("X", "🇯🇵 JP"): prev(1580), key("X", "🇦🇺 AU"): prev(1805)}
    (a,) = run(offers, state, alerted={(key("X", "🇯🇵 JP"), RULE_GAP): Decimal(1494)}).alerts
    assert a.rules == (RULE_DROP,)


def test_first_run_has_no_alerts():
    offers = [offer("X", "🇯🇵 JP", 900), offer("X", "🇺🇸 US", 1000), offer("Y", "🇺🇸 US", 10)]
    ev = run(offers, first_run=True)
    assert ev.alerts == [] and ev.first_run
    assert len(ev.positions) == 3 and list(ev.gaps) == [key("X", "🇯🇵 JP")]
    updates, _ = next_state({}, ev, set(), NOW)
    assert len(updates) == 3


def test_cap_keeps_strongest():
    offers, state = [], {}
    for i in range(20):
        offers.append(offer(f"P{i}", "🇺🇸 US", 1000 - 50 - i))
        state[key(f"P{i}", "🇺🇸 US")] = prev(1000)
    ev = run(offers, state)
    top, rest = split_by_cap(ev.alerts, 15)
    assert len(top) == 15 and len(rest) == 5
    assert [a.strength for a in top] == sorted((a.strength for a in ev.alerts), reverse=True)[:15]
    assert top[0].position.title == "P19" and rest[-1].position.title == "P0"
    assert split_by_cap(ev.alerts[:3], 15) == (ev.alerts[:3], [])


def test_unsent_alert_position_keeps_old_state():
    offers = [offer("X", "🇺🇸 US", 900), offer("Y", "🇺🇸 US", 500)]
    state = {key("X", "🇺🇸 US"): prev(1000), key("Y", "🇺🇸 US"): prev(500)}
    ev = run(offers, state)
    updates, _ = next_state(state, ev, {key("X", "🇺🇸 US")}, NOW)
    assert [u.title for u in updates] == ["Y"]
    # в следующем цикле тот же алерт посчитается снова
    assert len(run(offers, state).alerts) == 1


# --- второе предложение --------------------------------------------------------------

def test_second_offer_other_region():
    product = group([offer("X", "🇯🇵 JP", 1494), offer("X", "🇦🇺 AU", 1805), offer("X", "🇭🇰 HK", 1900)])
    ((_, p),) = product.items()
    assert second_offer(p, p.positions[0]).region == "🇦🇺 AU"
    assert second_offer(p, p.positions[1]).region == "🇯🇵 JP"


def test_second_offer_same_region_other_row():
    product = group([offer("iPad", "🇺🇸 US", 631, qty=5, eta="08 Oct"), offer("iPad", "🇺🇸 US", 681, qty=30, eta="17 Oct"),
                     offer("iPad", "🇯🇵 JP", 700)])
    ((_, p),) = product.items()
    s = second_offer(p, p.positions[0])
    assert (s.region, s.price_usd, s.eta_raw) == ("🇺🇸 US", Decimal(681), "17 Oct")


def test_second_offer_none():
    product = group([offer("X", "🇺🇸 US", 100)])
    ((_, p),) = product.items()
    assert second_offer(p, p.positions[0]) is None


def test_best_row_tie_prefers_bigger_qty():
    product = group([offer("X", "🇺🇸 US", 100, qty=1), offer("X", "🇺🇸 US", 100, qty=7)])
    ((_, p),) = product.items()
    assert p.positions[0].best.qty == 7
    assert second_offer(p, p.positions[0]).qty == 1


# --- фильтры --------------------------------------------------------------------------

def test_filters_before_grouping():
    offers = [
        offer("X", "🇺🇸 US", 631, eta_date=date(2026, 10, 8)),
        offer("X", "🇺🇸 US", 600, eta_date=date(2026, 11, 30)),  # в пути далеко
        offer("X", "🇯🇵 JP", 5, eta_date=date(2026, 10, 8)),  # дешевле порога
    ]
    kept = apply_filters(offers, date(2026, 10, 8), eta_max_days=10, min_price=Decimal(10))
    assert [o.price_usd for o in kept] == [Decimal(631)]
    assert len(apply_filters(offers, date(2026, 10, 8), None, Decimal(0))) == 3
    ev = run(kept)
    (pos,) = ev.positions.values()
    assert pos.price == Decimal(631) and second_offer(next(iter(ev.products.values())), pos) is None


def test_custom_thresholds():
    th = Thresholds(drop_pct=Decimal(10), drop_abs=Decimal(1000))
    ev = run([offer("X", "🇺🇸 US", 920)], {key("X", "🇺🇸 US"): prev(1000)}, th=th)
    assert ev.alerts == []
