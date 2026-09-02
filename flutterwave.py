"""
ValueBets Analytics — Paystack billing (fixed build, Sept 2026).

WHAT THIS FIXES (plain English):

1. CHECKOUT 500 ROOT CAUSE
   The payments ledger table was named "flutterwave_payments" (leftover of the
   old Flutterwave integration). The live database almost certainly still holds
   an OLD version of that table; Base.metadata.create_all() never alters an
   existing table, so the INSERT after Paystack answered crashed unhandled =
   HTTP 500 for every customer.
   -> The ledger now lives in a FRESH table "paystack_payments". The old table
      is left untouched in the database, harmlessly unused (it holds nothing of
      value — checkout never once succeeded, so there are no real payments in it).
   -> Even if a ledger write ever fails again, /initialize now logs it and STILL
      hands the customer their checkout URL. Checkout can never 500 on the
      ledger again.

2. WEBHOOK ACCEPTED FORGED EVENTS (security)
   The signature was only checked when a second env var (PAYSTACK_WEBHOOK_SECRET)
   existed. It was never set on Render, so unsigned webhooks were processed.
   Paystack signs webhooks with your normal secret key — no second secret needed.
   -> The signature is now ALWAYS verified with hmac.compare_digest, using
      PAYSTACK_WEBHOOK_SECRET when set, otherwise PAYSTACK_SECRET_KEY.
   -> If no usable secret is configured at all, webhooks are rejected with 503
      (fail closed) instead of being processed unsigned.
   -> A server-side re-verification call to Paystack still happens before any
      premium is granted (defense in depth, kept).

3. DOUBLE-GRANT RACE / SPLIT-GRANT
   The payment row and the user's premium extension were saved in two separate
   commits, and premium_granted was flagged BEFORE premium was actually added —
   a crash in between left the ledger lying, and a webhook+verify double-hit
   could stretch 30 days into 60.
   -> One atomic commit now writes BOTH (or neither). A replayed webhook, or
      verify + webhook firing together, can never extend premium twice for the
      same payment.

4. SMALL HARDENING
   - If the re-verification call to Paystack fails inside the webhook, we now
     answer 502 so Paystack retries the event (previously it was swallowed).
   - The webhook handler is a sync function reading Body()/Header(), so the
     blocking Paystack call runs in the threadpool instead of freezing the
     event loop.
   - New /health route (admin only): reports whether the required env vars are
     wired, so setup can be confirmed without reading server logs.

UNCHANGED on purpose: route paths (/api/billing/flutterwave/*), response
shapes, price/currency logic, tx_ref format, verify-redirect flow. The
grant_premium() and find_existing_payment() helpers are kept so any other file
importing them keeps working.
"""

import hmac
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import (
    Base,
    User,
    UserOut,
    engine,
    get_db,
    get_current_user,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # python-dotenv absent on the server is fine — env vars arrive via the host
    pass

logger = logging.getLogger("valuebets.billing")

PAYSTACK_SECRET_KEY = os.getenv("PAYSTACK_SECRET_KEY", "").strip()
PAYSTACK_WEBHOOK_SECRET = os.getenv("PAYSTACK_WEBHOOK_SECRET", "").strip()

try:
    PAYSTACK_PREMIUM_AMOUNT = float(os.getenv("PAYSTACK_PREMIUM_AMOUNT", "10000"))
except Exception:
    PAYSTACK_PREMIUM_AMOUNT = 10000.0

try:
    PREMIUM_DAYS = int(os.getenv("PAYSTACK_PREMIUM_DAYS", "30"))
except Exception:
    PREMIUM_DAYS = 30

PAYSTACK_CURRENCY = os.getenv("PAYSTACK_CURRENCY", "NGN").strip() or "NGN"

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://127.0.0.1:8000").rstrip("/")

PAYSTACK_API = "https://api.paystack.co"

router = APIRouter(prefix="/billing/flutterwave", tags=["paystack"])


# -----------------------------
# Payment model
# -----------------------------

class Payment(Base):
    # Fresh ledger table. The legacy "flutterwave_payments" table is abandoned
    # on purpose: create_all() never migrates old shapes, and the legacy shape
    # is what crashed /initialize with a 500 on every checkout attempt.
    __tablename__ = "paystack_payments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, nullable=False, index=True)

    tx_ref = Column(String(255), unique=True, index=True, nullable=False)
    flw_ref = Column(String(255), nullable=True, index=True)
    transaction_id = Column(String(255), nullable=True, index=True)

    amount = Column(Float, nullable=False)
    currency = Column(String(20), nullable=False)
    status = Column(String(50), nullable=False, default="pending")
    premium_granted = Column(Boolean, nullable=False, default=False)
    premium_days = Column(Integer, nullable=False, default=30)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


Base.metadata.create_all(bind=engine)


class PaymentOut(BaseModel):
    id: int
    user_id: int
    tx_ref: str
    flw_ref: Optional[str] = None
    transaction_id: Optional[str] = None
    amount: float
    currency: str
    status: str
    premium_granted: bool
    premium_days: int
    created_at: datetime

    model_config = {
        "from_attributes": True
    }


class InitializeResponse(BaseModel):
    payment_url: str
    tx_ref: str


# -----------------------------
# Helpers
# -----------------------------

def paystack_headers():
    return {
        "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
        "Content-Type": "application/json",
    }


def webhook_signing_secret() -> str:
    """Paystack signs webhooks with your secret key; PAYSTACK_WEBHOOK_SECRET
    (if someone set it) is honoured for backwards compatibility."""
    return PAYSTACK_WEBHOOK_SECRET or PAYSTACK_SECRET_KEY


def make_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt


def parse_user_id_from_tx_ref(tx_ref: str) -> Optional[int]:
    """
    tx_ref format:
    premium_{user_id}_{random}
    """
    try:
        parts = tx_ref.split("_")
        return int(parts[1])
    except Exception:
        return None


def parse_user_id_from_meta(meta) -> Optional[int]:
    if not meta:
        return None

    try:
        if isinstance(meta, dict):
            if meta.get("user_id"):
                return int(meta["user_id"])

        if isinstance(meta, list):
            for item in meta:
                if item.get("key") == "user_id":
                    return int(item.get("value"))
    except Exception:
        return None

    return None


def grant_premium(user: User, db: Session, days: int = PREMIUM_DAYS):
    """Kept for backwards compatibility with any other module that imports it.
    The hot path (process_verified_transaction) grants and records in ONE
    commit instead, so it never uses this."""
    now = datetime.now(timezone.utc)

    current_until = make_aware(user.premium_until)

    if current_until and current_until > now:
        new_until = current_until + timedelta(days=days)
    else:
        new_until = now + timedelta(days=days)

    user.is_premium = True
    user.plan = "premium"
    user.premium_until = new_until

    db.commit()
    db.refresh(user)


def find_existing_payment(db: Session, tx_ref: str, flw_ref: Optional[str], transaction_id: Optional[str]):
    payment = None

    if flw_ref:
        payment = db.query(Payment).filter(
            Payment.flw_ref == flw_ref
        ).first()

    if not payment and transaction_id:
        payment = db.query(Payment).filter(
            Payment.transaction_id == transaction_id
        ).first()

    if not payment:
        payment = db.query(Payment).filter(
            Payment.tx_ref == tx_ref
        ).first()

    return payment


def process_verified_transaction(db: Session, tx: dict) -> Optional[Payment]:
    """Grant premium for a transaction that PAYSTACK ITSELF has confirmed
    successful. Idempotent: calling it twice for the same reference never
    adds a second 30 days. User + ledger are written in ONE commit."""
    if not isinstance(tx, dict):
        return None

    if tx.get("status") != "success":
        return None

    tx_ref = tx.get("reference")

    if not tx_ref:
        return None

    user_id = parse_user_id_from_tx_ref(tx_ref)

    if not user_id:
        user_id = parse_user_id_from_meta(tx.get("metadata"))

    if not user_id:
        return None

    try:
        charged_amount = float(tx.get("amount", 0)) / 100.0
    except Exception:
        return None

    currency = tx.get("currency")

    if charged_amount < PAYSTACK_PREMIUM_AMOUNT:
        logger.warning("billing: underpayment rejected for %s (%.2f < %.2f)", tx_ref, charged_amount, PAYSTACK_PREMIUM_AMOUNT)
        return None

    if currency != PAYSTACK_CURRENCY:
        logger.warning("billing: wrong currency rejected for %s (%s)", tx_ref, currency)
        return None

    user = db.get(User, user_id)

    if not user:
        return None

    # tx_ref (== the reference Paystack stores) is the ONLY authoritative key:
    # we generated it, it is unique per payment, and it is signed into the
    # transaction. Never cross-match on Paystack transaction ids — a colliding
    # or reused id could graft onto the wrong payment and block the grant.
    payment = db.query(Payment).filter(Payment.tx_ref == tx_ref).first()

    if payment and payment.premium_granted:
        # webhook retry / verify-redirect double hit — do NOT extend again
        return payment

    now = datetime.now(timezone.utc)
    current_until = make_aware(user.premium_until)
    new_until = (current_until if current_until and current_until > now else now) + timedelta(days=PREMIUM_DAYS)

    if not payment:
        payment = Payment(user_id=user.id, tx_ref=tx_ref)

    payment.user_id = user.id
    payment.tx_ref = tx_ref
    payment.flw_ref = tx.get("reference") or payment.flw_ref
    payment.transaction_id = str(tx.get("id") or "") or payment.transaction_id
    payment.amount = charged_amount
    payment.currency = currency
    payment.status = "successful"
    payment.premium_days = PREMIUM_DAYS
    payment.premium_granted = True

    user.is_premium = True
    user.plan = "premium"
    user.premium_until = new_until

    db.add(payment)
    db.add(user)

    try:
        db.commit()
    except IntegrityError:
        # Lost a race: another worker already granted this same reference.
        db.rollback()
        logger.info("billing: concurrent grant for %s — ledger already has it", tx_ref)
        return db.query(Payment).filter(Payment.tx_ref == tx_ref).first()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("billing: atomic grant failed for %s", tx_ref)
        return None

    db.refresh(payment)
    logger.info("billing: premium granted to user %s until %s (ref %s)", user.id, new_until.isoformat(), tx_ref)
    return payment


def verify_with_paystack(tx_ref: str) -> Optional[dict]:
    """Ask Paystack directly for the truth about a reference. Returns the
    transaction dict only when Paystack confirms it, else None."""
    try:
        response = requests.get(
            f"{PAYSTACK_API}/transaction/verify/{tx_ref}",
            headers=paystack_headers(),
            timeout=30,
        )
    except Exception:
        return None

    try:
        data = response.json()
    except Exception:
        return None

    if response.status_code != 200 or data.get("status") is not True:
        return None

    tx = data.get("data", {})
    return tx if isinstance(tx, dict) else None


# -----------------------------
# Routes
# -----------------------------

@router.get("/health")
def paystack_billing_health(user: User = Depends(get_current_user)):
    """Admin-only setup check: which env wiring is present (no secret values
    are ever returned)."""
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only.")

    secret = PAYSTACK_SECRET_KEY
    mode = "missing"
    if secret.startswith("sk_live"):
        mode = "live"
    elif secret.startswith("sk_test"):
        mode = "test"

    return {
        "paystack_configured": bool(secret),
        "key_mode": mode,
        "webhook_signature_protected": bool(webhook_signing_secret()),
        "backend_url": BACKEND_URL,
        "frontend_url": FRONTEND_URL,
        "callback_url": f"{BACKEND_URL}/api/billing/flutterwave/verify",
        "amount": PAYSTACK_PREMIUM_AMOUNT,
        "currency": PAYSTACK_CURRENCY,
        "premium_days": PREMIUM_DAYS,
    }


@router.get("/status")
def paystack_billing_status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    payments = db.query(Payment).filter(
        Payment.user_id == user.id
    ).order_by(Payment.id.desc()).limit(20).all()

    return {
        "user": UserOut.model_validate(user),
        "payments": [
            PaymentOut.model_validate(payment)
            for payment in payments
        ],
    }


@router.post("/initialize", response_model=InitializeResponse)
def initialize_payment(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not PAYSTACK_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Paystack is not configured. Set PAYSTACK_SECRET_KEY in .env.",
        )

    tx_ref = f"premium_{user.id}_{uuid.uuid4().hex}"

    payload = {
        "reference": tx_ref,
        "email": user.email,
        "amount": int(PAYSTACK_PREMIUM_AMOUNT * 100),
        "currency": PAYSTACK_CURRENCY,
        "callback_url": f"{BACKEND_URL}/api/billing/flutterwave/verify",
        "metadata": {
            "user_id": user.id,
            "product": "premium",
            "premium_days": PREMIUM_DAYS,
        },
    }

    try:
        response = requests.post(
            f"{PAYSTACK_API}/transaction/initialize",
            json=payload,
            headers=paystack_headers(),
            timeout=30,
        )
    except Exception:
        logger.exception("billing: could not reach Paystack /transaction/initialize")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not connect to Paystack.",
        )

    try:
        data = response.json()
    except Exception:
        logger.error("billing: non-JSON Paystack initialize response (HTTP %s)", response.status_code)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid Paystack response.",
        )

    if response.status_code != 200 or data.get("status") is not True:
        logger.warning("billing: Paystack initialize rejected (HTTP %s): %s", response.status_code, data.get("message"))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=data.get("message", "Paystack payment initialization failed."),
        )

    payment_url = data.get("data", {}).get("authorization_url")

    if not payment_url:
        logger.error("billing: Paystack initialize returned no authorization_url")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Paystack did not return a payment URL.",
        )

    # Ledger is a receipt, not a gate: never let a ledger hiccup 500 the checkout.
    try:
        payment = Payment(
            user_id=user.id,
            tx_ref=tx_ref,
            amount=PAYSTACK_PREMIUM_AMOUNT,
            currency=PAYSTACK_CURRENCY,
            status="pending",
            premium_granted=False,
            premium_days=PREMIUM_DAYS,
        )
        db.add(payment)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        logger.exception("billing: pending ledger write failed for %s (non-fatal, checkout continues)", tx_ref)

    return InitializeResponse(
        payment_url=payment_url,
        tx_ref=tx_ref,
    )


@router.get("/verify")
def verify_payment(
    reference: Optional[str] = None,
    trxref: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if not PAYSTACK_SECRET_KEY:
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=error")

    tx_ref = reference or trxref

    if not tx_ref:
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=cancelled")

    tx = verify_with_paystack(tx_ref)

    if tx is None:
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=error")

    payment = process_verified_transaction(db, tx)

    if payment:
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=success")

    return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=error")


@router.post("/webhook")
def paystack_webhook(
    body: bytes = Body(default=b""),
    x_paystack_signature: Optional[str] = Header(default=None),
    db: Session = Depends(get_db),
):
    secret = webhook_signing_secret()

    if not secret:
        # Fail closed: never process unsigned webhook traffic.
        logger.error("billing: webhook received but no Paystack secret is configured — rejecting")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook is not configured.",
        )

    computed = hmac.new(secret.encode("utf-8"), body, hashlib.sha512).hexdigest()

    if not x_paystack_signature or not hmac.compare_digest(x_paystack_signature, computed):
        logger.warning("billing: webhook with invalid signature rejected")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Paystack webhook signature.",
        )

    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return {
            "status": "invalid_payload",
        }

    event = str(payload.get("event", "")).lower()
    data = payload.get("data", {})

    if event == "charge.success" and isinstance(data, dict):
        tx_ref = data.get("reference")

        if tx_ref:
            tx = verify_with_paystack(tx_ref)

            if tx is None:
                # Could not confirm with Paystack right now — answer 502 so
                # Paystack retries the webhook instead of dropping the event.
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail="Could not verify transaction with Paystack.",
                )

            process_verified_transaction(db, tx)

    return {
        "status": "ok",
    }
