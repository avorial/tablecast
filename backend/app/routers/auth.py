from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from .. import config, models, security
from ..deps import DbDep, get_current_user, templates

router = APIRouter()


def _login_redirect(user_id: int) -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        config.SESSION_COOKIE,
        security.create_session_token(user_id),
        max_age=config.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/login")
def login_page(request: Request, user: Annotated[models.User | None, Depends(get_current_user)]):
    if user:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"registration_open": config.REGISTRATION_OPEN})


@router.post("/login")
def login(request: Request, db: DbDep, email: Annotated[str, Form()], password: Annotated[str, Form()]):
    user = db.query(models.User).filter_by(email=email.strip().lower()).first()
    if user is None or not security.verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "Invalid email or password", "registration_open": config.REGISTRATION_OPEN},
            status_code=401,
        )
    return _login_redirect(user.id)


@router.get("/register")
def register_page(request: Request):
    if not config.REGISTRATION_OPEN:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(request, "register.html", {})


@router.post("/register")
def register(
    request: Request,
    db: DbDep,
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    if not config.REGISTRATION_OPEN:
        return RedirectResponse("/login", status_code=303)
    email = email.strip().lower()
    name = name.strip()[:80]
    if not name or not email or len(password) < 8:
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "Name, email, and a password of at least 8 characters are required"},
            status_code=400,
        )
    if db.query(models.User).filter_by(email=email).first():
        return templates.TemplateResponse(
            request, "register.html", {"error": "That email is already registered"},
            status_code=400,
        )
    user = models.User(email=email, name=name, password_hash=security.hash_password(password))
    db.add(user)
    db.commit()
    return _login_redirect(user.id)


@router.post("/logout")
def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(config.SESSION_COOKIE)
    return response
