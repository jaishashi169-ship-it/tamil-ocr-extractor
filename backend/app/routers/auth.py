import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from app.auth import create_access_token, get_current_user, hash_password, verify_password
from app.config import get_settings
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/auth", tags=["Authentication"])
settings = get_settings()


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: str
    email: str
    role: str


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email.lower()).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Email is already registered")
    user = User(
        email=payload.email.lower(),
        hashed_password=hash_password(payload.password),
        role="user",
        storage_quota_bytes=settings.USER_STORAGE_QUOTA_BYTES,
        retention_days=settings.DEFAULT_RETENTION_DAYS,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id, user.role)
    return TokenResponse(access_token=token, user_id=user.id, email=user.email, role=user.role)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email.lower()).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")
    token = create_access_token(user.id, user.role)
    return TokenResponse(access_token=token, user_id=user.id, email=user.email, role=user.role)


@router.post("/guest", response_model=TokenResponse)
def create_guest_session(db: Session = Depends(get_db)):
    """Create an isolated temporary user session so beginners can use the studio immediately."""
    guest_email = f"guest-{uuid.uuid4().hex[:12]}@studio.local"
    user = User(
        email=guest_email,
        hashed_password=hash_password(uuid.uuid4().hex),
        role="user",
        storage_quota_bytes=settings.USER_STORAGE_QUOTA_BYTES,
        retention_days=settings.DEFAULT_RETENTION_DAYS,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id, user.role)
    return TokenResponse(access_token=token, user_id=user.id, email=user.email, role=user.role)


@router.get("/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "user_id": current_user.id,
        "email": current_user.email,
        "role": current_user.role,
        "storage_quota_bytes": current_user.storage_quota_bytes,
        "retention_days": current_user.retention_days,
    }
