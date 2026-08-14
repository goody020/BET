import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.orm import Session

from app import (
    Base,
    User,
    UserOut,
    engine,
    get_db,
    get_current_user,
)


load_dotenv()

FLW_SECRET_KEY = os.getenv("FLUTTERWAVE_SECRET_KEY", "").strip()
FLW_SECRET_HASH = os.getenv("FLUTTERWAVE_SECRET_HASH", "").strip()

try:
    FLW_PREMIUM_AMOUNT = float(os.getenv("FLUTTERWAVE_PREMIUM_AMOUNT", "10000"))
except Exception:
    FLW_PREMIUM_AMOUNT = 10000.0

try:
    PREMIUM_DAYS = int(os.getenv("FLUTTERWAVE_PREMIUM_DAYS", "30"))
except Exception:
    PREMIUM_DAYS = 30

FLW_CURRENCY = os.getenv("FLUTTERWAVE_CURRENCY", "NGN").strip() or "NGN"

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://127.0.0.1:8000").rstrip("/")

FLW_API = "https://api.flutterwave.com/v3"

router = APIRouter(prefix="/billing/flutterwave", tags=["flutterwave"])


# -----------------------------
# Payment model
# -----------------------------

class Payment(Base):
    __tablename__ = "flutterwave_payments"

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

def flw_headers():
    return {
        "Authorization": f"Bearer {FLW_SECRET_KEY}",
        "Content-Type": "application/json",
    }


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
    if not isinstance(tx, dict):
        return None

    if tx.get("status") != "successful":
        return None

    tx_ref = tx.get("tx_ref")

    if not tx_ref:
        return None

    user_id = parse_user_id_from_tx_ref(tx_ref)

    if not user_id:
        user_id = parse_user_id_from_meta(tx.get("meta"))

    if not user_id:
        return None

    user = db.get(User, user_id)

    if not user:
        return None

    try:
        charged_amount = float(tx.get("charged_amount", tx.get("amount", 0)))
    except Exception:
        charged_amount = 0.0

    currency = tx.get("currency")

    if charged_amount < FLW_PREMIUM_AMOUNT:
        return None

    if currency != FLW_CURRENCY:
        return None

    flw_ref = tx.get("flw_ref")
    transaction_id = str(tx.get("id") or tx.get("transaction_id") or "")

    payment = find_existing_payment(db, tx_ref, flw_ref, transaction_id)

    if payment and payment.premium_granted:
        return payment

    if not payment:
        payment = Payment(
            user_id=user.id,
            tx_ref=tx_ref,
            amount=charged_amount,
            currency=currency,
            status="successful",
            premium_granted=False,
            premium_days=PREMIUM_DAYS,
        )

    payment.user_id = user.id
    payment.tx_ref = tx_ref
    payment.flw_ref = flw_ref or payment.flw_ref
    payment.transaction_id = transaction_id or payment.transaction_id
    payment.amount = charged_amount
    payment.currency = currency
    payment.status = "successful"
    payment.premium_granted = True
    payment.premium_days = PREMIUM_DAYS

    db.add(payment)
    db.commit()
    db.refresh(payment)

    grant_premium(user, db, PREMIUM_DAYS)

    return payment


# -----------------------------
# Routes
# -----------------------------

@router.get("/status")
def flutterwave_billing_status(
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
    if not FLW_SECRET_KEY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Flutterwave is not configured. Set FLUTTERWAVE_SECRET_KEY in .env.",
        )

    tx_ref = f"premium_{user.id}_{uuid.uuid4().hex}"

    payload = {
        "tx_ref": tx_ref,
        "amount": int(FLW_PREMIUM_AMOUNT),
        "currency": FLW_CURRENCY,
        "redirect_url": f"{BACKEND_URL}/api/billing/flutterwave/verify",
        "customer": {
            "email": user.email,
        },
        "meta": [
            {
                "key": "user_id",
                "value": str(user.id),
            },
            {
                "key": "product",
                "value": "premium",
            },
            {
                "key": "premium_days",
                "value": str(PREMIUM_DAYS),
            },
        ],
        "customizations": {
            "title": "Premium Access",
            "description": "Premium predictions",
        },
    }

    try:
        response = requests.post(
            f"{FLW_API}/payments",
            json=payload,
            headers=flw_headers(),
            timeout=30,
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not connect to Flutterwave.",
        )

    try:
        data = response.json()
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Invalid Flutterwave response.",
        )

    if response.status_code != 200 or data.get("status") != "success":
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=data.get("message", "Flutterwave payment initialization failed."),
        )

    payment_url = data.get("data", {}).get("link")

    if not payment_url:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Flutterwave did not return a payment URL.",
        )

    payment = Payment(
        user_id=user.id,
        tx_ref=tx_ref,
        amount=FLW_PREMIUM_AMOUNT,
        currency=FLW_CURRENCY,
        status="pending",
        premium_granted=False,
        premium_days=PREMIUM_DAYS,
    )

    db.add(payment)
    db.commit()

    return InitializeResponse(
        payment_url=payment_url,
        tx_ref=tx_ref,
    )


@router.get("/verify")
def verify_payment(
    transaction_id: Optional[str] = None,
    tx_ref: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    if not FLW_SECRET_KEY:
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=error")

    if not transaction_id:
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=cancelled")

    try:
        response = requests.get(
            f"{FLW_API}/transactions/{transaction_id}/verify",
            headers=flw_headers(),
            timeout=30,
        )
    except Exception:
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=error")

    try:
        data = response.json()
    except Exception:
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=error")

    if response.status_code != 200 or data.get("status") != "success":
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=error")

    tx = data.get("data", {})

    payment = process_verified_transaction(db, tx)

    if payment:
        return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=success")

    return RedirectResponse(url=f"{FRONTEND_URL}/app?payment=error")


@router.post("/webhook")
async def flutterwave_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    signature = request.headers.get("verifi-hash")

    if FLW_SECRET_HASH and signature != FLW_SECRET_HASH:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Flutterwave webhook signature.",
        )

    try:
        payload = await request.json()
    except Exception:
        return {
            "status": "invalid_payload",
        }

    event = str(payload.get("event", "")).lower()
    data = payload.get("data", {})

    transaction_id = None

    if isinstance(data, dict):
        transaction_id = data.get("id")

    if not transaction_id:
        transaction_id = payload.get("id")

    should_process = event in [
        "charge.completed",
        "charge.success",
        "payment.completed",
        "transaction.success",
        "subscription.create",
    ]

    if transaction_id and should_process:
        try:
            response = requests.get(
                f"{FLW_API}/transactions/{transaction_id}/verify",
                headers=flw_headers(),
                timeout=30,
            )

            if response.status_code == 200:
                response_data = response.json()

                if response_data.get("status") == "success":
                    tx = response_data.get("data", {})
                    process_verified_transaction(db, tx)
        except Exception:
            pass

    return {
        "status": "ok",
    }