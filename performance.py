from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app import get_db
from sports_models import Prediction
from picks import BANKER_MARKETS, HIGH_ODDS_MARKETS, SAFE_MARKET_LABELS


router = APIRouter(prefix="/performance", tags=["performance"])


@router.get("/summary")
def performance_summary(db: Session = Depends(get_db)):
    settled = db.query(Prediction).filter(
        Prediction.status.in_(["won", "lost", "void"])
    ).all()

    wins = sum(1 for p in settled if p.status == "won")
    losses = sum(1 for p in settled if p.status == "lost")
    voids = sum(1 for p in settled if p.status == "void")
    decided = wins + losses

    hit_rate = round((wins / decided) * 100, 1) if decided else 0.0

    bankers = [p for p in settled if p.market in BANKER_MARKETS]
    b_wins = sum(1 for p in bankers if p.status == "won")
    b_decided = sum(1 for p in bankers if p.status in ("won", "lost"))
    banker_hit = round((b_wins / b_decided) * 100, 1) if b_decided else 0.0

    high = [p for p in settled if p.market in HIGH_ODDS_MARKETS or p.market in ("CS", "HTFT")]
    h_wins = sum(1 for p in high if p.status == "won")
    h_decided = sum(1 for p in high if p.status in ("won", "lost"))
    high_hit = round((h_wins / h_decided) * 100, 1) if h_decided else 0.0

    by_market = {}
    for p in settled:
        m = by_market.setdefault(p.market, {"total": 0, "wins": 0, "losses": 0})
        m["total"] += 1
        if p.status in ("won", "lost"):
            m[p.status] += 1

    market_stats = []
    for market, m in sorted(by_market.items(), key=lambda kv: kv[1]["total"], reverse=True)[:10]:
        dec = m["wins"] + m["losses"]
        market_stats.append({
            "market": market,
            "label": SAFE_MARKET_LABELS.get(market, market),
            "total": m["total"],
            "wins": m["wins"],
            "losses": m["losses"],
            "hit_rate": round((m["wins"] / dec) * 100, 1) if dec else 0.0,
        })

    return {
        "total": len(settled),
        "settled": len(settled),
        "wins": wins,
        "losses": losses,
        "voids": voids,
        "hit_rate": banker_hit,
        "win_rate": banker_hit,
        "overall_hit_rate": hit_rate,
        "bankers": {
            "total": len(bankers),
            "wins": b_wins,
            "losses": b_decided - b_wins,
            "hit_rate": banker_hit,
        },
        "high_odds": {
            "total": len(high),
            "wins": h_wins,
            "losses": h_decided - h_wins,
            "hit_rate": high_hit,
        },
        "market_stats": market_stats,
        "markets": market_stats,
    }