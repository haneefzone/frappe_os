from datetime import datetime

from pydantic import BaseModel, EmailStr

from app.models import User


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    role: str
    permissions: list[str]
    last_login: datetime | None

    @classmethod
    def from_user(cls, user: User) -> "UserOut":
        return cls(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role.name,
            permissions=list(user.role.permissions or []),
            last_login=user.last_login,
        )
