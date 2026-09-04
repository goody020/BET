"""scores24 trend ingest — powers the "WHY" streak chips on dealt cards and the
FORM TRENDS block in the options sheet.

Rules this module lives by:
- scores24 robots.txt allows current pages; the trends list is current by nature.
- Max 2 requests per sync, 1.5s apart, identifying UA. Failure = chips absent,
  never a broken sync (automation wraps us in try/except and we self-guard too).
- THEIR PROSE IS NEVER STORED. We keep factual titles ("Arsenal have won") plus
  the parsed streak NUMBERS and rebuild one-line chips in our own format.
- Streaks are DISPLAY context (and later, small model features). They never
  change which pick the model deals — that stays pure argmax.

Storage: RatingsCache rows, key "trendfx:{fixture_id}", teams_json = JSON list of
chips. Zero schema change, pruned after 3 days.
"""
import json
import re
import time
from datetime import date, datetime, timedelta

import requests
from sqlalchemy.orm import Session

from sports_models import Fixture, RatingsCache
from sync_odds import normalize_team, similar

S24_BASE = "https://scores24.live/en/trends/soccer"
PAGES = 2                       # server embeds ~10 matches per page
REQUEST_GAP_SECONDS = 1.5
UA = "Mozilla/5.0 (compatible; ValueBetsBot/1.0; +https://www.valuebets.name.ng)"
CACHE_PREFIX = "trendfx:"
PRUNE_DAYS = 3
MAX_TRENDS_PER_FIXTURE = 8
MATCH_WINDOW_HOURS = 30
UPCOMING = {"NS", "TBD", "NOT_STARTED", "SCHEDULED", "TIMED", ""}

_STATE_RE = re.compile(
    r'window\.__REACT_QUERY_STATE__\s*=\s*JSON\.parse\(("(?:[^"\\]|\\.)*")\)'
)

# "in 8 of last 9 games." / "in last 7 games (Premier League)." /
# "in last 10 Arsenal home games." / "in 15 of last 16 home games against Napoli (Serie A)."
# The (?!home |away ) stop keeps the venue words OUT of the optional team-word
# group — "in last 5 home games." has NO team, and "in last 8 away games."
# reads as away, never as a team called "away".
_FACT_RE = re.compile(
    r"^in (?:(\d+) of )?last (\d+)"
    r"(?: (?!home |away )([A-Za-zÀ-ž0-9'&.\- ]+?))?"
    r"( home| away)? games?"
    r"(?: against (.+?))?"
    r"(?:\s*\((.+?)\))?\.$"
)


# ---------------------------------------------------------------- small helpers
def _clamp01(x):
    return max(0.0, min(1.0, x))


def _line_code(value):
    """'2_5' -> '25', 1.5 -> '15', 0.5 -> '05'. None if not a .5 line we carry."""
    s = str(value).replace("_", ".")
    try:
        f = float(s)
    except (TypeError, ValueError):
        return None
    code = f"{int(round(f * 10)):02d}"
    return code if code in {"05", "15", "25", "35", "45"} else None


def map_market(prediction_type):
    """scores24 prediction.type pair -> our market code, or None (display-only)."""
    if not prediction_type or len(prediction_type) < 2:
        return None
    fam, val = prediction_type[0], prediction_type[1]

    if fam == "one_x_two":
        return {"w1": "HOME_WIN", "x": "DRAW", "w2": "AWAY_WIN"}.get(str(val))
    if fam == "double_chance":
        return {"1x": "DC_1X", "12": "DC_12", "x2": "DC_X2"}.get(str(val))
    if fam == "both_to_score":
        return {"yes": "BTTS_YES", "no": "BTTS_NO"}.get(str(val))

    code = _line_code(val)
    if code is None:
        return None
    if fam == "total_over":
        return f"OVER_{code}"
    if fam == "total_under":
        return f"UNDER_{code}"
    if fam == "total_t1_over":
        return f"HOME_OVER_{code}"
    if fam == "total_t2_over":
        return f"AWAY_OVER_{code}"
    if fam == "total_over_half1":
        return f"HT_OVER_{code}"
    return None


# ---------------------------------------------------------------- page parsing
def fetch_html(url):
    try:
        resp = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        if resp.status_code != 200:
            print(f"Trends fetch: HTTP {resp.status_code} for {url}")
            return ""
        return resp.text
    except Exception as e:
        print(f"Trends fetch failed for {url}: {e}")
        return ""


def decode_state(html):
    m = _STATE_RE.search(html or "")
    if not m:
        return None
    try:
        return json.loads(json.loads(m.group(1)))
    except Exception:
        return None


def extract_edges(html):
    """ -> list of trend-match edges (node {match, groups}) from the react-query state."""
    state = decode_state(html)
    if not state:
        return []
    for query in state.get("queries", []):
        key = query.get("queryKey")
        if isinstance(key, list):
            key = key[0] if key else {}
        if not isinstance(key, dict) or not str(key.get("_id", "")).startswith("localizedTrendsList"):
            continue
        data = (query.get("state") or {}).get("data") or {}
        edges = ((data.get("data") or {}).get("edges")) or []
        return [e.get("node") for e in edges if isinstance(e, dict) and e.get("node")]
    return []


# ---------------------------------------------------------------- fact parsing
def parse_fact(fact_text):
    """'in 15 of last 16 home games against Napoli (Serie A).' ->
    {k, n, venue, opp, league} — numbers only, or None if it isn't a streak."""
    m = _FACT_RE.match((fact_text or "").strip())
    if not m:
        return None
    k = int(m.group(1)) if m.group(1) is not None else int(m.group(2))
    n = int(m.group(2))
    if n <= 0 or k < 0 or k > n:
        return None
    venue = (m.group(4) or "").strip() or None      # group(3) is their embedded team word
    return {
        "k": k,
        "n": n,
        "team_word": (m.group(3) or "").strip() or None,
        "venue": venue,
        "opp": (m.group(5) or "").strip() or None,
        "league": (m.group(6) or "").strip() or None,
    }


def make_chip(title, parsed_facts):
    """One-line chip in OUR format from title + best streak numbers.
    'Arsenal have won' + {8 of 9} -> 'Arsenal have won: 8 of last 9'"""
    if not title or not parsed_facts:
        return None

    # If the title names a specific team ("Arsenal scored..."), only facts about
    # THAT team may back the chip — scores24 mixes both teams' facts under one
    # title, and "Arsenal scored: 9 straight — Chelsea" is just confusing.
    title_l = title.lower()
    subject = None
    for f in parsed_facts:
        who = (f.get("who") or "").lower()
        if who and title_l.startswith(who):
            subject = who
            break
    candidates = [f for f in parsed_facts if (f.get("who") or "").lower() == subject] if subject else parsed_facts
    if not candidates:
        candidates = parsed_facts

    # quality-weighted best: a 23-of-23 reads louder than a 5-of-5
    best = max(candidates, key=lambda f: ((f["k"] / f["n"]) * min(f["n"], 25), f["k"] / f["n"]))
    num = f'{best["k"]} of last {best["n"]}' if best["k"] != best["n"] else f'{best["n"]} straight'

    who = best.get("who")
    title_has_team = bool(who) and title.lower().startswith(who.lower())
    ctx = []
    if who and not title_has_team:
        ctx.append(who)
    if best.get("opp"):
        ctx.append("v " + best["opp"])
    elif best.get("venue"):
        ctx.append(best["venue"] + " games")
    if best.get("league"):
        ctx.append(best["league"])

    chip = f"{title}: {num}"
    if ctx:
        chip += " — " + ", ".join(ctx)
    return chip, best


def build_trend(trend):
    """One scores24 trend node -> our chip dict, or None if it has no parseable streak."""
    ptype = (trend.get("prediction") or {}).get("type") or []
    odd = (trend.get("prediction") or {}).get("value")

    title = None
    parsed = []
    for gf in trend.get("groupedFacts") or []:
        title = title or (gf.get("title") or "").strip()
        for fact in gf.get("facts") or []:
            p = parse_fact(fact.get("fact"))
            if p:
                team = fact.get("team") or {}
                p["who"] = (team.get("name") or "").strip() or None
                p["is_h2h"] = fact.get("type") == "h2h" or bool(p.get("opp"))
                parsed.append(p)

    built = make_chip(title, parsed)
    if not built:
        return None
    chip, best = built

    score = (best["k"] / best["n"]) * min(best["n"], 25)
    if trend.get("trendType") == "surefire":
        score += 3.0
    if best.get("is_h2h"):
        score += 2.0
    if best["n"] >= 10:
        score += 1.0

    return {
        "m": map_market(ptype),
        "raw": "/".join(str(x) for x in ptype) if ptype else None,
        "chip": chip,
        "score": round(score, 2),
        "odd": odd,
    }


# ---------------------------------------------------------------- fixture matching
def _parse_kickoff(value):
    """scores24: '2026-09-06 15:30:00' (UTC). -> naive datetime"""
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def match_fixture(db: Session, home_name, away_name, kickoff):
    """Strict same-orientation team-pair match inside a kickoff window.
    If their card lists the pair flipped we DECLINE — attaching 'home streaks'
    to the wrong team is worse than showing no chips."""
    if not home_name or not away_name or not kickoff:
        return None
    lo = kickoff - timedelta(hours=MATCH_WINDOW_HOURS)
    hi = kickoff + timedelta(hours=MATCH_WINDOW_HOURS)
    try:
        candidates = (
            db.query(Fixture)
            .filter(Fixture.kickoff >= lo, Fixture.kickoff <= hi)
            .all()
        )
    except Exception:
        return None

    for f in candidates:
        if normalize_team(f.home_team) == normalize_team(home_name) and \
           normalize_team(f.away_team) == normalize_team(away_name):
            return f
    for f in candidates:
        if similar(f.home_team, home_name) and similar(f.away_team, away_name) \
           and not (similar(f.home_team, away_name) or similar(f.away_team, home_name)):
            return f
    return None


# ---------------------------------------------------------------- storage
def save_fixture_trends(db: Session, fixture_id, trends, today_str, session_cache):
    """Upsert one fixture's chip list. session_cache tracks rows THIS run so a
    fixture seen on two pages is updated, never inserted twice — required even
    under autoflush=False sessions (the pending add is invisible to queries)."""
    payload = json.dumps(trends, ensure_ascii=False)

    row = session_cache.get(fixture_id)
    if row is None:
        row = db.query(RatingsCache).filter(
            RatingsCache.cache_key == f"{CACHE_PREFIX}{fixture_id}"
        ).first()

    if row:
        row.teams_json = payload
        row.updated = today_str
    else:
        row = RatingsCache(cache_key=f"{CACHE_PREFIX}{fixture_id}", updated=today_str, teams_json=payload)
        db.add(row)
    session_cache[fixture_id] = row


def prune_stale(db: Session, today_str):
    cutoff = (date.fromisoformat(today_str) - timedelta(days=PRUNE_DAYS)).isoformat()
    for row in db.query(RatingsCache).filter(RatingsCache.cache_key.like(f"{CACHE_PREFIX}%")).all():
        if (row.updated or "0000") < cutoff:
            db.delete(row)


def load_fixture_trends(db: Session, fixture_id):
    """Serializer-side read: chip list for one fixture, [] when nothing stored."""
    row = db.query(RatingsCache).filter(
        RatingsCache.cache_key == f"{CACHE_PREFIX}{fixture_id}"
    ).first()
    if not row:
        return []
    try:
        trends = json.loads(row.teams_json or "[]")
    except Exception:
        return []
    return trends if isinstance(trends, list) else []


# ---------------------------------------------------------------- main ingest
def ingest_edges(db: Session, nodes, today_str):
    """Pure pipeline (no network): nodes -> matched fixtures -> stored chips."""
    matched = 0
    seen_slugs = set()
    session_cache = {}
    for node in nodes:
        try:
            match = node.get("match") or {}
            slug = (match.get("slug") or "").strip()
            if slug and slug in seen_slugs:
                continue
            if slug:
                seen_slugs.add(slug)

            teams = match.get("teams") or []
            if len(teams) < 2:
                continue
            home = (teams[0].get("name") or "").strip()
            away = (teams[1].get("name") or "").strip()
            kickoff = _parse_kickoff(match.get("matchDate"))

            fixture = match_fixture(db, home, away, kickoff)
            if not fixture:
                continue

            trends = []
            for group in node.get("groups") or []:
                for trend in group.get("trends") or []:
                    built = build_trend(trend)
                    if built:
                        trends.append(built)

            if not trends:
                continue
            trends.sort(key=lambda t: t["score"], reverse=True)
            save_fixture_trends(db, fixture.id, trends[:MAX_TRENDS_PER_FIXTURE], today_str, session_cache)
            matched += 1
        except Exception as e:
            print(f"Trend edge skipped ({e})")
            continue
    return matched


def sync_trends(db: Session):
    """Daily ingest. Wrapped so a scores24 outage can never hurt the sync."""
    today_str = date.today().isoformat()
    total_edges = 0
    nodes = []

    for page in range(1, PAGES + 1):
        url = S24_BASE if page == 1 else f"{S24_BASE}?page={page}"
        html = fetch_html(url)
        if html:
            edges = extract_edges(html)
            total_edges += len(edges)
            nodes.extend(edges)
        if page < PAGES:
            time.sleep(REQUEST_GAP_SECONDS)

    matched = ingest_edges(db, nodes, today_str)
    prune_stale(db, today_str)
    try:
        db.commit()
    except Exception as e:
        print(f"Trends commit failed (non-fatal): {e}")
        db.rollback()
        return {"edges": total_edges, "matched": 0}

    print(f"Trends ingest: {total_edges} trend-cards seen, matched {matched} fixtures.")
    return {"edges": total_edges, "matched": matched}


if __name__ == "__main__":
    from app import SessionLocal

    db = SessionLocal()
    try:
        sync_trends(db)
    finally:
        db.close()
