from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import get_db, get_current_user, User
from sports_models import Fixture, Prediction, OddsInsight, AutomationState
from sync_trends import load_fixture_trends


router = APIRouter(prefix="/picks", tags=["picks"])

UPCOMING_STATUSES = {"NS", "TBD", "NOT_STARTED", "SCHEDULED", "TIMED", ""}

SAFE_MARKET_LABELS = {
    "OVER_05": "Over 0.5 Goals", "OVER_15": "Over 1.5 Goals", "OVER_25": "Over 2.5 Goals",
    "OVER_35": "Over 3.5 Goals", "OVER_45": "Over 4.5 Goals",
    "UNDER_15": "Under 1.5 Goals", "UNDER_25": "Under 2.5 Goals",
    "UNDER_35": "Under 3.5 Goals", "UNDER_45": "Under 4.5 Goals",
    "ODD_GOALS": "Odd Number Of Goals", "EVEN_GOALS": "Even Number Of Goals",
    "HOME_O25": "Home Win & Over 2.5", "AWAY_O25": "Away Win & Over 2.5",
    "HOME_GG": "Home Win & BTTS", "AWAY_GG": "Away Win & BTTS",
    "HT_OVER_05": "1st Half Over 0.5 Goals", "HT_OVER_15": "1st Half Over 1.5 Goals",
    "DRAW": "Draw",
    "BTTS_YES": "Both Teams To Score - Yes", "BTTS_NO": "Both Teams To Score - No",
    "BTTS_O25": "BTTS & Over 2.5 Goals",
    "DC_1X": "Home or Draw", "DC_X2": "Away or Draw", "DC_12": "Any Team To Win",
    "HOME_DNB": "Home Draw No Bet", "AWAY_DNB": "Away Draw No Bet",
    "HOME_OVER_15": "Home Over 1.5 Goals", "AWAY_OVER_15": "Away Over 1.5 Goals",
    "HOME_OVER_05": "Home To Score", "AWAY_OVER_05": "Away To Score",
    "HOME_OVER_25": "Home Over 2.5 Goals", "AWAY_OVER_25": "Away Over 2.5 Goals",
    "WIN_TO_NIL_HOME": "Home Win To Nil", "WIN_TO_NIL_AWAY": "Away Win To Nil",
    "HOME_MULTI_LOW": "Home Multiscore (1-0, 2-0)", "HOME_MULTI_HIGH": "Home Multiscore (2-1, 3-1)",
    "AWAY_MULTI_LOW": "Away Multiscore (0-1, 0-2)", "AWAY_MULTI_HIGH": "Away Multiscore (1-2, 1-3)",
    "DRAW_MULTI": "Draw Multiscore (1-1, 2-2)",
    "HOME_MARGIN_2": "Home Win By 2+ Goals", "AWAY_MARGIN_2": "Away Win By 2+ Goals",
    "HT_HOME_MULTI": "HT Lead - Home (1-0, 2-0)", "HT_AWAY_MULTI": "HT Lead - Away (0-1, 0-2)",
    "HT_DRAW_MULTI": "HT Draw (0-0, 1-1)",
    "HOME_WIN": "Home Win", "AWAY_WIN": "Away Win",
    "CS": "Correct Score", "HTFT": "HT/FT",
}

# FREE: high hit-rate markets. Free users win with these and trust you.
FREE_MARKETS = {
    "OVER_05", "OVER_15", "OVER_25", "UNDER_15", "UNDER_25", "UNDER_35", "UNDER_45",
    "BTTS_YES", "BTTS_NO",
    "HOME_WIN", "AWAY_WIN", "DRAW",
    "DC_1X", "DC_X2", "DC_12",
    "HOME_DNB", "AWAY_DNB",
    "HOME_OVER_15", "AWAY_OVER_15",
    "HOME_OVER_05", "AWAY_OVER_05",
    "HT_OVER_05", "HT_OVER_15",
}

# PREMIUM: specialised high-odds markets (HT options, multiscores, combos, CS, HTFT)
PREMIUM_MARKETS = {
    "BTTS_O25", "WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY",
    "HOME_MULTI_LOW", "HOME_MULTI_HIGH",
    "AWAY_MULTI_LOW", "AWAY_MULTI_HIGH",
    "DRAW_MULTI", "HOME_MARGIN_2", "AWAY_MARGIN_2",
    "HT_HOME_MULTI", "HT_AWAY_MULTI", "HT_DRAW_MULTI",
    "HOME_OVER_25", "AWAY_OVER_25",
    "HOME_O25", "AWAY_O25", "HOME_GG", "AWAY_GG",
    "ODD_GOALS", "EVEN_GOALS", "OVER_45",
    "CS", "HTFT",
}

HIGH_ODDS_ONLY = PREMIUM_MARKETS - {"CS", "HTFT"}

BOARD_LIMIT_SAFE = 10
BOARD_LIMIT_HIGH_ODDS = 4   # legacy pool selector (fetch_high_odds); not part of the daily deal
BOARD_LIMIT_CS = 2
BOARD_LIMIT_HTFT = 2

# ---------------------------------------------------------------- premium deal tiers
# The daily premium card is tiered by how markets actually SURVIVE (pool audit
# Aug 2026): BTTS & Over 2.5 lands ~6/10, home-side attack bands ~1/4, true
# lotteries ~1/10. Away-side multis/margins, win-to-nil and HT combos stay in
# the engine pool but are NEVER dealt daily — 7% commodities are not a product.
PREMIUM_DEAL_CORE = {"BTTS_O25"}                            # value core, lands often
PREMIUM_DEAL_EDGE = {"HOME_MARGIN_2", "HOME_MULTI_HIGH"}    # attack tier, spicy
PREMIUM_DEAL_LOTTERY = {"CS", "HTFT"}                       # drama tier, 1 a day

DEALT_TIER = {
    **{m: "CORE" for m in PREMIUM_DEAL_CORE},
    **{m: "EDGE" for m in PREMIUM_DEAL_EDGE},
    **{m: "LOTTERY" for m in PREMIUM_DEAL_LOTTERY},
}

BONUS_LIMIT_CORE = 3
BONUS_LIMIT_EDGE = 1
BONUS_LIMIT_LOTTERY = 1

ACCA_LEG_COUNT = 2   # Acca of the Day = top banker legs combined


def format_kickoff(dt):
    if not dt:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.isoformat()


def merge_selected(existing, new_rows, limit, max_per_fixture=1, max_per_market=None):
    selected = list(existing)
    fixture_counts = {}
    market_counts = {}
    excluded_ids = set()

    for pred, fixture in selected:
        excluded_ids.add(pred.id)
        fixture_counts[fixture.id] = fixture_counts.get(fixture.id, 0) + 1
        market_counts[pred.market] = market_counts.get(pred.market, 0) + 1

    for pred, fixture in new_rows:
        if len(selected) >= limit:
            break
        if pred.id in excluded_ids:
            continue
        if fixture_counts.get(fixture.id, 0) >= max_per_fixture:
            continue
        if max_per_market and market_counts.get(pred.market, 0) >= max_per_market:
            continue

        selected.append((pred, fixture))
        excluded_ids.add(pred.id)
        fixture_counts[fixture.id] = fixture_counts.get(fixture.id, 0) + 1
        market_counts[pred.market] = market_counts.get(pred.market, 0) + 1

    return selected


# ---------------------------------------------------------------- trend chips ("why this pick")
# Which scores24 trend markets support which dealt market on a card. Display
# context only — chips never decide what gets dealt (that stays pure argmax).
_TREND_OVERS = {"OVER_15", "OVER_25", "OVER_35"}
_TREND_UNDERS = {"UNDER_15", "UNDER_25", "UNDER_35", "UNDER_45"}

TREND_SUPPORT = {
    "HOME_WIN": {"HOME_WIN", "DC_1X"},
    "AWAY_WIN": {"AWAY_WIN", "DC_X2"},
    "DRAW": {"DRAW"},
    "DC_1X": {"DC_1X", "HOME_WIN", "DRAW"},
    "DC_X2": {"DC_X2", "AWAY_WIN", "DRAW"},
    "DC_12": {"DC_12", "HOME_WIN", "AWAY_WIN"},
    "HOME_DNB": {"HOME_WIN", "DC_1X"},
    "AWAY_DNB": {"AWAY_WIN", "DC_X2"},
    "OVER_05": _TREND_OVERS, "OVER_15": _TREND_OVERS, "OVER_25": _TREND_OVERS | {"BTTS_YES"},
    "OVER_35": _TREND_OVERS, "OVER_45": _TREND_OVERS,
    "UNDER_15": _TREND_UNDERS, "UNDER_25": _TREND_UNDERS,
    "UNDER_35": _TREND_UNDERS, "UNDER_45": _TREND_UNDERS,
    "BTTS_YES": {"BTTS_YES"}, "BTTS_NO": {"BTTS_NO"},
    "BTTS_O25": {"BTTS_YES", "OVER_25", "OVER_35"},
    "HOME_OVER_05": {"HOME_OVER_05", "HOME_OVER_15"},
    "HOME_OVER_15": {"HOME_OVER_05", "HOME_OVER_15", "HOME_OVER_25"},
    "HOME_OVER_25": {"HOME_OVER_15", "HOME_OVER_25"},
    "AWAY_OVER_05": {"AWAY_OVER_05", "AWAY_OVER_15"},
    "AWAY_OVER_15": {"AWAY_OVER_05", "AWAY_OVER_15", "AWAY_OVER_25"},
    "AWAY_OVER_25": {"AWAY_OVER_15", "AWAY_OVER_25"},
    "HOME_O25": {"HOME_WIN", "HOME_OVER_15", "OVER_25"},
    "AWAY_O25": {"AWAY_WIN", "AWAY_OVER_15", "OVER_25"},
    "HOME_GG": {"HOME_WIN", "BTTS_YES"},
    "AWAY_GG": {"AWAY_WIN", "BTTS_YES"},
    "HOME_MARGIN_2": {"HOME_WIN"}, "AWAY_MARGIN_2": {"AWAY_WIN"},
    "HOME_MULTI_HIGH": {"HOME_WIN", "BTTS_YES"},
    "AWAY_MULTI_HIGH": {"AWAY_WIN", "BTTS_YES"},
    "HT_OVER_05": {"HT_OVER_05", "HT_OVER_15"},
    "HT_OVER_15": {"HT_OVER_05", "HT_OVER_15"},
}


def trend_chips_for(db: Session, fixture_id: int, market: str, limit: int = 2):
    """Best streak chips supporting a dealt pick market, ready to render."""
    support = TREND_SUPPORT.get(market)
    if not support:
        return []
    trends = load_fixture_trends(db, fixture_id)
    picked = [t for t in trends if t.get("m") in support and t.get("chip")]
    picked.sort(key=lambda t: t.get("score", 0.0), reverse=True)
    return [{"chip": t["chip"], "odd": t.get("odd")} for t in picked[:limit]]


def serialize_pick(pred: Prediction, fixture: Fixture, db: Session, locked: bool = False, tier=None):
    if locked:
        return {
            "prediction_id": pred.id,
            "fixture_id": fixture.id,
            "league": fixture.league,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "kickoff": format_kickoff(fixture.kickoff),
            "status": "Upcoming",
            "market": "LOCKED",
            "market_label": "Premium Pick",
            "pick": "Locked",
            "probability": None,
            "confidence_score": None,
            "confidence_label": "Locked",
            "locked": True,
            "is_value_bet": False,
            "market_edge": 0,
            "trends": [],
            "tier": tier,
        }

    insight = None
    try:
        insight = db.query(OddsInsight).filter(
            OddsInsight.fixture_id == fixture.id,
            OddsInsight.market == pred.market,
            OddsInsight.is_value_bet == True,
        ).first()
    except Exception:
        pass

    return {
        "prediction_id": pred.id,
        "fixture_id": fixture.id,
        "league": fixture.league,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "kickoff": format_kickoff(fixture.kickoff),
        "status": "Upcoming",
        "market": pred.market,
        "market_label": SAFE_MARKET_LABELS.get(pred.market, pred.market),
        "pick": pred.pick,
        "probability": pred.probability,
        "confidence_score": pred.confidence_score,
        "confidence_label": pred.confidence_label,
        "locked": False,
        "is_value_bet": bool(insight),
        "market_edge": round(insight.value_edge * 100, 1) if insight else 0,
        "trends": trend_chips_for(db, fixture.id, pred.market, limit=2),
        "tier": tier,
    }


def _build_acca(free_rows, locked: bool):
    """Acca of the Day: the card's top banker legs combined into one product.
    Served only while every leg is still playable (pending + upcoming) —
    once a leg settles, the acca retires to the record (history.py card-record).
    Free users see the box and the combined probability; the legs stay premium."""
    if len(free_rows) < ACCA_LEG_COUNT:
        return None
    legs = free_rows[:ACCA_LEG_COUNT]
    combined = 1.0
    for pred, _ in legs:
        combined *= (pred.probability or 0.0)
    combined = round(combined, 4)

    if locked:
        return {
            "kind": "ACCA",
            "locked": True,
            "leg_count": ACCA_LEG_COUNT,
            "combined_probability": combined,
            "legs": None,
        }
    return {
        "kind": "ACCA",
        "locked": False,
        "leg_count": ACCA_LEG_COUNT,
        "combined_probability": combined,
        "legs": [
            {
                "home_team": fixture.home_team,
                "away_team": fixture.away_team,
                "league": fixture.league,
                "kickoff": format_kickoff(fixture.kickoff),
                "market": pred.market,
                "market_label": SAFE_MARKET_LABELS.get(pred.market, pred.market),
                "pick": pred.pick,
                "probability": pred.probability,
            }
            for pred, fixture in legs
        ],
    }


# ---------------------------------------------------------------- day-window selection
# "Today" = the Africa/Lagos calendar day (UTC+1, no DST) — the same day the
# frontend TODAY header shows users. Busy days (weekends) fill the card with
# today's games only; quiet days top up from the week's best so the board
# never looks abandoned. The card itself is still frozen at the 07:00 sync.
LAGOS_OFFSET = timedelta(hours=1)


def lagos_today_window():
    """(start, end) as naive UTC datetimes covering the Lagos calendar day."""
    now_utc = datetime.now(timezone.utc)
    lagos_now = now_utc + LAGOS_OFFSET
    d = lagos_now.date()
    midnight_lagos_utc = datetime(d.year, d.month, d.day) - LAGOS_OFFSET
    return midnight_lagos_utc, midnight_lagos_utc + timedelta(days=1)


def board_cutoff():
    # fixtures are stored as naive UTC — keep comparisons naive
    return (datetime.now(timezone.utc) - timedelta(hours=6)).replace(tzinfo=None)


def _free_candidates(db: Session, kickoff_start=None, kickoff_end=None):
    q = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Fixture.status.in_(UPCOMING_STATUSES),
            Prediction.market.in_(FREE_MARKETS),
            Prediction.status == "pending",
            Prediction.confidence_score >= 60,
            Prediction.probability <= 0.90,
        )
    )
    if kickoff_start is not None:
        q = q.filter(Fixture.kickoff >= kickoff_start, Fixture.kickoff < kickoff_end)
    else:
        q = q.filter(Fixture.kickoff >= board_cutoff())
    return q.order_by(Prediction.probability.desc()).limit(200).all()


def fetch_free_picks(db: Session, limit: int = BOARD_LIMIT_SAFE):
    return merge_selected([], _free_candidates(db), limit, max_per_fixture=1, max_per_market=3)


def _high_odds_candidates(db: Session, kickoff_start=None, kickoff_end=None):
    q = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Fixture.status.in_(UPCOMING_STATUSES),
            Prediction.market.in_(HIGH_ODDS_ONLY),
            Prediction.status == "pending",
        )
    )
    if kickoff_start is not None:
        q = q.filter(Fixture.kickoff >= kickoff_start, Fixture.kickoff < kickoff_end)
    else:
        q = q.filter(Fixture.kickoff >= board_cutoff())
    return q.order_by(Prediction.probability.desc()).limit(120).all()


def fetch_high_odds(db: Session, limit: int = BOARD_LIMIT_HIGH_ODDS):
    return merge_selected([], _high_odds_candidates(db), limit, max_per_fixture=1, max_per_market=1)


BONUS_MIN_PROB = {"CS": 0.10, "HTFT": 0.10}
LOTTERY_MIN_PROB = 0.10


def _dealt_candidates(db: Session, markets, min_prob: float = 0.0, kickoff_start=None, kickoff_end=None):
    q = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Fixture.status.in_(UPCOMING_STATUSES),
            Prediction.market.in_(markets),
            Prediction.status == "pending",
            Prediction.probability >= min_prob,
        )
    )
    if kickoff_start is not None:
        q = q.filter(Fixture.kickoff >= kickoff_start, Fixture.kickoff < kickoff_end)
    else:
        q = q.filter(Fixture.kickoff >= board_cutoff())
    return q.order_by(Prediction.probability.desc()).limit(60).all()


def fetch_bonus(db: Session, market: str, limit: int = 2):
    return merge_selected(
        [],
        _dealt_candidates(db, {market}, BONUS_MIN_PROB.get(market, 0.0)),
        limit,
        max_per_fixture=1,
    )


def select_today_first(db: Session, candidates_fn, limit, merge_kwargs, *args):
    """Stage A: today's games. Stage B (only if short): the week's best.
    Today rows keep priority — the week never outranks today's card."""
    start, end = lagos_today_window()
    window_start = max(start, board_cutoff())
    today_rows = merge_selected([], candidates_fn(db, *args, kickoff_start=window_start, kickoff_end=end), limit, **merge_kwargs)
    if len(today_rows) >= limit:
        return today_rows
    return merge_selected(today_rows, candidates_fn(db, *args), limit, **merge_kwargs)


def select_safe_rows(db: Session):
    return select_today_first(db, _free_candidates, BOARD_LIMIT_SAFE, {"max_per_fixture": 1, "max_per_market": 3})


def select_high_odds_rows(db: Session):
    return select_today_first(db, _high_odds_candidates, BOARD_LIMIT_HIGH_ODDS, {"max_per_fixture": 1, "max_per_market": 1})


def select_dealt_rows(db: Session, markets, limit: int, min_prob: float = 0.0):
    return select_today_first(
        db, _dealt_candidates, limit, {"max_per_fixture": 1}, markets, min_prob
    )


def select_bonus_market_rows(db: Session, market: str, limit: int):
    # legacy shim: older callers pass a single market string
    return select_dealt_rows(db, {market}, limit, BONUS_MIN_PROB.get(market, 0.0))


def select_bonus_rows(db: Session):
    """The daily premium card, tiered by survivability:
    3x value core (BTTS & Over 2.5) + 1x attack (home margin/score band)
    + 1x clearly-labeled lottery (best of CS/HTFT)."""
    return (
        select_dealt_rows(db, PREMIUM_DEAL_CORE, BONUS_LIMIT_CORE)
        + select_dealt_rows(db, PREMIUM_DEAL_EDGE, BONUS_LIMIT_EDGE)
        + select_dealt_rows(db, PREMIUM_DEAL_LOTTERY, BONUS_LIMIT_LOTTERY, LOTTERY_MIN_PROB)
    )


# ---------------------------------------------------------------- daily board pin ("print the card once")
# At the 07:00 sync the ranked selection is frozen (pinned) into AutomationState.
# /picks/today serves the pinned card and it can only SHRINK during the day:
# matches leave when they kick off / settle (they move to RESULTS); no new
# faces appear, no re-ranking. A fresh card is printed with the next daily sync.
PIN_PREFIX = "boardpin"


def _pin_key(kind: str, day: date):
    return f"{PIN_PREFIX}:{kind}:{day.isoformat()}"


def store_board_pins(db: Session, day: date):
    """Freeze today's ranked board. Write-once per day: never re-deals intra-day."""
    already = db.query(AutomationState).filter(
        AutomationState.state_key == _pin_key("safe", day)
    ).first()
    if already:
        return "exists"

    safe_ids = [p.id for p, _ in select_safe_rows(db)]
    bonus_ids = [p.id for p, _ in select_bonus_rows(db)]

    if not safe_ids and not bonus_ids:
        # Nothing to pin (e.g. data outage day): leave unpinned so the live
        # fallback keeps serving whatever the pool has instead of freezing emptiness.
        return "skipped-empty"

    payload = {"safe": ",".join(str(i) for i in safe_ids), "bonus": ",".join(str(i) for i in bonus_ids)}
    for kind, value in payload.items():
        db.add(AutomationState(state_key=_pin_key(kind, day), state_value=value if value else "-"))

    # tidy: pins are the permanent audit trail (they anchor both the public
    # receipts AND the dealt-card performance record), so keep them long.
    # ~2 tiny rows/day — 180 days costs nothing.
    # NOTE: compare parsed DAYS, never raw key strings — "boardpin:bonus:..."
    # sorts before "boardpin:safe:..." alphabetically ('b' < 's'), so a naive
    # string cutoff silently deletes EVERY bonus pin at the next sync.
    cutoff_day = day - timedelta(days=180)
    for row in db.query(AutomationState).filter(AutomationState.state_key.like(f"{PIN_PREFIX}:%")).all():
        try:
            row_day = date.fromisoformat(row.state_key.rsplit(":", 1)[1])
        except Exception:
            continue
        if row_day < cutoff_day:
            db.delete(row)

    db.commit()
    return "pinned"


def load_board_pins(db: Session, day: date):
    keys = [_pin_key("safe", day), _pin_key("bonus", day)]
    rows = db.query(AutomationState).filter(AutomationState.state_key.in_(keys)).all()
    if not rows:
        return None

    pins = {"safe": None, "bonus": None}
    for row in rows:
        kind = row.state_key.split(":")[1]
        value = (row.state_value or "").strip()
        ids = []
        if value and value != "-":
            for token in value.split(","):
                token = token.strip()
                if token.isdigit():
                    ids.append(int(token))
        pins[kind] = ids

    if pins["safe"] is None or pins["bonus"] is None:
        return None
    return pins


def fetch_pinned(db: Session, ids):
    """Pinned picks still eligible for the board: pending, upcoming, not kicked off.
    Order stays exactly as pinned — no re-ranking."""
    if not ids:
        return []

    rows = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Prediction.id.in_(ids),
            Fixture.status.in_(UPCOMING_STATUSES),
            Prediction.status == "pending",
            Fixture.kickoff >= board_cutoff(),
        )
        .all()
    )

    order = {pid: idx for idx, pid in enumerate(ids)}
    rows.sort(key=lambda pf: order.get(pf[0].id, 10 ** 9))
    return rows


@router.get("/today")
def picks_today(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    pins = None
    try:
        pins = load_board_pins(db, date.today())
    except Exception:
        pins = None

    if pins is not None:
        free_rows = fetch_pinned(db, pins["safe"])
        # Acca of the Day: the top banker legs, only while every leg stays playable
        acca_rows = fetch_pinned(db, pins["safe"][:ACCA_LEG_COUNT])
        bonus_rows = fetch_pinned(db, pins["bonus"])
    else:
        # no card pinned today (pre-first-sync / degraded day): live fallback, still today-first
        free_rows = select_safe_rows(db)
        acca_rows = free_rows[:ACCA_LEG_COUNT]
        bonus_rows = select_bonus_rows(db)

    acca = _build_acca(acca_rows, locked=not user.is_premium)

    if user.is_premium:
        return {
            "plan": user.plan,
            "safe_picks": [serialize_pick(p, f, db) for p, f in free_rows],
            "bonus_picks": [serialize_pick(p, f, db, tier=DEALT_TIER.get(p.market)) for p, f in bonus_rows],
            "acca": acca,
        }

    # Free users: all free-market picks unlocked, premium section locked
    return {
        "plan": user.plan,
        "safe_picks": [serialize_pick(p, f, db) for p, f in free_rows],
        "bonus_picks": [serialize_pick(p, f, db, locked=True, tier=DEALT_TIER.get(p.market)) for p, f in bonus_rows],
        "acca": acca,
    }


# ---------------------------------------------------------------- fixture options browser
# SportyBet-style per-match menu: every priced option for one fixture, grouped
# the way a bettor reads a coupon. Gating is per-row: commoditized staples are
# free; the specialised families the premium card deals from stay premium.
OPTION_GROUPS = [
    ("1x2",     "1X2",                  ["HOME_WIN", "DRAW", "AWAY_WIN"]),
    ("dc",      "Double Chance",        ["DC_1X", "DC_12", "DC_X2"]),
    ("dnb",     "Draw No Bet",          ["HOME_DNB", "AWAY_DNB"]),
    ("goals",   "Goals Over / Under",   ["OVER_05", "OVER_15", "OVER_25", "OVER_35", "OVER_45",
                                         "UNDER_15", "UNDER_25", "UNDER_35", "UNDER_45"]),
    ("team",    "Team Goals",           ["HOME_OVER_05", "AWAY_OVER_05",
                                         "HOME_OVER_15", "AWAY_OVER_15",
                                         "HOME_OVER_25", "AWAY_OVER_25"]),
    ("btts",    "Both Teams To Score",  ["BTTS_YES", "BTTS_NO", "BTTS_O25"]),
    ("combo",   "Combos",               ["HOME_O25", "AWAY_O25", "HOME_GG", "AWAY_GG"]),
    ("multi",   "Margins & Multiscore", ["HOME_MARGIN_2", "AWAY_MARGIN_2",
                                         "HOME_MULTI_LOW", "HOME_MULTI_HIGH",
                                         "AWAY_MULTI_LOW", "AWAY_MULTI_HIGH",
                                         "DRAW_MULTI", "WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY"]),
    ("half",    "1st Half",             ["HT_OVER_05", "HT_OVER_15",
                                         "HT_HOME_MULTI", "HT_AWAY_MULTI", "HT_DRAW_MULTI"]),
    ("oddeven", "Odd / Even Goals",     ["ODD_GOALS", "EVEN_GOALS"]),
    ("cs",      "Correct Score",        ["CS"]),
    ("htft",    "HT / FT",              ["HTFT"]),
]

# Staples that are exact complements of other stored rows — computed on the fly
# so fixtures created BEFORE these markets existed still show a full menu.
# (No DB row is written; derived rows are never "dealt" and carry no receipt.)
def _clamp01(x):
    return max(0.0, min(1.0, x))

DERIVED_STAPLES = {
    "DRAW":     lambda m: _clamp01(1.0 - m.get("HOME_WIN", 0.0) - m.get("AWAY_WIN", 0.0)),
    "UNDER_15": lambda m: _clamp01(1.0 - m["OVER_15"]) if "OVER_15" in m else None,
    "UNDER_25": lambda m: _clamp01(1.0 - m["OVER_25"]) if "OVER_25" in m else None,
    "OVER_45":  lambda m: _clamp01(1.0 - m["UNDER_45"]) if "UNDER_45" in m else None,
}


def _dealt_labels(db: Session):
    """pred_id -> display tag for every pick pinned on today's card."""
    labels = {}
    try:
        pins = load_board_pins(db, date.today())
    except Exception:
        pins = None
    if not pins:
        return labels
    safe_ids = pins.get("safe") or []
    bonus_ids = pins.get("bonus") or []
    for pid in safe_ids:
        labels[pid] = "BANKER"
    for pid in safe_ids[:ACCA_LEG_COUNT]:
        labels[pid] = "ACCA LEG"
    if bonus_ids:
        rows = db.query(Prediction).filter(Prediction.id.in_(bonus_ids)).all()
        tier_label = {"CORE": "VALUE CORE", "EDGE": "ATTACK TIER", "LOTTERY": "LOTTERY"}
        for row in rows:
            labels[row.id] = tier_label.get(DEALT_TIER.get(row.market), "CARD PICK")
    return labels


@router.get("/fixture/{fixture_id}/options")
def fixture_options(fixture_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    fixture = db.query(Fixture).filter(Fixture.id == fixture_id).first()
    if not fixture:
        raise HTTPException(status_code=404, detail="Fixture not found.")

    preds = db.query(Prediction).filter(Prediction.fixture_id == fixture.id).all()
    if not preds:
        raise HTTPException(status_code=404, detail="No options priced for this fixture yet.")

    # highest-prob row per market (defensive: engine writes one row/market, but
    # a double-write on a retry must never duplicate a menu line)
    row_by_market = {}
    for p in preds:
        existing = row_by_market.get(p.market)
        if existing is None or (p.probability or 0.0) > (existing.probability or 0.0):
            row_by_market[p.market] = p
    prob_by_market = {m: (p.probability or 0.0) for m, p in row_by_market.items()}

    insights = {}
    try:
        for ins in db.query(OddsInsight).filter(
            OddsInsight.fixture_id == fixture.id,
            OddsInsight.is_value_bet == True,
        ).all():
            insights[ins.market] = ins
    except Exception:
        insights = {}

    dealt = _dealt_labels(db)
    is_premium = bool(getattr(user, "is_premium", False))

    groups = []
    for key, label, markets in OPTION_GROUPS:
        options = []
        for market in markets:
            row = row_by_market.get(market)
            if row is not None:
                prob = row.probability or 0.0
                pick_label = row.pick or SAFE_MARKET_LABELS.get(market, market)
                pred_id = row.id
            else:
                derive = DERIVED_STAPLES.get(market)
                if derive is None:
                    continue
                prob = derive(prob_by_market)
                if prob is None:
                    continue
                pick_label = SAFE_MARKET_LABELS.get(market, market)
                pred_id = None

            locked = (not is_premium) and (market in PREMIUM_MARKETS)
            insight = insights.get(market)
            options.append({
                "market": market,
                "label": pick_label,
                "probability": round(prob, 4),
                "fair_odds": round(1.0 / prob, 2) if prob > 0.005 else None,
                "locked": locked,
                "dealt": dealt.get(pred_id) if pred_id else None,
                "is_value_bet": bool(insight) and not locked,
                "market_edge": round(insight.value_edge * 100, 1) if (insight and not locked) else 0,
            })
        if options:
            options.sort(key=lambda o: o["probability"], reverse=True)
            groups.append({"key": key, "label": label, "options": options})

    # streak trends for the FORM TRENDS block (display context, never dealt logic)
    sheet_trends = []
    try:
        for t in load_fixture_trends(db, fixture.id)[:6]:
            sheet_trends.append({
                "chip": t.get("chip"),
                "market": t.get("m"),
                "market_label": SAFE_MARKET_LABELS.get(t.get("m")) if t.get("m") else None,
                "odd": t.get("odd"),
            })
    except Exception:
        sheet_trends = []

    return {
        "fixture": {
            "id": fixture.id,
            "league": fixture.league,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "kickoff": format_kickoff(fixture.kickoff),
        },
        "plan": "premium" if is_premium else "free",
        "trends": sheet_trends,
        "groups": groups,
    }
