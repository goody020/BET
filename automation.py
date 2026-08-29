import os
import threading
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from app import SessionLocal
from sync_sports import sync_fixtures_and_predictions
from settle_predictions import update_fixtures_from_api, settle_all
from sync_odds import match_and_save_odds


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
    from sports_models import AutomationState

    db = SessionLocal()
    try:
        state = {}
        for row in db.query(AutomationState).all():
            state[row.state_key] = row.state_value
        return state
    except Exception:
        return {}
    finally:
        db.close()


def update_state(key):
    from sports_models import AutomationState

    db = SessionLocal()
    try:
        row = db.query(AutomationState).filter(
            AutomationState.state_key == key
        ).first()

        value = datetime.now(timezone.utc).isoformat()

        if not row:
            db.add(AutomationState(state_key=key, state_value=value))
        else:
            row.state_value = value

        db.commit()
    except Exception as e:
        print(f"Automation state error: {e}")
    finally:
        db.close()


def pin_daily_board():
    """Freeze today's card after a successful sync (write-once per day).
    Runs in its own session + try/except so a pin hiccup can never break the sync."""
    try:
        from picks import store_board_pins

        db = SessionLocal()
        try:
            result = store_board_pins(db, date.today())
        finally:
            db.close()

        if result == "pinned":
            print("Automation: today's board pinned (card frozen until tomorrow's sync).")
        elif result == "exists":
            print("Automation: board already pinned today (card unchanged).")
        else:
            print("Automation: nothing to pin today (empty pool, live fallback stays on).")
    except Exception as e:
        print(f"Board pin error (non-fatal): {e}")


def run_sync():
    with _lock:
        if RUNNING["sync"]:
            return {"status": "already_running"}
        RUNNING["sync"] = True

    try:
        print("Automation: starting fixture sync...")
        sync_fixtures_and_predictions()

        print("Automation: starting odds sync...")
        db = SessionLocal()
        match_and_save_odds(db)
        db.close()

        pin_daily_board()

        update_state("last_sync")
        print("Automation: fixture sync complete.")
        return {"status": "success"}
    except Exception as e:
        import traceback
        print(f"Automation sync error: {e}")
        traceback.print_exc()
        return {"status": "error", "detail": str(e)}
    finally:
        with _lock:
            RUNNING["sync"] = False


def run_settlement():
    with _lock:
        if RUNNING["settlement"]:
            return {"status": "already_running"}
        RUNNING["settlement"] = True

    try:
        print("Automation: starting settlement...")
        db = SessionLocal()
        updated = update_fixtures_from_api(db)
        counts = settle_all(db)
        db.close()

        update_state("last_settlement")
        print("Automation: settlement complete.")
        return {"status": "success", "updated_fixtures": updated, "counts": counts}
    except Exception as e:
        print(f"Automation settlement error: {e}")
        return {"status": "error", "detail": str(e)}
    finally:
        with _lock:
            RUNNING["settlement"] = False


def run_all():
    settlement_result = run_settlement()
    sync_result = run_sync()
    return {"settlement": settlement_result, "sync": sync_result}


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


_last_auto_check = {"time": None}


def maybe_run_automation():
    try:
        now = datetime.now(timezone.utc)

        if _last_auto_check["time"] and (now - _last_auto_check["time"]) < timedelta(minutes=10):
            return

        _last_auto_check["time"] = now

        state = load_state()

        def is_stale(value, hours):
            if not value:
                return True
            try:
                then = datetime.fromisoformat(value)
            except Exception:
                return True
            return (now - then) > timedelta(hours=hours)

        jobs = []

        if is_stale(state.get("last_settlement"), 12):
            jobs.append(run_settlement)

        if is_stale(state.get("last_sync"), 20):
            jobs.append(run_sync)

        if not jobs:
            return

        def runner():
            for job in jobs:
                job()

        threading.Thread(target=runner, daemon=True).start()
    except Exception as e:
        print(f"Automation check error: {e}")
