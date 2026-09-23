"""
Auth Service — pure domain logic.

Boundary Contract:
- ZERO imports from fastapi or starlette
- ZERO imports from schemas.py, exceptions.py, models.py, repository.py
- Imports ONLY from interfaces.py and app.shared
- Raises ONLY domain exceptions
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

import jwt
from passlib.context import CryptContext

from app.auth.interfaces import (
    AuthResult,
    IUserRepository,
    InvalidCredentialsError,
    InvalidTokenError,
    UserAlreadyExistsError,
    UserDTO,
    UserNotFoundError,
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthService:
    def __init__(
        self,
        user_repo: IUserRepository,
        jwt_secret: str,
        jwt_algorithm: str = "HS256",
        token_expire_minutes: int = 60 * 24 * 365 * 10,  # 10 years
    ) -> None:
        self._repo = user_repo
        self._secret = jwt_secret
        self._algo = jwt_algorithm
        self._expire_minutes = token_expire_minutes

    def _create_token(self, user_id: UUID) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "sub": str(user_id),
            "iat": now,
            "exp": now + timedelta(minutes=self._expire_minutes),
        }
        return jwt.encode(payload, self._secret, algorithm=self._algo)

    def decode_token(self, token: str) -> UUID:
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._algo])
            user_id_str = payload.get("sub")
            if not user_id_str:
                raise InvalidTokenError("Token missing subject")
            return UUID(user_id_str)
        except (jwt.PyJWTError, ValueError) as exc:
            raise InvalidTokenError("Invalid token") from exc

    async def register(self, email: str, password: str) -> AuthResult:
        normalized_email = email.lower().strip()
        existing = await self._repo.get_by_email(normalized_email)
        if existing is not None:
            raise UserAlreadyExistsError(f"User with email '{email}' already exists")

        pw_hash = pwd_context.hash(password)
        user = await self._repo.create(
            email=normalized_email, password_hash=pw_hash
        )
        token = self._create_token(user.id)
        return AuthResult(token=token, user=user)

    async def login(self, email: str, password: str) -> AuthResult:
        normalized_email = email.lower().strip()
        record = await self._repo.get_by_email(normalized_email)
        if record is None:
            raise InvalidCredentialsError("Invalid email or password")

        user, pw_hash = record
        if not pwd_context.verify(password, pw_hash):
            raise InvalidCredentialsError("Invalid email or password")

        token = self._create_token(user.id)
        return AuthResult(token=token, user=user)

    async def get_user_by_id(self, user_id: UUID) -> UserDTO:
        user = await self._repo.get_by_id(user_id)
        if user is None:
            raise UserNotFoundError("User not found")
        return user