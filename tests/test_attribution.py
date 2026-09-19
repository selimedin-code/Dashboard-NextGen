"""Policy benchmark, Brinson attribution, trading alpha, risk budget, journal,
cadence — the closed-loop additions."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app import reviews as rv
from app.attribution import load_periods, rebuild_attribution, rollups
from app.ingest.commit import commit_snapshot
from app.ingest.parser import ParsedHolding
from app.ingest.seed import SeedFormatError, parse_target_weight
from app.models import NavPoint, Pillar, PriceHistoryCache, QuoteCache, ReviewLog, Security
from app.performance import (
    build_performance,
    component_return,
    load_policy,
    policy_series,
    trading_alpha,
)
from app.risk_budget import ai_complex_rule, monthly_loss_rule

D0, D1 = date(2026, 7, 1), date(2026, 7, 31)


def _h(t, u, c, cash=False):
    return ParsedHolding(raw_ticker=f"{t} US", ticker=t, exchange="US",
                         units=Decimal(str(u)), avg_cost=Decimal(str(c)),
                         isin=None, is_cash=cash)


def _series(session, ticker, pairs):
    row = session.get(PriceHistoryCache, ticker)
    data = [[d, str(c)] for d, c in pairs]
    if row is None:
        session.add(PriceHistoryCache(ticker=ticker, series=data, fetched_at=datetime.now(timezone.utc)))
    else:
        row.series = data


def _commit(session, as_of, holdings):
    return commit_snapshot(holdings=holdings, as_of=as_of, filename="f", notes=None,
                           pillar_assignments=None, session=session)


def _scenario(session):
    """Semis (SMH, 60% target) and Neo (no ETF -> EW basket, 40%). Snapshot A
    holds AAA/BBB (Semis), CCC (Neo) and cash; B adds 5 AAA at 11."""
    session.add_all([
        Pillar(id="P01", name="Semis", primary_etf="SMH", target_weight=Decimal("0.6")),
        Pillar(id="P03", name="Neo", alt_etf="SKYY", target_weight=Decimal("0.4")),
    ])
    session.commit()
    _commit(session, D0, [_h("AAA", 10, 10), _h("BBB", 10, 10), _h("CCC", 10, 10),
                          _h("USD Cash", 100, 1, cash=True)])
    for t, pid, name in [("AAA", "P01", "Semis"), ("BBB", "P01", "Semis"), ("CCC", "P03", "Neo")]:
        s = session.get(Security, t)
        s.pillar_id, s.pillar = pid, name
    mid = "2026-07-15"
    _series(session, "AAA", [(D0.isoformat(), 10), (mid, 11), (D1.isoformat(), 12)])
    _series(session, "BBB", [(D0.isoformat(), 10), (mid, 10), (D1.isoformat(), 9)])
    _series(session, "CCC", [(D0.isoformat(), 10), (mid, 10), (D1.isoformat(), 11)])
    _series(session, "SMH", [(D0.isoformat(), 100), (mid, 102), (D1.isoformat(), 105)])
    session.commit()
    # 15 AAA at a blended 155/15: the implied add price is 11.
    _commit(session, D1, [_h("AAA", 15, Decimal("155") / 15), _h("BBB", 10, 10), _h("CCC", 10, 10),
                          _h("USD Cash", 45, 1, cash=True)])


# ---- policy benchmark ----

def test_policy_normalizes_and_baskets(session):
    session.add_all([
        Pillar(id="P01", name="Semis", primary_etf="SMH", target_weight=Decimal("0.45")),
        Pillar(id="P03", name="Neo", alt_etf="SKYY", target_weight=Decimal("0.45")),
        Pillar(id="P05", name="Soft", primary_etf="IGV"),              # no target
    ])
    session.commit()
    pol = load_policy(session)
    assert pol.raw_total == Decimal("0.90")
    assert [c.pillar_id for c in pol.components] == ["P01", "P03"]
    assert all(c.weight == Decimal("0.5") for c in pol.components)
    neo = pol.components[1]
    assert neo.etf is None and neo.fallback == "SKYY"


def test_basket_falls_back_to_alt_etf():
    from app.performance import PolicyComponent
    c = PolicyComponent("P03", "Neo", Decimal("1"), Decimal("1"), basket=["X"], fallback="SKYY")
    hist = {"X": [], "SKYY": [("2026-07-01", Decimal("10")), ("2026-07-31", Decimal("11"))]}.get
    assert component_return(c, "2026-07-01", "2026-07-31", lambda s: hist(s) or []) == Decimal("0.1")


def test_policy_series_rebalances_daily(session):
    session.add_all([Pillar(id="P01", name="A", primary_etf="E1", target_weight=Decimal("0.5")),
                     Pillar(id="P02", name="B", primary_etf="E2", target_weight=Decimal("0.5"))])
    _series(session, "E1", [("2026-07-01", 100), ("2026-07-02", 110), ("2026-07-03", 110)])
    _series(session, "E2", [("2026-07-01", 100), ("2026-07-02", 100), ("2026-07-03", 90)])
    session.commit()
    s = policy_series(session)
    assert [d for d, _ in s] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    # day 2: +5% ; day 3: 0.5*0 + 0.5*(-10%) = -5%  -> 1.05 * 0.95
    assert s[-1][1] == Decimal("1.05") * Decimal("0.95")


def test_performance_measures_against_policy(session):
    session.add(Pillar(id="P01", name="A", primary_etf="E1", target_weight=Decimal("1")))
    _series(session, "E1", [("2026-07-01", 100), ("2026-07-31", 104)])
    _series(session, "QQQ", [("2026-07-01", 100), ("2026-07-31", 101)])
    _series(session, "SPY", [("2026-07-01", 100), ("2026-07-31", 101)])
    session.add_all([NavPoint(as_of=D0, nav_per_share=Decimal("1000")),
                     NavPoint(as_of=D1, nav_per_share=Decimal("1050"))])
    session.commit()
    v = build_performance(session)
    assert v.returns["POLICY"] == Decimal("0.04")
    assert v.kpis["ref_label"] == "Policy"
    assert any(ln["is_policy"] for ln in v.chart["lines"])


def test_parse_target_weight():
    assert parse_target_weight("22.5%") == Decimal("0.225")
    assert parse_target_weight("22.5") == Decimal("0.225")
    assert parse_target_weight("0.225") == Decimal("0.225")
    assert parse_target_weight("") is None
    with pytest.raises(SeedFormatError):
        parse_target_weight("abc")
    with pytest.raises(SeedFormatError):
        parse_target_weight("150%")


# ---- Brinson ----

def test_brinson_period_numbers_and_identity(session):
    _scenario(session)                      # the commit hook already rebuilt
    periods = load_periods(session)
    assert len(periods) == 1
    p = periods[0]
    seg = {s.name: s for s in p.segments}
    # weights at A: Semis 200/400, Neo 100/400, cash 100/400
    assert seg["Semis"].port_weight == Decimal("0.5")
    assert seg["Cash"].port_weight == Decimal("0.25")
    # R_P = .5*.05 + .25*.10 = 5% ; R_B = .6*.05 + .4*.10 = 7%
    assert p.port_return == Decimal("0.05")
    assert p.bench_return == Decimal("0.07")
    assert seg["Semis"].allocation == Decimal("0.002")          # (.5-.6)(.05-.07)
    assert seg["Neo"].allocation == Decimal("-0.0045")          # (.25-.4)(.10-.07)
    assert seg["Cash"].allocation == Decimal("-0.0175")         # cash drag, all allocation
    assert seg["Cash"].interaction == 0 and seg["Cash"].selection == 0
    assert p.selection == 0                                     # names matched their benchmarks
    assert p.excess == p.port_return - p.bench_return


def test_selection_when_names_beat_the_etf(session):
    _scenario(session)
    _series(session, "SMH", [(D0.isoformat(), 100), (D1.isoformat(), 102)])   # ETF +2%, ours +5%
    session.commit()
    rebuild_attribution(session, commit=True)
    seg = {s.name: s for s in load_periods(session)[0].segments}
    assert seg["Semis"].selection == Decimal("0.6") * Decimal("0.03")
    assert seg["Semis"].interaction == Decimal("-0.1") * Decimal("0.03")


def test_no_targets_no_periods(session):
    _commit(session, D0, [_h("AAA", 10, 10)])
    _commit(session, D1, [_h("AAA", 10, 10)])
    assert load_periods(session) == []


def test_rollups_t12m_and_quarters():
    from app.attribution import Period, Segment
    def per(d0, d1, a):
        return Period(1, 2, d0, d1, [Segment("X", Decimal(1), Decimal(1), Decimal("0.1"), Decimal("0.1") - a,
                                             a, Decimal(0), Decimal(0))])
    ps = [per(date(2025, 5, 1), date(2025, 6, 1), Decimal("0.01")),
          per(date(2026, 6, 1), date(2026, 6, 30), Decimal("0.02")),
          per(date(2026, 6, 30), date(2026, 7, 31), Decimal("0.03"))]
    rus = rollups(ps, today=date(2026, 9, 19))
    assert rus[0].key == "t12m" and len(rus[0].periods) == 2
    assert rus[0].allocation == Decimal("0.05")
    assert [r.key for r in rus[1:]] == ["2026q3", "2026q2", "2025q2"]
    # compounded excess vs summed effects -> residual is the difference
    assert rus[0].linking_residual == (rus[0].port_return - rus[0].bench_return) - Decimal("0.05")


# ---- trading alpha ----

def test_trading_alpha_discretionary_add(session):
    _scenario(session)
    session.add_all([NavPoint(as_of=D0, nav_per_share=Decimal("1000")),
                     NavPoint(as_of=D1, nav_per_share=Decimal("1060"))])
    session.commit()
    [tp] = trading_alpha(session)
    assert tp.days == 30 and not tp.wide
    assert tp.frozen_return == Decimal("0.05")            # 420/400 - 1
    [e] = tp.effects
    assert e.ticker == "AAA" and e.classification == "DISCRETIONARY"
    assert e.price_basis == "implied" and round(e.trade_price, 4) == Decimal("11")
    assert round(e.effect, 4) == Decimal("5")             # 5 x (12 - 11)
    assert round(tp.discretionary, 6) == Decimal("0.0125")
    assert round(tp.residual, 6) == Decimal("-0.0025")    # 6% - 5% - 1.25%


def test_trading_alpha_trim_uses_avg_close_estimate(session):
    _scenario(session)
    _commit(session, date(2026, 8, 31), [_h("AAA", 15, Decimal("155") / 15), _h("BBB", 4, 10),
                                         _h("CCC", 10, 10), _h("USD Cash", 45, 1, cash=True)])
    _series(session, "BBB", [(D0.isoformat(), 10), (D1.isoformat(), 9), ("2026-08-15", 8), ("2026-08-31", 7)])
    session.commit()
    tp = trading_alpha(session)[-1]
    [e] = tp.effects
    assert e.change_type == "TRIM" and e.price_basis == "avg close (est.)"
    assert e.trade_price == Decimal("8")                  # mean of 9, 8, 7
    assert e.effect == Decimal("6")                       # -6 x (7 - 8): selling early saved $6


def test_wide_interval_flagged(session):
    _commit(session, D0, [_h("AAA", 10, 10)])
    _commit(session, D0 + timedelta(days=60), [_h("AAA", 10, 10)])
    assert trading_alpha(session)[0].wide


# ---- risk budget ----

class _Risk:
    def __init__(self, fw, iw):
        self.ai_complex_fund_weight, self.ai_complex_invested_weight = fw, iw


class _Exp:
    def __init__(self, fw):
        self.total_value = Decimal("100")
        self.risk = _Risk(Decimal(fw), Decimal(fw))


def test_ai_complex_cap():
    assert ai_complex_rule(_Exp("0.70")).status == "ok"
    assert ai_complex_rule(_Exp("0.78")).status == "warn"
    r = ai_complex_rule(_Exp("0.85"))
    assert r.status == "breach" and r.headroom == Decimal("-0.05")
    assert ai_complex_rule(None).status == "unknown"


def _navs(*pairs):
    return [NavPoint(as_of=d, nav_per_share=Decimal(str(v))) for d, v in pairs]


def test_monthly_loss_trigger():
    today = date(2026, 9, 19)
    ok = monthly_loss_rule(_navs((date(2026, 8, 31), 1000), (date(2026, 9, 18), 980)), today)
    assert ok.status == "ok" and ok.value == Decimal("-0.02")
    hit = monthly_loss_rule(_navs((date(2026, 8, 31), 1000), (date(2026, 9, 18), 910)), today)
    assert hit.status == "breach"
    # a loss straddling month-end is caught by the trailing 30 days
    roll = monthly_loss_rule(_navs((date(2026, 8, 15), 1000), (date(2026, 8, 31), 930),
                                   (date(2026, 9, 18), 915)), today)
    assert roll.status == "breach" and roll.value == Decimal("-0.085")
    # a stale fund price can't vouch for the month
    stale = monthly_loss_rule(_navs((date(2026, 7, 31), 1000), (date(2026, 8, 20), 1000)), today)
    assert stale.status == "unknown"


# ---- journal ----

def _review(session, **kw):
    args = dict(note="Thesis.", price_raw="100", stance="ADD", review_date=date(2026, 6, 1), files=[])
    args.update(kw)
    return rv.create_review(session, "NVDA", **args)


def test_expected_outcome_and_horizon_go_together(session):
    with pytest.raises(rv.ReviewError):
        _review(session, expected_outcome="beats", horizon_date=None)
    with pytest.raises(rv.ReviewError):
        _review(session, expected_outcome="", horizon_date=date(2026, 9, 1))
    with pytest.raises(rv.ReviewError):
        _review(session, expected_outcome="beats", horizon_date=date(2026, 5, 1))   # before review


def test_due_list_score_and_unscore(session):
    session.add(QuoteCache(ticker="NVDA", price=Decimal("120"), ok=True))
    session.commit()
    due = _review(session, expected_outcome="DC rev > 60B", horizon_date=date(2026, 9, 1),
                  invalidation="capex cut")
    _review(session, expected_outcome="later", horizon_date=date(2026, 12, 1))
    _review(session)                                           # no call -> never due
    today = date(2026, 9, 19)
    j = rv.build_journal(session, today)
    assert [r.review.id for r in j.due] == [due.id]
    assert j.due[0].since_pct == Decimal("0.2") and j.due[0].days_overdue == 18
    assert len(j.upcoming) == 1 and rv.due_count(session, today) == 1

    with pytest.raises(rv.ReviewError):
        rv.score_review(session, due.id, "MAYBE")
    rv.score_review(session, due.id, "right_wrong", "capex, not the reason we gave")
    j = rv.build_journal(session, today)
    assert j.due == [] and j.scorecard["counts"]["RIGHT_WRONG"] == 1
    assert j.scorecard["skill_rate"] == 0 and j.scorecard["hit_rate"] == 1

    rv.unscore_review(session, due.id)
    assert rv.due_count(session, today) == 1


# ---- signals + pages ----

def test_signals_fire_for_budget_cadence_journal(session, monkeypatch):
    from app import signals
    from app.risk_budget import BudgetRule, RiskBudget
    monkeypatch.setattr("app.risk_budget.build_risk_budget", lambda s, exposure=None: RiskBudget(rules=[
        BudgetRule("ai_complex", "AI-complex cap (% of NAV)", Decimal("0.9"), Decimal("0.8"), "breach", "trim")]))
    _commit(session, date.today() - timedelta(days=40), [_h("AAA", 10, 10)])
    session.add(ReviewLog(ticker="AAA", review_date=date.today() - timedelta(days=90), note="x",
                          expected_outcome="up", horizon_date=date.today() - timedelta(days=1)))
    session.commit()
    v = signals.build_signals(session)
    assert v.budget and v.budget[0].severity == "high"
    assert v.cadence and v.cadence[0].severity == "high"       # 40d > 35d
    assert v.journal and v.journal[0].ticker == "AAA"


@pytest.fixture
def client(session):
    from fastapi.testclient import TestClient
    from app.main import app
    import base64
    tok = base64.b64encode(b"nextgen:change-me").decode()
    return TestClient(app, headers={"Authorization": f"Basic {tok}"})


def test_pages_render(session, client):
    _scenario(session)
    session.add(ReviewLog(ticker="AAA", review_date=D0, note="x", price=Decimal("10"),
                          expected_outcome="up", horizon_date=D1))
    session.get(Pillar, "P01").macro_bet = "ai_compute_supply"
    for t, px in [("AAA", 12), ("BBB", 9), ("CCC", 11)]:
        session.add(QuoteCache(ticker=t, fmp_symbol=t, price=Decimal(px), prev_close=Decimal(px),
                               day_change_pct=Decimal("0"), ok=True))
    session.add_all([NavPoint(as_of=D0, nav_per_share=Decimal("1000")),
                     NavPoint(as_of=D1, nav_per_share=Decimal("900"))])
    session.commit()
    for url in ["/", "/attribution", "/attribution?r=2026q3", "/reviews/due", "/risk",
                "/performance", "/pillars", "/signals", "/upload", "/ticker/AAA"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert "Brinson" in client.get("/attribution").text
    assert "Due now" in client.get("/reviews/due").text
    assert "Risk budget" in client.get("/risk").text


def test_targets_form_saves_percent(session, client):
    session.add(Pillar(id="P01", name="Semis", primary_etf="SMH"))
    session.commit()
    r = client.post("/pillars/targets", data={"target__P01": "22.5"}, follow_redirects=False)
    assert r.status_code == 303
    session.expire_all()
    assert session.get(Pillar, "P01").target_weight == Decimal("0.225")
    client.post("/pillars/targets", data={"target__P01": ""})
    session.expire_all()
    assert session.get(Pillar, "P01").target_weight is None
