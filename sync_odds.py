import os
import requests
from difflib import SequenceMatcher
from sqlalchemy.orm import Session
from dotenv import load_dotenv

from app import SessionLocal
from sports_models import Fixture, Prediction, OddsInsight

load_dotenv()

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

# The Odds API requires specific league keys, not a generic "soccer" key
SOCCER_SPORTS_KEYS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_italy_serie_a",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_europa_league"
]

# We only care if our model is at least 4% better than the bookmaker
VALUE_THRESHOLD = 0.04 

def similar(a, b):
    if not a or not b:
        return False
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.80

def fetch_odds():
    if not ODDS_API_KEY:
        print("ODDS_API_KEY is missing.")
        return []

    all_events = []
    
    # Loop through the top leagues
    for sport_key in SOCCER_SPORTS_KEYS:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        params = {
            "apiKey": ODDS_API_KEY,
            "regions": "eu",          
            "markets": "h2h,totals",  
            "oddsFormat": "decimal"
        }

        try:
            response = requests.get(url, params=params, timeout=30)
            if response.status_code == 200:
                events = response.json()
                all_events.extend(events)
                print(f"Fetched {len(events)} events for {sport_key}")
            else:
                print(f"Odds API Error for {sport_key}: {response.json()}")
        except Exception as e:
            print(f"Failed to fetch odds for {sport_key}: {e}")
            
    print(f"Total events fetched from Odds API: {len(all_events)}")
    return all_events

def calculate_true_prob(odds_list):
    """Removes the bookmaker's margin (overround) to find the true market probability"""
    if not odds_list or len(odds_list) < 2:
        return None
        
    implied = [1.0 / o for o in odds_list if o > 1.0]
    margin = sum(implied) - 1.0
    
    if margin <= 0:
        return None
        
    # Normalize to 100%
    true_probs = [i / (1.0 + margin) for i in implied]
    return true_probs

def match_and_save_odds(db: Session):
    odds_events = fetch_odds()
    if not odds_events:
        print("No odds events to process.")
        return

    # Get upcoming fixtures from our database that have predictions
    fixtures = db.query(Fixture).filter(Fixture.status.in_(["NS", "TBD", "NOT_STARTED"])).all()
    
    value_bets_found = 0
    processed_fixtures = 0

    for fixture in fixtures:
        # Find the matching odds event using fuzzy matching
        matched_event = None
        for event in odds_events:
            if similar(fixture.home_team, event.get("home_team", "")) and \
               similar(fixture.away_team, event.get("away_team", "")):
                matched_event = event
                break
                
        if not matched_event:
            continue

        processed_fixtures += 1
        bookmakers = matched_event.get("bookmakers", [])
        
        # --- 1. Match Winner (1X2) -> Double Chance 1X ---
        h2h_odds = []
        for bookmaker in bookmakers:
            for market in bookmaker.get("markets", []):
                if market.get("key") == "h2h":
                    outcomes = {o["name"]: o["price"] for o in market.get("outcomes", [])}
                    if "home" in outcomes and "away" in outcomes and "draw" in outcomes:
                        h2h_odds.append([outcomes["home"], outcomes["draw"], outcomes["away"]])
        
        if h2h_odds:
            avg_home = sum(o[0] for o in h2h_odds) / len(h2h_odds)
            avg_draw = sum(o[1] for o in h2h_odds) / len(h2h_odds)
            avg_away = sum(o[2] for o in h2h_odds) / len(h2h_odds)
            
            true_probs = calculate_true_prob([avg_home, avg_draw, avg_away])
            if true_probs:
                market_home_prob = true_probs[0]
                market_draw_prob = true_probs[1]
                market_dc_1x = market_home_prob + market_draw_prob
                
                model_pred = db.query(Prediction).filter(
                    Prediction.fixture_id == fixture.id,
                    Prediction.market == "DC_1X"
                ).first()
                
                if model_pred:
                    edge = model_pred.probability - market_dc_1x
                    
                    # Delete old insight if exists to prevent duplicates
                    db.query(OddsInsight).filter(
                        OddsInsight.fixture_id == fixture.id,
                        OddsInsight.market == "DC_1X"
                    ).delete()

                    db.add(OddsInsight(
                        fixture_id=fixture.id,
                        market="DC_1X",
                        bookmaker_odds=avg_home, 
                        market_prob=market_dc_1x,
                        model_prob=model_pred.probability,
                        value_edge=edge,
                        is_value_bet=(edge >= VALUE_THRESHOLD)
                    ))
                    if edge >= VALUE_THRESHOLD:
                        value_bets_found += 1

        # --- 2. Over/Under 2.5 Goals ---
        over_odds = []
        under_odds = []
        
        for bookmaker in bookmakers:
            for market in bookmaker.get("markets", []):
                if market.get("key") == "totals" and market.get("point") == 2.5:
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == "Over":
                            over_odds.append(outcome["price"])
                        elif outcome.get("name") == "Under":
                            under_odds.append(outcome["price"])
                            
        if over_odds and under_odds:
            avg_over = sum(over_odds) / len(over_odds)
            avg_under = sum(under_odds) / len(under_odds)
            
            true_probs = calculate_true_prob([avg_over, avg_under])
            if true_probs:
                market_over_prob = true_probs[0]
                
                model_pred = db.query(Prediction).filter(
                    Prediction.fixture_id == fixture.id,
                    Prediction.market == "OVER_25"
                ).first()
                
                if model_pred:
                    edge = model_pred.probability - market_over_prob
                    
                    db.query(OddsInsight).filter(
                        OddsInsight.fixture_id == fixture.id,
                        OddsInsight.market == "OVER_25"
                    ).delete()

                    db.add(OddsInsight(
                        fixture_id=fixture.id,
                        market="OVER_25",
                        bookmaker_odds=avg_over,
                        market_prob=market_over_prob,
                        model_prob=model_pred.probability,
                        value_edge=edge,
                        is_value_bet=(edge >= VALUE_THRESHOLD)
                    ))
                    if edge >= VALUE_THRESHOLD:
                        value_bets_found += 1

    db.commit()
    print(f"Odds sync complete. Matched {processed_fixtures} fixtures. Found {value_bets_found} mathematical value bets.")

if __name__ == "__main__":
    db = SessionLocal()
    match_and_save_odds(db)
    db.close()