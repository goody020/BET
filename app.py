import os
from datetime import datetime, timedelta, timezone
from math import exp, factorial
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field, computed_field
from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker


# -----------------------------
# Database setup
# -----------------------------

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)

    is_premium = Column(Boolean, nullable=False, default=False)
    plan = Column(String(20), nullable=False, default="free")
    premium_until = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


Base.metadata.create_all(bind=engine)


# -----------------------------
# App setup
# -----------------------------

app = FastAPI(title="CS/HTFT Prediction API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

ph = PasswordHasher()
security = HTTPBearer(auto_error=False)

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "10080"))


# -----------------------------
# Pydantic schemas
# -----------------------------

class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: EmailStr
    is_premium: bool
    plan: str
    premium_until: Optional[datetime] = None
    created_at: datetime

    model_config = {
        "from_attributes": True
    }

    @computed_field
    @property
    def is_admin(self) -> bool:
        admin_emails = {
            e.strip().lower()
            for e in os.getenv("ADMIN_EMAILS", "").split(",")
            if e.strip()
        }
        return self.email.lower() in admin_emails


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class PredictRequest(BaseModel):
    home_team: str = Field(..., min_length=1)
    away_team: str = Field(..., min_length=1)
    home_xg: Optional[float] = Field(default=None, ge=0.0, le=6.0)
    away_xg: Optional[float] = Field(default=None, ge=0.0, le=6.0)
    data_quality: Optional[float] = Field(default=0.35, ge=0.0, le=1.0)


# -----------------------------
# Prediction helpers
# -----------------------------

def poisson(k: int, lam: float) -> float:
    lam = max(float(lam), 0.0001)
    return exp(-lam) * (lam ** k) / factorial(k)


def result_str(h: int, a: int) -> str:
    if h > a:
        return "H"
    if a > h:
        return "A"
    return "D"


def score_matrix(lh: float, la: float, max_goals: int = 6):
    raw = {}
    total = 0.0

    for h in range(max_goals + 1):
        for a in range(max_goals + 1):
            p = poisson(h, lh) * poisson(a, la)
            raw[(h, a)] = p
            total += p

    if total <= 0:
        return {(0, 0): 1.0}

    return {k: v / total for k, v in raw.items()}


def ht_ft(lh: float, la: float, max_ht: int = 3, max_add: int = 4):
    lh1, la1 = lh * 0.45, la * 0.45
    lh2, la2 = lh * 0.55, la * 0.55

    raw = {}
    total = 0.0

    for hh in range(max_ht + 1):
        for ha in range(max_ht + 1):
            p_ht = poisson(hh, lh1) * poisson(ha, la1)
            ht_res = result_str(hh, ha)

            for sh in range(max_add + 1):
                for sa in range(max_add + 1):
                    p_add = poisson(sh, lh2) * poisson(sa, la2)

                    ft_h = hh + sh
                    ft_a = ha + sa
                    ft_res = result_str(ft_h, ft_a)

                    key = f"{ht_res}/{ft_res}"
                    p = p_ht * p_add

                    raw[key] = raw.get(key, 0.0) + p
                    total += p

    if total <= 0:
        return {"D/D": 1.0}

    return {k: v / total for k, v in raw.items()}


def confidence_score(prob: float, data_quality: float, market: str) -> int:
    dq = max(0.0, min(1.0, float(data_quality)))

    if market == "CS":
        p_norm = min(1.0, float(prob) / 0.18)
    else:
        p_norm = min(1.0, float(prob) / 0.40)

    score = (0.55 * p_norm + 0.45 * dq) * 100
    return max(5, min(94, int(round(score))))


def confidence_label(score: int) -> str:
    if score >= 75:
        return "High"
    if score >= 55:
        return "Medium"
    return "Low"


def verification_notes(data_quality: float):
    dq = float(data_quality)

    notes = [
        "This backend does not yet independently verify team strength. Sports API integration comes next.",
        "Data quality is currently self-reported/manual.",
    ]

    if dq < 0.55:
        notes.append("Low data quality: do not treat this as a high-confidence pick.")

    if dq < 0.75:
        notes.append("Missing verified lineups, injuries, suspensions, motivation, and odds movement.")

    notes.extend([
        "Check last 5 matches, home/away form, goals for/against, xG, H2H, fatigue, weather, and team news.",
        "Compare model probability against bookmaker implied probability. If there is no positive edge, do not bet.",
        "Correct score and HT/FT are high-variance markets. No prediction is guaranteed.",
        "Never advise users to chase losses or bet money they cannot afford to lose.",
    ])

    return notes


# -----------------------------
# Auth helpers
# -----------------------------

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_access_token(user_id: int) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "exp": expires,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def make_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)

    return dt


def refresh_premium_status(user: User, db: Session):
    now = datetime.now(timezone.utc)
    until = make_aware(user.premium_until)

    if until and until > now:
        if not user.is_premium:
            user.is_premium = True
            user.plan = "premium"
            db.commit()
            db.refresh(user)
    else:
        if user.is_premium:
            user.is_premium = False
            user.plan = "free"
            db.commit()
            db.refresh(user)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not credentials or credentials.scheme.lower() != "bearer":
        raise unauthorized

    token = credentials.credentials

    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload.get("sub"))
    except Exception:
        raise unauthorized

    user = db.get(User, user_id)

    if not user:
        raise unauthorized

    refresh_premium_status(user, db)

    return user


# -----------------------------
# Routes
# -----------------------------

@app.get("/")
def health():
    return {
        "status": "ok",
        "message": "CS/HTFT prediction API is running.",
    }


@app.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(req: RegisterRequest, db: Session = Depends(get_db)):
    email = req.email.lower()

    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    password_hash = ph.hash(req.password)

    user = User(
        email=email,
        password_hash=password_hash,
        is_premium=False,
        plan="free",
    )

    db.add(user)
    db.commit()
    db.refresh(user)

    return user


@app.post("/auth/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    email = req.email.lower()

    user = db.query(User).filter(User.email == email).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    try:
        ph.verify(user.password_hash, req.password)
    except VerifyMismatchError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )

    refresh_premium_status(user, db)

    access_token = create_access_token(user.id)

    return TokenResponse(
        access_token=access_token,
        user=UserOut.model_validate(user),
    )


@app.get("/auth/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user


@app.post("/predictions/predict")
def predict(req: PredictRequest, user: User = Depends(get_current_user)):
    lh = req.home_xg if req.home_xg is not None else 1.35
    la = req.away_xg if req.away_xg is not None else 1.15
    dq = req.data_quality if req.data_quality is not None else 0.35

    scores = score_matrix(lh, la)
    top_scores = sorted(scores.items(), key=lambda item: item[1], reverse=True)[:5]

    htft_probs = ht_ft(lh, la)
    top_htft = sorted(htft_probs.items(), key=lambda item: item[1], reverse=True)[:5]

    best_score, best_score_prob = top_scores[0]
    best_htft, best_htft_prob = top_htft[0]

    cs_score = confidence_score(best_score_prob, dq, "CS")
    htft_score = confidence_score(best_htft_prob, dq, "HTFT")

    response_base = {
        "match": f"{req.home_team} vs {req.away_team}",
        "expected_goals": {
            "home": lh,
            "away": la,
        },
        "data_quality": dq,
        "risk": {
            "correct_score": "Very high variance. Use only as entertainment unless there is a verified positive edge.",
            "ht_ft": "High variance. Often less stable than simpler markets such as double chance or over/under.",
        },
        "verification": verification_notes(dq),
        "responsible_gambling": "18+. Informational only. No guaranteed profit. Set limits. Do not chase losses. If gambling is causing harm, seek professional help.",
    }

    if not user.is_premium:
        return {
            **response_base,
            "plan": "free",
            "message": "Free account limited. Upgrade to premium to unlock HT/FT and alternatives.",
            "correct_score": {
                "prediction": f"{best_score[0]}-{best_score[1]}",
                "probability": round(best_score_prob, 4),
                "confidence_score": cs_score,
                "confidence": confidence_label(cs_score),
                "alternatives_locked": True,
            },
            "ht_ft": {
                "locked": True,
                "message": "Upgrade to premium to unlock HT/FT predictions.",
            },
        }

    return {
        **response_base,
        "plan": "premium",
        "correct_score": {
            "prediction": f"{best_score[0]}-{best_score[1]}",
            "probability": round(best_score_prob, 4),
            "confidence_score": cs_score,
            "confidence": confidence_label(cs_score),
            "alternatives": [
                {
                    "score": f"{h}-{a}",
                    "probability": round(p, 4),
                }
                for (h, a), p in top_scores[1:]
            ],
        },
        "ht_ft": {
            "prediction": best_htft,
            "probability": round(best_htft_prob, 4),
            "confidence_score": htft_score,
            "confidence": confidence_label(htft_score),
            "alternatives": [
                {
                    "option": option,
                    "probability": round(p, 4),
                }
                for option, p in top_htft[1:]
            ],
        },
    }