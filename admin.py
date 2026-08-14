import os
import threading
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import User, get_db, get_current_user
from flutterwave import Payment
from automation import (
    AUTOMATION_ENABLED,
    RUNNING,
    SETTLEMENT_INTERVAL_HOURS,
    SYNC_HOUR,
    SYNC_MINUTE,
    load_state,
    run_all,
    run_settlement,
    run_sync,
)


ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.getenv("ADMIN_EMAILS", "").split(",")
    if email.strip()
}

router = APIRouter(prefix="/admin", tags=["admin"])


def require_admin(user: User = Depends(get_current_user)):
    if user.email.lower() not in ADMIN_EMAILS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access denied",
        )
    return user


def start_background_job(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()


class GrantPremiumRequest(BaseModel):
    days: int = 30


@router.get("/check")
def check_admin(user: User = Depends(get_current_user)):
    return {"is_admin": user.email.lower() in ADMIN_EMAILS}


@router.get("/automation/status")
def automation_status(user: User = Depends(require_admin)):
    return {
        "automation_enabled": AUTOMATION_ENABLED,
        "sync_time": f"{SYNC_HOUR:02d}:{SYNC_MINUTE:02d}",
        "settlement_interval_hours": SETTLEMENT_INTERVAL_HOURS,
        "running": RUNNING,
        "state": load_state(),
    }


@router.post("/automation/sync")
def trigger_sync(user: User = Depends(require_admin)):
    if RUNNING["sync"]:
        return {"status": "already_running"}
    start_background_job(run_sync)
    return {"status": "sync_started"}


@router.post("/automation/settle")
def trigger_settlement(user: User = Depends(require_admin)):
    if RUNNING["settlement"]:
        return {"status": "already_running"}
    start_background_job(run_settlement)
    return {"status": "settlement_started"}


@router.post("/automation/run-all")
def trigger_run_all(user: User = Depends(require_admin)):
    if RUNNING["sync"] or RUNNING["settlement"]:
        return {"status": "already_running"}
    start_background_job(run_all)
    return {"status": "run_all_started"}


@router.get("/users")
def list_users(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    users = db.query(User).order_by(User.id.desc()).limit(500).all()
    return [
        {
            "id": u.id,
            "email": u.email,
            "plan": u.plan,
            "is_premium": u.is_premium,
            "premium_until": u.premium_until.isoformat() if u.premium_until else None,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in users
    ]


@router.post("/users/{user_id}/grant")
def grant_premium_manual(
    user_id: int,
    req: GrantPremiumRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    target_user = db.get(User, user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    now = datetime.now(timezone.utc)
    current_until = target_user.premium_until

    if current_until and current_until.tzinfo is None:
        current_until = current_until.replace(tzinfo=timezone.utc)

    if current_until and current_until > now:
        new_until = current_until + timedelta(days=req.days)
    else:
        new_until = now + timedelta(days=req.days)

    target_user.is_premium = True
    target_user.plan = "premium"
    target_user.premium_until = new_until

    db.commit()
    db.refresh(target_user)

    return {
        "status": "success",
        "user_id": target_user.id,
        "email": target_user.email,
        "premium_until": target_user.premium_until.isoformat(),
    }


@router.get("/payments")
def list_payments(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    payments = db.query(Payment).order_by(Payment.id.desc()).limit(500).all()
    return [
        {
            "id": p.id,
            "user_id": p.user_id,
            "amount": p.amount,
            "currency": p.currency,
            "status": p.status,
            "premium_granted": p.premium_granted,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in payments
    ]