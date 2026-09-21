"""Authentication, RBAC and tenant isolation.

Every protected route resolves a Principal. Anything that touches hospital
data goes through `require_hospital_access`, which is the single place where
"Hospital A must never read Hospital B" is enforced.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.hash import pbkdf2_sha256
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

bearer = HTTPBearer(auto_error=False)

PLATFORM_ADMIN = "PLATFORM_ADMIN"
HOSPITAL_ADMIN = "HOSPITAL_ADMIN"
DOCTOR = "DOCTOR"
PATIENT = "PATIENT"


@dataclass
class Principal:
    user_id: str
    role: str
    hospital_id: str | None = None
    doctor_id: str | None = None
    patient_id: str | None = None


def hash_password(raw: str) -> str:
    return pbkdf2_sha256.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    return pbkdf2_sha256.verify(raw, hashed)


def make_token(user: User) -> str:
    payload = {
        "sub": user.id,
        "role": user.role,
        "hospital_id": user.hospital_id,
        "doctor_id": user.doctor_id,
        "patient_id": user.patient_id,
        "exp": datetime.utcnow() + timedelta(hours=12),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGO)


def current_principal(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Principal:

    # 1. Check whether Authorization header was received
    if creds is None:
        print("AUTH ERROR: No bearer token received")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
        )

    print("AUTH TOKEN RECEIVED:", creds.credentials[:20] + "...")

    # 2. Decode JWT
    try:
        data = jwt.decode(
            creds.credentials,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGO],
        )

        print("JWT DATA:", data)

    except jwt.ExpiredSignatureError:
        print("AUTH ERROR: Token expired")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )

    except jwt.InvalidTokenError as e:
        print("AUTH ERROR: Invalid token:", str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    # 3. Check required fields
    if "sub" not in data or "role" not in data:
        print("AUTH ERROR: Token missing sub or role")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )

    # 4. Create Principal
    return Principal(
        user_id=data["sub"],
        role=data["role"],
        hospital_id=data.get("hospital_id"),
        doctor_id=data.get("doctor_id"),
        patient_id=data.get("patient_id"),
    )


def require_roles(*roles: str):
    def dep(p: Principal = Depends(current_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Role not permitted")
        return p
    return dep


def require_hospital_access(p: Principal, hospital_id: str) -> None:
    """Tenant boundary. Platform admin crosses tenants; nobody else does."""
    if p.role == PLATFORM_ADMIN:
        return
    if p.hospital_id != hospital_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-tenant access denied")
