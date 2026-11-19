import uuid

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, EmailStr, Field

from travelrag.api.deps import CurrentUserDep, SessionDep
from travelrag.core.config import get_settings
from travelrag.core.security import create_access_token
from travelrag.core.users import EmailTakenError, authenticate, create_user

router = APIRouter(prefix="/auth", tags=["auth"])


class Credentials(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: Credentials, session: SessionDep) -> UserOut:
    try:
        user = await create_user(session, body.email, body.password)
    except EmailTakenError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered") from exc
    return UserOut(id=user.id, email=user.email)


@router.post("/login")
async def login(body: Credentials, session: SessionDep) -> TokenOut:
    user = await authenticate(session, body.email, body.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    settings = get_settings()
    token = create_access_token(
        str(user.id),
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_minutes=settings.jwt_expire_minutes,
    )
    return TokenOut(access_token=token)


@router.get("/me")
async def me(user: CurrentUserDep) -> UserOut:
    return UserOut(id=user.id, email=user.email)
