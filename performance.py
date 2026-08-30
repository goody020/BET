from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db
from sports_models import Prediction, Fixture
from picks import PREMIUM_MARKETS
from history import get_card_record


router = APIRouter(prefix="/performance", tags=["performance"])

# Pool telemetry set = every market the premium boards can deal.
# (Imported from picks.py so the tracker can never drift from the product:
# BTTS+O25, win-to-nil and team totals are included here.)
HIGH_ODDS_SET = PREMIUM_MARKETS


@router.get("/summary")
def performance_summary(db: Session = Depends(get_db)):
    rows = db.query(Prediction, Fixture).join(
        Fixture, Prediction.fixture_id == Fixture.id
    ).filter(
        Prediction.status.in_(["won", "lost", "void"])
    ).all()

    # One main pick per match = highest probability settled prediction
    main = {}
    for pred, fixture in rows:
        cur = main.get(fixture.id)
        if cur is None or pred.probability > cur.probability:
            main[fixture.id] = pred

    won = sum(1 for p in main.values() if p.status == "won")
    lost = sum(1 for p in main.values() if p.status == "lost")
    void = sum(1 for p in main.values() if p.status == "void")
    total = won + lost
    win_rate = round(won / total * 100, 1) if total else 0.0

    # High-odds POOL (engine telemetry: every premium longshot priced —
    # most of which were never dealt on a card). The PUBLIC headline record
    # is the dealt-card record below, served via /history/card-record and
    # mirrored into this payload under "card_record".
    ho = [p for p, f in rows if p.market in HIGH_ODDS_SET and p.status in ("won", "lost")]
    ho_won = sum(1 for p in ho if p.status == "won")
    ho_total = len(ho)
    ho_rate = round(ho_won / ho_total * 100, 1) if ho_total else 0.0

    # Last 10 form + streak (newest first)
    ordered = sorted(main.values(), key=lambda p: p.id, reverse=True)
    last_10 = [p.status[0].upper() for p in ordered if p.status in ("won", "lost")][:10]

    streak = 0
    for s in last_10:
        if not last_10 or s != last_10[0]:
            break
        streak += 1
    streak_text = f"{streak}{'W' if last_10 and last_10[0] == 'W' else 'L'}" if last_10 else "0"

    # Per-market breakdown (all settled picks)
    market_stats = {}
    for pred, fixture in rows:
        if pred.status not in ("won", "lost"):
            continue
        m = market_stats.setdefault(pred.market, {"market": pred.market, "total": 0, "won": 0})
        m["total"] += 1
        if pred.status == "won":
            m["won"] += 1

    market_list = sorted(market_stats.values(), key=lambda m: m["total"], reverse=True)
    for m in market_list:
        m["win_rate"] = round(m["won"] / m["total"] * 100, 1) if m["total"] else 0.0

    # Dealt-card record: settled picks that actually sat on a pinned daily
    # card. Single source of truth lives in history.py — never reimplement.
    try:
        card_record = get_card_record(db=db)
    except Exception:
        card_record = None

    banker_card = (card_record or {}).get("banker") or {}
    high_card = (card_record or {}).get("high_odds") or {}

    payload = {
        "total": total,
        "won": won,
        "lost": lost,
        "void": void,
        "win_rate": win_rate,
        "strike_rate": win_rate,
        "accuracy": win_rate,
        "settled": total,
        "predictions": total,
        "high_odds_won": ho_won,
        "high_odds_total": ho_total,
        "high_odds_rate": ho_rate,
        "last_10": last_10,
        "current_streak": streak_text,
        "market_stats": market_list,
        # dealt-card record (the number that represents the product)
        "card_record": card_record,
        "banker_card_won": banker_card.get("won", 0),
        "banker_card_total": banker_card.get("graded", 0),
        "banker_card_rate": banker_card.get("rate"),
        "high_odds_card_won": high_card.get("won", 0),
        "high_odds_card_total": high_card.get("graded", 0),
        "high_odds_card_rate": high_card.get("rate"),
        "card_since": (card_record or {}).get("since"),
    }
    payload["summary"] = {
        "total": total,
        "won": won,
        "lost": lost,
        "void": void,
        "win_rate": win_rate,
        "high_odds_won": ho_won,
        "high_odds_total": ho_total,
        "high_odds_rate": ho_rate,
        "last_10": last_10,
        "current_streak": streak_text,
    }

    return payload
