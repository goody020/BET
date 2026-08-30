from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db, get_current_user, User
from sports_models import Fixture, Prediction, OddsInsight, AutomationState


router = APIRouter(prefix="/picks", tags=["picks"])

UPCOMING_STATUSES = {"NS", "TBD", "NOT_STARTED", "SCHEDULED", "TIMED", ""}

SAFE_MARKET_LABELS = {
    "OVER_05": "Over 0.5 Goals", "OVER_15": "Over 1.5 Goals", "OVER_25": "Over 2.5 Goals",
    "OVER_35": "Over 3.5 Goals", "UNDER_35": "Under 3.5 Goals", "UNDER_45": "Under 4.5 Goals",
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
    "OVER_05", "OVER_15", "OVER_25", "UNDER_35", "UNDER_45",
    "BTTS_YES", "BTTS_NO",
    "HOME_WIN", "AWAY_WIN",
    "DC_1X", "DC_X2", "DC_12",
    "HOME_DNB", "AWAY_DNB",
    "HOME_OVER_15", "AWAY_OVER_15",
    "HOME_OVER_05", "AWAY_OVER_05",
}

# PREMIUM: specialised high-odds markets (HT options, multiscores, CS, HTFT)
PREMIUM_MARKETS = {
    "BTTS_O25", "WIN_TO_NIL_HOME", "WIN_TO_NIL_AWAY",
    "HOME_MULTI_LOW", "HOME_MULTI_HIGH",
    "AWAY_MULTI_LOW", "AWAY_MULTI_HIGH",
    "DRAW_MULTI", "HOME_MARGIN_2", "AWAY_MARGIN_2",
    "HT_HOME_MULTI", "HT_AWAY_MULTI", "HT_DRAW_MULTI",
    "HOME_OVER_25", "AWAY_OVER_25",
    "CS", "HTFT",
}

HIGH_ODDS_ONLY = PREMIUM_MARKETS - {"CS", "HTFT"}

BOARD_LIMIT_SAFE = 10
BOARD_LIMIT_HIGH_ODDS = 4
BOARD_LIMIT_CS = 2
BOARD_LIMIT_HTFT = 2


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


def serialize_pick(pred: Prediction, fixture: Fixture, db: Session, locked: bool = False):
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


def _bonus_candidates(db: Session, market: str, kickoff_start=None, kickoff_end=None):
    q = (
        db.query(Prediction, Fixture)
        .join(Fixture, Prediction.fixture_id == Fixture.id)
        .filter(
            Fixture.status.in_(UPCOMING_STATUSES),
            Prediction.market == market,
            Prediction.status == "pending",
            Prediction.probability >= BONUS_MIN_PROB.get(market, 0.0),
        )
    )
    if kickoff_start is not None:
        q = q.filter(Fixture.kickoff >= kickoff_start, Fixture.kickoff < kickoff_end)
    else:
        q = q.filter(Fixture.kickoff >= board_cutoff())
    return q.order_by(Prediction.probability.desc()).limit(40).all()


def fetch_bonus(db: Session, market: str, limit: int = 2):
    return merge_selected([], _bonus_candidates(db, market), limit, max_per_fixture=1)


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


def select_bonus_market_rows(db: Session, market: str, limit: int):
    return select_today_first(db, _bonus_candidates, limit, {"max_per_fixture": 1}, market)


def select_bonus_rows(db: Session):
    return (
        select_high_odds_rows(db)
        + select_bonus_market_rows(db, "CS", BOARD_LIMIT_CS)
        + select_bonus_market_rows(db, "HTFT", BOARD_LIMIT_HTFT)
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
    cutoff_key = _pin_key("safe", day - timedelta(days=180))
    for row in db.query(AutomationState).filter(AutomationState.state_key.like(f"{PIN_PREFIX}:%")).all():
        if row.state_key < cutoff_key:
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
        bonus_rows = fetch_pinned(db, pins["bonus"])
    else:
        # no card pinned today (pre-first-sync / degraded day): live fallback, still today-first
        free_rows = select_safe_rows(db)
        bonus_rows = select_bonus_rows(db)

    if user.is_premium:
        return {
            "plan": user.plan,
            "safe_picks": [serialize_pick(p, f, db) for p, f in free_rows],
            "bonus_picks": [serialize_pick(p, f, db) for p, f in bonus_rows],
        }

    # Free users: all free-market picks unlocked, premium section locked
    return {
        "plan": user.plan,
        "safe_picks": [serialize_pick(p, f, db) for p, f in free_rows],
        "bonus_picks": [serialize_pick(p, f, db, locked=True) for p, f in bonus_rows],
    }
