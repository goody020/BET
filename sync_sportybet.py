"""SportyBet NG price feed — the book OUR USERS actually stake on.

Daily ingest inside the 07:00 UTC sync. Reads SportyBet Nigeria's public
upcoming-events endpoint (the same unauthenticated GET their own website
makes — no account, no keys, no access control), matches events to OUR
fixtures by normalized team names + kickoff proximity, and stores decimal
prices per dealt market in RatingsCache (zero schema change).

What this is FOR: pick cards carry a real, bettable price —
"SPORTYBET @1.70" — so a dealt pick is instantly actionable on the book the
user has in their pocket. The Odds API consensus stays the value-edge
benchmark (that's the graded record); SportyBet prices are display
information, never model inputs, never part of settlement.

Hard rules (same framework as every external source):
- server-side only, one batch inside the daily sync, polite pacing (1 page /
  sb crawl politeness), hard page cap
- factual prices only, always attributed ("SportyBet @x.xx"), never their prose
- any outage = clean no-op: the board is NEVER blocked on SportyBet
- no bypass: if the endpoint ever demands auth/keys, this turns OFF, not around

Storage: RatingsCache row, key "sbodds:{fixture_id}", teams_json = JSON
{"event_id", "home", "away", "kickoff_ms", "prices": {...}, "updated"}.
"""

import json
import os
import time
from datetime import date, datetime, timedelta

import requests
from sqlalchemy.orm import Session

from sports_models import AutomationState, Fixture, RatingsCache
from sync_odds import normalize_team, similar, UPCOMING_STATUSES  # noqa: F401  (normalize_team: shared vocabulary)

LASTRUN_KEY = "sbodds:lastrun"


def _record_lastrun(db: Session, status: str, today_str: str, matched=None, fixtures=None, pages=None, error=None):
    """Self-report of the last ingest run — a tiny JSON row the site meta +
    Render logs can both point at, so 'did it run?' is never a mystery again.
    state_value is String(100): keep the payload compact."""
    payload = {"d": today_str, "s": status}
    if matched is not None:
        payload["m"] = matched
    if fixtures is not None:
        payload["f"] = fixtures
    if pages is not None:
        payload["p"] = pages
    if error:
        payload["e"] = str(error)[:40]
    try:
        value = json.dumps(payload, separators=(",", ":"))[:100]
        row = db.query(AutomationState).filter(AutomationState.state_key == LASTRUN_KEY).first()
        if row:
            row.state_value = value
        else:
            db.add(AutomationState(state_key=LASTRUN_KEY, state_value=value))
        db.commit()
    except Exception:
        db.rollback()  # the report must never break the ingest itself


def load_lastrun(db: Session):
    row = db.query(AutomationState).filter(AutomationState.state_key == LASTRUN_KEY).first()
    if not row or not row.state_value:
        return None
    try:
        return json.loads(row.state_value)
    except Exception:
        return None

CACHE_PREFIX = "sbodds:"
PRUNE_DAYS = 3

API_URL = "https://www.sportybet.com/api/ng/factsCenter/pcUpcomingEvents"
SPORT_ID = "sr:sport:1"
# SportyBet market ids (verified live 2026-09-04):
#   1=1X2  10=Double Chance  11=Draw No Bet  18=Over/Under (full-time)
#   19=Home team O/U  20=Away team O/U  26=Odd/Even  29=GG/NG
#   35=1X2 & GG/NG  36=Over/Under & GG/NG  37=1X2 & Over/Under
#   45=Correct Score  47=HT/FT  68=1st Half Over/Under
MARKET_IDS = "1,10,11,18,19,20,26,29,35,36,37,45,47,68"

PAGE_SIZE = 100
MAX_PAGES = int(os.getenv("SPORTYBET_MAX_PAGES", "18"))
TIMELINE_H = int(os.getenv("SPORTYBET_TIMELINE_H", "72"))
REQUEST_GAP = float(os.getenv("SPORTYBET_REQUEST_GAP", "1.2"))
HTTP_TIMEOUT = 20
# Fixtures this far from a SportyBet kickoff still count as the same match.
# football-data and Sportradar clocks occasionally disagree by minutes.
MATCH_WINDOW_HOURS = 2.5

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_HTA = {"Home": "H", "Draw": "D", "Away": "A"}


def _enabled() -> bool:
    return os.getenv("SPORTYBET_SYNC", "on").strip().lower() not in {"off", "0", "false", "no"}


def _ou_code(side: str, desc: str, prefix: str = ""):
    """outcome desc 'Over 2.5' -> 'OVER_25' (prefix='HOME_' -> 'HOME_OVER_25')."""
    try:
        head, tail = desc.split(" ", 1)
    except ValueError:
        return None
    if head.lower() != side:
        return None
    try:
        line = float(tail)
    except ValueError:
        return None
    if line not in (0.5, 1.5, 2.5, 3.5, 4.5):
        return None
    return f"{prefix}{side.upper()}_{int(line * 10):02d}"


def event_prices(event):
    """SportyBet event -> {our_market: odd} (+ nested dicts for CS / HTFT).

    Only markets we actually DEAL are decoded; everything else in the payload
    is ignored on purpose — the card stays the product.
    """
    prices = {}
    cs, htft = {}, {}
    for market in event.get("markets") or []:
        mid = str(market.get("id") or "")
        for outcome in market.get("outcomes") or []:
            desc = (outcome.get("desc") or "").strip()
            try:
                odd = float(outcome.get("odds"))
            except (TypeError, ValueError):
                continue
            if odd <= 1.0:
                continue
            code = None
            if mid == "1":            # 1X2
                code = {"Home": "HOME_WIN", "Draw": "DRAW", "Away": "AWAY_WIN"}.get(desc)
            elif mid == "10":         # Double Chance
                code = {"Home or Draw": "DC_1X", "Home or Away": "DC_12", "Draw or Away": "DC_X2"}.get(desc)
            elif mid == "11":         # Draw No Bet
                code = {"Home": "HOME_DNB", "Away": "AWAY_DNB"}.get(desc)
            elif mid == "18":         # full-time totals
                code = _ou_code("over", desc) or _ou_code("under", desc)
            elif mid == "19":         # home team totals
                code = _ou_code("over", desc, "HOME_")
            elif mid == "20":         # away team totals
                code = _ou_code("over", desc, "AWAY_")
            elif mid == "26":         # Odd/Even
                code = {"Odd": "ODD_GOALS", "Even": "EVEN_GOALS"}.get(desc)
            elif mid == "29":         # BTTS
                code = {"Yes": "BTTS_YES", "No": "BTTS_NO"}.get(desc)
            elif mid == "35":         # 1X2 & GG/NG
                code = {"Home & yes": "HOME_GG", "Away & yes": "AWAY_GG"}.get(desc)
            elif mid == "36":         # total & BTTS combos
                if desc == "Over 2.5 & Yes":
                    code = "BTTS_O25"
            elif mid == "37":         # 1X2 & total combos
                code = {"Home & Over 2.5": "HOME_O25", "Away & Over 2.5": "AWAY_O25"}.get(desc)
            elif mid == "45":         # Correct Score — '2:0' -> our '2-0'
                try:
                    h, a = desc.split(":")
                    cs[f"{int(h)}-{int(a)}"] = odd
                except (ValueError, AttributeError):
                    pass
            elif mid == "47":         # HT/FT — 'Home/Draw' -> our 'H/D'
                try:
                    ht, ft = desc.split("/")
                    htft[f"{_HTA[ht]}/{_HTA[ft]}"] = odd
                except (ValueError, KeyError, AttributeError):
                    pass
            elif mid == "68":         # 1st-half totals
                code = _ou_code("over", desc, "HT_")
            if code and code not in prices:
                prices[code] = odd
    if cs:
        prices["CS"] = cs
    if htft:
        prices["HTFT"] = htft
    return prices


def parse_events(raw):
    """One API page -> flat list of normalized event stubs."""
    events = []
    for tourn in (raw or {}).get("tournaments") or []:
        league = ((tourn or {}).get("name")) or ""
        for ev in tourn.get("events") or []:
            try:
                kickoff_ms = int(ev.get("estimateStartTime"))
            except (TypeError, ValueError):
                continue
            events.append({
                "event_id": ev.get("eventId"),
                "home": ev.get("homeTeamName") or "",
                "away": ev.get("awayTeamName") or "",
                "league": league,
                "kickoff_ms": kickoff_ms,
                "prices": event_prices(ev),
            })
    return events


def fetch_page(page_num, session=None):
    """One paged request. Raises on transport error — caller treats as skip."""
    http = session or requests
    resp = http.get(API_URL, params={
        "sportId": SPORT_ID,
        "marketId": MARKET_IDS,
        "specifyMarkets": 1,
        "pageSize": PAGE_SIZE,
        "pageNum": page_num,
        "timeline": TIMELINE_H,
    }, headers={"User-Agent": USER_AGENT}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("bizCode") != 10000:
        raise RuntimeError(f"SportyBet bizCode {payload.get('bizCode')}: {payload.get('message')}")
    return payload.get("data") or {}


def _kickoff_delta_hours(fixture_kickoff, kickoff_ms):
    if not fixture_kickoff:
        return None
    sb_kickoff = datetime.utcfromtimestamp(kickoff_ms / 1000.0)
    ours = fixture_kickoff.replace(tzinfo=None) if getattr(fixture_kickoff, "tzinfo", None) else fixture_kickoff
    return abs((sb_kickoff - ours).total_seconds()) / 3600.0


def match_event(fixture, events):
    """Best SAME-ORIENTATION event for a fixture (teams + kickoff proximity).

    A flipped pairing (their home vs our away) is never accepted — same rule
    as the trends ingest: a wrong price is worse than no price.
    """
    best, best_delta = None, None
    for ev in events:
        if not ev.get("prices"):
            continue
        if not (similar(fixture.home_team, ev["home"]) and similar(fixture.away_team, ev["away"])):
            continue
        delta = _kickoff_delta_hours(fixture.kickoff, ev["kickoff_ms"])
        if delta is None or delta > MATCH_WINDOW_HOURS:
            continue
        if best is None or delta < best_delta:
            best, best_delta = ev, delta
    return best


def save_prices(db: Session, fixture_id, ev, today_str, session_cache):
    payload = json.dumps({
        "event_id": ev["event_id"],
        "home": ev["home"], "away": ev["away"],
        "kickoff_ms": ev["kickoff_ms"],
        "prices": ev["prices"],
        "updated": today_str,
    })
    row = session_cache.get(fixture_id)
    if row is None:
        row = db.query(RatingsCache).filter(
            RatingsCache.cache_key == f"{CACHE_PREFIX}{fixture_id}"
        ).first()
    if row is None:
        row = RatingsCache(cache_key=f"{CACHE_PREFIX}{fixture_id}", updated=today_str, teams_json=payload)
    else:
        row.updated = today_str
        row.teams_json = payload
    db.add(row)
    session_cache[fixture_id] = row


def prune_stale(db: Session, today_str):
    cutoff = (date.fromisoformat(today_str) - timedelta(days=PRUNE_DAYS)).isoformat()
    for row in db.query(RatingsCache).filter(RatingsCache.cache_key.like(f"{CACHE_PREFIX}%")).all():
        if (row.updated or "0000") < cutoff:
            db.delete(row)


def load_fixture_prices(db: Session, fixture_id):
    """Serializer-side read: prices dict for one fixture, None when nothing stored."""
    row = db.query(RatingsCache).filter(
        RatingsCache.cache_key == f"{CACHE_PREFIX}{fixture_id}"
    ).first()
    if not row:
        return None
    try:
        data = json.loads(row.teams_json or "{}")
    except Exception:
        return None
    prices = data.get("prices")
    return prices if isinstance(prices, dict) else None


# ---------------------------------------------------------------- main ingest
def sync_sportybet(db: Session, fetcher=None):
    """Daily entry point (automation.py). Never raises outward — any failure
    is a reported status, never a broken sync."""
    if not _enabled():
        print("SportyBet ingest: disabled via SPORTYBET_SYNC — skipping.")
        _record_lastrun(db, "off", date.today().isoformat())
        return {"status": "off"}

    today_str = date.today().isoformat()
    fetch = fetcher or fetch_page

    now = datetime.utcnow()
    lo = now - timedelta(hours=3)
    hi = now + timedelta(hours=TIMELINE_H)
    pending = [
        f for f in db.query(Fixture).filter(Fixture.status.in_(UPCOMING_STATUSES)).all()
        if f.kickoff and lo <= f.kickoff <= hi
    ]
    if not pending:
        prune_stale(db, today_str)
        db.commit()
        _record_lastrun(db, "no-fixtures", today_str, matched=0, fixtures=0)
        print("SportyBet ingest: no upcoming fixtures — nothing to price.")
        return {"status": "no-fixtures"}

    unmatched = {f.id: f for f in pending}
    matched = 0
    pages = 0
    session_cache = {}
    try:
        for page in range(1, MAX_PAGES + 1):
            if page > 1:
                time.sleep(REQUEST_GAP)  # polite pacing — one batch per day
            data = fetch(page)
            pages = page
            events = parse_events(data)
            if not events:
                break
            for fid, fixture in list(unmatched.items()):
                ev = match_event(fixture, events)
                if ev:
                    save_prices(db, fid, ev, today_str, session_cache)
                    del unmatched[fid]
                    matched += 1
            if not unmatched:
                break
    except Exception as e:
        # partial coverage is FINE — priced fixtures keep their prices.
        # (network failures don't poison the session; commit what we have)
        print(f"SportyBet ingest: fetch stopped early ({e}); keeping {matched} priced fixtures.")
        try:
            prune_stale(db, today_str)
            db.commit()
        except Exception:
            db.rollback()
        _record_lastrun(db, "partial", today_str, matched=matched, fixtures=len(pending), pages=pages, error=e)
        return {"status": "partial", "matched": matched, "error": str(e)}

    prune_stale(db, today_str)
    db.commit()
    _record_lastrun(db, "ok", today_str, matched=matched, fixtures=len(pending), pages=pages)
    print(f"SportyBet ingest: priced {matched}/{len(pending)} fixtures over {pages} page(s).")
    return {"status": "ok", "matched": matched, "fixtures": len(pending), "pages": pages}
