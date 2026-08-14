import os
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from app import SessionLocal
from sync_sports import sync_fixtures_and_predictions
from settle_predictions import update_fixtures_from_api, settle_all


STATE_FILE = Path(__file__).resolve().parent / "automation_state.json"

AUTOMATION_ENABLED = os.getenv("AUTOMATION_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

try:
    SYNC_HOUR = int(os.getenv("SYNC_HOUR", "7"))
except Exception:
    SYNC_HOUR = 7

try:
    SYNC_MINUTE = int(os.getenv("SYNC_MINUTE", "0"))
except Exception:
    SYNC_MINUTE = 0

try:
    SETTLEMENT_INTERVAL_HOURS = int(os.getenv("SETTLEMENT_INTERVAL_HOURS", "12"))
except Exception:
    SETTLEMENT_INTERVAL_HOURS = 12


_lock = threading.Lock()

RUNNING = {
    "sync": False,
    "settlement": False,
}

scheduler = BackgroundScheduler()


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_state(key):
    state = load_state()
    state[key] = datetime.now(timezone.utc).isoformat()
    save_state(state)


def run_sync():
    with _lock:
        if RUNNING["sync"]:
            return {
                "status": "already_running",
            }

        RUNNING["sync"] = True

    try:
        print("Automation: starting fixture sync...")

        sync_fixtures_and_predictions()

        update_state("last_sync")

        print("Automation: fixture sync complete.")

        return {
            "status": "success",
        }
    except Exception as e:
        print(f"Automation sync error: {e}")

        return {
            "status": "error",
            "detail": str(e),
        }
    finally:
        with _lock:
            RUNNING["sync"] = False


def run_settlement():
    with _lock:
        if RUNNING["settlement"]:
            return {
                "status": "already_running",
            }

        RUNNING["settlement"] = True

    try:
        print("Automation: starting settlement...")

        db = SessionLocal()

        updated = update_fixtures_from_api(db)
        counts = settle_all(db)

        db.close()

        update_state("last_settlement")

        print("Automation: settlement complete.")

        return {
            "status": "success",
            "updated_fixtures": updated,
            "counts": counts,
        }
    except Exception as e:
        print(f"Automation settlement error: {e}")

        return {
            "status": "error",
            "detail": str(e),
        }
    finally:
        with _lock:
            RUNNING["settlement"] = False


def run_all():
    settlement_result = run_settlement()
    sync_result = run_sync()

    return {
        "settlement": settlement_result,
        "sync": sync_result,
    }


def start_scheduler():
    if not AUTOMATION_ENABLED:
        print("Automation disabled.")
        return

    if scheduler.running:
        return

    scheduler.add_job(
        run_sync,
        "cron",
        hour=SYNC_HOUR,
        minute=SYNC_MINUTE,
        id="daily_sync",
        replace_existing=True,
    )

    scheduler.add_job(
        run_settlement,
        "interval",
        hours=SETTLEMENT_INTERVAL_HOURS,
        id="settlement_interval",
        replace_existing=True,
    )

    scheduler.start()

    print(
        f"Automation started: sync daily at {SYNC_HOUR:02d}:{SYNC_MINUTE:02d}, "
        f"settlement every {SETTLEMENT_INTERVAL_HOURS} hours."
    )


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        print("Automation stopped.")