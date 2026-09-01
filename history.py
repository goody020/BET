from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db
from sports_models import Prediction, Fixture, AutomationState
from picks import HIGH_ODDS_ONLY, SAFE_MARKET_LABELS, PIN_PREFIX


router = APIRouter(prefix="/history", tags=["history"])

# Receipts floor: below 5% model probability a hit is lottery noise, not skill.
# Never celebrate it in public — it was never on a sellable card anyway.
RECEIPT_MIN_PROB = 0.05


def kickoff_key(fixture):
    return fixture.kickoff or datetime(1970, 1, 1)


def _card_anchor(db: Session):
    """(pinned_ids, earliest_pin_day) — the set of prediction IDs that were
    actually published on a daily card, and when card-freezing began.
    (empty set / None) means the freeze feature never ran (legacy/history mode)."""
    pinned_ids = set()
    earliest_day = None
    try:
        rows = db.query(AutomationState).filter(
            AutomationState.state_key.like(f"{PIN_PREFIX}:%")
        ).all()
        for row in rows:
            value = (row.state_value or "").strip()
            if value and value != "-":
                for token in value.split(","):
                    token = token.strip()
                    if token.isdigit():
                        pinned_ids.add(int(token))
            try:
                day = datetime.fromisoformat(row.state_key.rsplit(":", 1)[1]).date()
                earliest_day = day if earliest_day is None else min(earliest_day, day)
            except Exception:
                continue
    except Exception:
        return set(), None
    return pinned_ids, earliest_day


def _is_receipt(pred, fixture, pinned_ids, earliest_day):
    """Was this something we actually stood behind?
    True if the pick sat on a daily card. For matches that predate the
    freeze feature, falls back to the 5% honesty floor."""
    if pred.id in pinned_ids:
        return True
    prob = pred.probability or 0.0
    if earliest_day is None:
        return prob >= RECEIPT_MIN_PROB
    kickoff_date = fixture.kickoff.date() if fixture.kickoff else None
    if kickoff_date is None or kickoff_date < earliest_day:
        return prob >= RECEIPT_MIN_PROB
    return False


@router.get("/wins")
def get_wins(limit: int = 20, db: Session = Depends(get_db)):
    pinned_ids, earliest_day = _card_anchor(db)

    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status == "won"
    ).order_by(Prediction.id.desc()).limit(300).all()

    # One headline win per match: biggest-odds CARD win (lowest probability
    # among receipts) — never lottery noise that was never given to anyone.
    best = {}
    for pred, fixture in rows:
        if not _is_receipt(pred, fixture, pinned_ids, earliest_day):
            continue
        cur = best.get(fixture.id)
        if cur is None or pred.probability < cur[0].probability:
            best[fixture.id] = (pred, fixture)

    chosen = sorted(best.values(), key=lambda pf: kickoff_key(pf[1]), reverse=True)

    return [
        {
            "market": pred.market,
            "pick": pred.pick,
            "date": fixture.kickoff.isoformat() if fixture.kickoff else None,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "league": fixture.league,
        }
        for pred, fixture in chosen[:limit]
    ]


@router.get("/results")
def get_results(limit: int = 30, db: Session = Depends(get_db)):
    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status.in_(["won", "lost"])
    ).order_by(Prediction.id.desc()).limit(300).all()

    # One line per match: the main pick (highest probability) — taking the
    # max-probability line already excludes lottery-noise entries by design.
    best = {}
    for pred, fixture in rows:
        cur = best.get(fixture.id)
        if cur is None or pred.probability > cur[0].probability:
            best[fixture.id] = (pred, fixture)

    chosen = sorted(best.values(), key=lambda pf: kickoff_key(pf[1]), reverse=True)

    return [
        {
            "market": pred.market,
            "pick": pred.pick,
            "status": pred.status,
            "date": fixture.kickoff.isoformat() if fixture.kickoff else None,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "score": f"{fixture.home_goals}-{fixture.away_goals}" if fixture.home_goals is not None else "-",
        }
        for pred, fixture in chosen[:limit]
    ]


@router.get("/high-odds-wins")
def get_high_odds_wins(limit: int = 20, db: Session = Depends(get_db)):
    """Itemized receipts behind the high-odds record: every won pick in a
    premium high-odds market THAT WAS ACTUALLY ON A CARD, newest first.
    Picks that were never given to users (sub-5% lottery noise) never appear."""
    pinned_ids, earliest_day = _card_anchor(db)

    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status == "won",
        Prediction.market.in_(HIGH_ODDS_ONLY),
    ).order_by(Prediction.settled_at.desc(), Prediction.id.desc()).limit(150).all()

    receipts = [
        (pred, fixture) for pred, fixture in rows
        if _is_receipt(pred, fixture, pinned_ids, earliest_day)
    ][:limit]

    return [
        {
            "market": pred.market,
            "market_label": SAFE_MARKET_LABELS.get(pred.market, pred.market),
            "pick": pred.pick,
            "probability": pred.probability,
            "date": fixture.kickoff.isoformat() if fixture.kickoff else None,
            "settled_at": pred.settled_at.isoformat() if pred.settled_at else None,
            "home_team": fixture.home_team,
            "away_team": fixture.away_team,
            "league": fixture.league,
            "score": f"{fixture.home_goals}-{fixture.away_goals}" if fixture.home_goals is not None else "-",
        }
        for pred, fixture in receipts
    ]


def _pin_ids_by_kind(db: Session, kind: str):
    """(pinned_ids, earliest_day_str) for one board ('safe' / 'bonus')."""
    ids = set()
    earliest = None
    rows = db.query(AutomationState).filter(
        AutomationState.state_key.like(f"{PIN_PREFIX}:{kind}:%")
    ).all()
    for row in rows:
        try:
            day = datetime.fromisoformat(row.state_key.rsplit(":", 1)[1]).date()
            earliest = day if earliest is None else min(earliest, day)
        except Exception:
            continue
        value = (row.state_value or "").strip()
        if value and value != "-":
            for token in value.split(","):
                token = token.strip()
                if token.isdigit():
                    ids.add(int(token))
    return ids, (earliest.isoformat() if earliest else None)


@router.get("/card-record")
def get_card_record(db: Session = Depends(get_db)):
    """The record of what was ACTUALLY DEALT — settled picks that sat on a
    pinned daily card. This is the number that represents the product:
    longshots the engine priced but never put on a card count for nobody.
    Rate = won / (won + lost); voids (e.g. refunded DNB) reported separately."""

    def tally(kind):
        ids, since = _pin_ids_by_kind(db, kind)
        won = lost = void = 0
        if ids:
            for (status,) in db.query(Prediction.status).filter(
                Prediction.id.in_(ids), Prediction.status != "pending"
            ).all():
                if status == "won":
                    won += 1
                elif status == "lost":
                    lost += 1
                else:
                    void += 1
        graded = won + lost
        return {
            "won": won,
            "lost": lost,
            "void": void,
            "graded": graded,
            "rate": round(won / graded * 100, 1) if graded else None,
            "since": since,
        }

    banker = tally("safe")
    high = tally("bonus")

    # Acca of the Day record: derived from the pinned banker card itself —
    # each day, the top-2 pinned safe picks are the acca's legs. A day grades
    # once every leg is settled: all legs won => won; any leg lost => lost;
    # void legs fall out (all-void day => void). Days still playing => pending.
    acca_ids_by_day = {}
    rows = db.query(AutomationState).filter(
        AutomationState.state_key.like(f"{PIN_PREFIX}:safe:%")
    ).all()
    for row in rows:
        try:
            day = datetime.fromisoformat(row.state_key.rsplit(":", 1)[1]).date()
        except Exception:
            continue
        value = (row.state_value or "").strip()
        if value and value != "-":
            ids = [int(t) for t in (x.strip() for x in value.split(",")) if t.isdigit()][:2]
            if len(ids) == 2:
                acca_ids_by_day[day] = ids

    acca = {"won": 0, "lost": 0, "void": 0, "graded": 0, "rate": None,
            "pending_days": 0, "since": min(acca_ids_by_day).isoformat() if acca_ids_by_day else None}
    all_leg_ids = {i for ids in acca_ids_by_day.values() for i in ids}
    statuses = {}
    if all_leg_ids:
        for pid, st in db.query(Prediction.id, Prediction.status).filter(Prediction.id.in_(all_leg_ids)).all():
            statuses[pid] = st
    for day, ids in acca_ids_by_day.items():
        sts = [statuses.get(i, "pending") for i in ids]
        if any(s == "pending" for s in sts):
            acca["pending_days"] += 1
            continue
        live = [s for s in sts if s != "void"]
        if not live:
            acca["void"] += 1
        elif all(s == "won" for s in live):
            acca["won"] += 1
        else:
            acca["lost"] += 1
    acca["graded"] = acca["won"] + acca["lost"]
    if acca["graded"]:
        acca["rate"] = round(acca["won"] / acca["graded"] * 100, 1)

    days = [d for d in (banker["since"], high["since"]) if d]
    return {"since": min(days) if days else None, "banker": banker, "high_odds": high, "acca": acca}
