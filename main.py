import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import Base, Click, Link, Offer, User, engine, SessionLocal

app = FastAPI(title="Telegram Traffic CRM", version="1.0.0")
templates = Jinja2Templates(directory="templates")
Base.metadata.create_all(bind=engine)

APP_SECRET = os.getenv("APP_SECRET", "CHANGE_ME_TO_A_LONG_RANDOM_SECRET")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me")
TELEGRAM_MAX_AGE = int(os.getenv("TELEGRAM_MAX_AGE", "86400"))
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", "604800"))

if APP_SECRET == "CHANGE_ME_TO_A_LONG_RANDOM_SECRET":
    print("WARNING: set APP_SECRET in production.")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def sign(value: str) -> str:
    return hmac.new(APP_SECRET.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_session(user_id: int) -> str:
    payload = f"{user_id}.{int(time.time())}"
    return f"{payload}.{sign(payload)}"


def get_session_user(request: Request, db: Session) -> Optional[User]:
    token = request.cookies.get("crm_session")
    if not token:
        return None
    try:
        user_id, issued, signature = token.split(".", 2)
        payload = f"{user_id}.{issued}"
        if not hmac.compare_digest(signature, sign(payload)):
            return None
        if int(time.time()) - int(issued) > SESSION_MAX_AGE:
            return None
        return db.query(User).filter(User.id == int(user_id), User.is_active.is_(True)).first()
    except (ValueError, TypeError):
        return None


def password_hash(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 180_000
    ).hex()
    return f"{salt}${digest}"


def password_ok(password: str, stored: str) -> bool:
    try:
        salt, digest = stored.split("$", 1)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt.encode(), 180_000
        ).hex()
        return hmac.compare_digest(candidate, digest)
    except ValueError:
        return False


def telegram_check_string(init_data: str) -> tuple[bool, dict]:
    """
    Validates Telegram Mini App initData using the bot token.
    Telegram docs:
    secret_key = HMAC-SHA256("WebAppData", bot_token)
    hash = HMAC-SHA256(secret_key, data_check_string)
    """
    if not BOT_TOKEN:
        return False, {}

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not received_hash:
        return False, {}

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pairs.items())
    )
    secret_key = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    calculated = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(calculated, received_hash):
        return False, {}

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError:
        return False, {}

    if auth_date <= 0 or time.time() - auth_date > TELEGRAM_MAX_AGE:
        return False, {}

    user = {}
    if pairs.get("user"):
        try:
            user = json.loads(pairs["user"])
        except json.JSONDecodeError:
            return False, {}

    return True, {"params": pairs, "user": user}


def unique_slug(db: Session, base: str) -> str:
    base = "".join(c.lower() if c.isalnum() else "-" for c in base).strip("-")
    base = base[:35] or "link"
    slug = base
    counter = 2
    while db.query(Link).filter(Link.slug == slug).first():
        slug = f"{base}-{counter}"
        counter += 1
    return slug


def require_user(request: Request, db: Session) -> User:
    user = get_session_user(request, db)
    if not user:
        raise HTTPException(401, "Не авторизовано")
    return user


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    return RedirectResponse(
        "/dashboard" if get_session_user(request, db) else "/login",
        status_code=303,
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/api/login")
async def password_login(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))

    if username != ADMIN_USERNAME or password != ADMIN_PASSWORD:
        raise HTTPException(401, "Невірний логін або пароль")

    user = db.query(User).filter(User.username == username).first()
    if not user:
        user = User(
            username=username,
            telegram_id=None,
            password_hash=password_hash(password),
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    response = JSONResponse({"ok": True})
    response.set_cookie(
        "crm_session",
        make_session(user.id),
        httponly=True,
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return response


@app.post("/api/telegram-auth")
async def telegram_auth(request: Request, db: Session = Depends(get_db)):
    data = await request.json()
    init_data = str(data.get("initData", ""))

    if not init_data:
        raise HTTPException(400, "Telegram initData відсутній")

    valid, result = telegram_check_string(init_data)
    if not valid:
        raise HTTPException(401, "Недійсний Telegram initData")

    tg_user = result["user"]
    telegram_id = tg_user.get("id")
    if not telegram_id:
        raise HTTPException(400, "Telegram user id відсутній")

    username = (
        tg_user.get("username")
        or f"tg_{telegram_id}"
    )

    user = db.query(User).filter(User.telegram_id == int(telegram_id)).first()
    if not user:
        user = User(
            username=username[:100],
            telegram_id=int(telegram_id),
            password_hash=None,
            first_name=tg_user.get("first_name"),
            last_name=tg_user.get("last_name"),
            is_active=True,
        )
        db.add(user)
    else:
        user.username = username[:100]
        user.first_name = tg_user.get("first_name")
        user.last_name = tg_user.get("last_name")

    db.commit()
    db.refresh(user)

    response = JSONResponse({"ok": True, "username": user.username})
    response.set_cookie(
        "crm_session",
        make_session(user.id),
        httponly=True,
        secure=os.getenv("COOKIE_SECURE", "false").lower() == "true",
        samesite="lax",
        max_age=SESSION_MAX_AGE,
    )
    return response


@app.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie("crm_session")
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = get_session_user(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "dashboard.html",
        {"request": request, "user": user},
    )


@app.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    return {
        "id": user.id,
        "username": user.username,
        "telegram_id": user.telegram_id,
        "first_name": user.first_name,
    }


@app.get("/api/offers")
def list_offers(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    offers = db.query(Offer).filter(Offer.owner_id == user.id).order_by(Offer.id.desc()).all()
    return [
        {
            "id": x.id,
            "name": x.name,
            "geo": x.geo,
            "payout": x.payout,
            "currency": x.currency,
            "created_at": x.created_at.isoformat(),
        }
        for x in offers
    ]


@app.post("/api/offers")
async def create_offer(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    data = await request.json()

    name = str(data.get("name", "")).strip()
    geo = str(data.get("geo", "")).strip()
    payout = str(data.get("payout", "")).strip()
    currency = str(data.get("currency", "USD")).strip().upper()[:10]

    if not name:
        raise HTTPException(400, "Назва офера обов'язкова")

    offer = Offer(
        name=name[:200],
        geo=geo[:100],
        payout=payout[:50],
        currency=currency or "USD",
        owner_id=user.id,
    )
    db.add(offer)
    db.commit()
    db.refresh(offer)
    return {"ok": True, "id": offer.id}


@app.delete("/api/offers/{offer_id}")
def delete_offer(offer_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    offer = db.query(Offer).filter(
        Offer.id == offer_id, Offer.owner_id == user.id
    ).first()
    if not offer:
        raise HTTPException(404, "Офер не знайдено")
    db.delete(offer)
    db.commit()
    return {"ok": True}


@app.get("/api/links")
def list_links(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    links = db.query(Link).filter(Link.owner_id == user.id).order_by(Link.id.desc()).all()

    result = []
    for link in links:
        clicks = db.query(func.count(Click.id)).filter(Click.link_id == link.id).scalar()
        result.append({
            "id": link.id,
            "name": link.name,
            "slug": link.slug,
            "destination_url": link.destination_url,
            "source": link.source,
            "offer_id": link.offer_id,
            "clicks": clicks,
            "created_at": link.created_at.isoformat(),
        })
    return result


@app.post("/api/links")
async def create_link(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    data = await request.json()

    name = str(data.get("name", "")).strip()
    destination_url = str(data.get("destination_url", "")).strip()
    source = str(data.get("source", "telegram")).strip()
    offer_id = data.get("offer_id")

    if not name or not destination_url:
        raise HTTPException(400, "Назва та URL обов'язкові")

    parsed = urlparse(destination_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "URL має починатися з http:// або https://")

    offer = None
    if offer_id:
        offer = db.query(Offer).filter(
            Offer.id == int(offer_id), Offer.owner_id == user.id
        ).first()
        if not offer:
            raise HTTPException(404, "Офер не знайдено")

    slug = unique_slug(db, name)
    link = Link(
        name=name[:200],
        slug=slug,
        destination_url=destination_url,
        source=source[:100],
        offer_id=offer.id if offer else None,
        owner_id=user.id,
    )
    db.add(link)
    db.commit()
    db.refresh(link)

    return {"ok": True, "id": link.id, "slug": link.slug}


@app.delete("/api/links/{link_id}")
def delete_link(link_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    link = db.query(Link).filter(
        Link.id == link_id, Link.owner_id == user.id
    ).first()
    if not link:
        raise HTTPException(404, "Лінк не знайдено")
    db.delete(link)
    db.commit()
    return {"ok": True}


@app.get("/api/stats")
def stats(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    link_ids = [
        row.id for row in db.query(Link.id).filter(Link.owner_id == user.id).all()
    ]

    if not link_ids:
        return {
            "total_clicks": 0,
            "today_clicks": 0,
            "unique_ips": 0,
            "daily": [],
        }

    total = db.query(func.count(Click.id)).filter(Click.link_id.in_(link_ids)).scalar()
    today = datetime.now(timezone.utc).date()
    today_count = db.query(func.count(Click.id)).filter(
        Click.link_id.in_(link_ids),
        func.date(Click.created_at) == today.isoformat(),
    ).scalar()
    unique_ips = db.query(func.count(func.distinct(Click.ip))).filter(
        Click.link_id.in_(link_ids)
    ).scalar()

    rows = db.query(
        func.date(Click.created_at).label("day"),
        func.count(Click.id).label("count"),
    ).filter(
        Click.link_id.in_(link_ids)
    ).group_by(
        func.date(Click.created_at)
    ).order_by(
        func.date(Click.created_at).desc()
    ).limit(14).all()

    return {
        "total_clicks": total or 0,
        "today_clicks": today_count or 0,
        "unique_ips": unique_ips or 0,
        "daily": [{"day": str(day), "count": count} for day, count in reversed(rows)],
    }


@app.get("/t/{slug}")
def track(slug: str, request: Request, db: Session = Depends(get_db)):
    link = db.query(Link).filter(Link.slug == slug, Link.is_active.is_(True)).first()
    if not link:
        raise HTTPException(404, "Tracking link not found")

    query = dict(request.query_params)
    utm_source = query.get("utm_source")
    utm_medium = query.get("utm_medium")
    utm_campaign = query.get("utm_campaign")
    tg_start_param = query.get("startapp") or query.get("tgWebAppStartParam")

    click = Click(
        link_id=link.id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        referrer=request.headers.get("referer"),
        utm_source=utm_source,
        utm_medium=utm_medium,
        utm_campaign=utm_campaign,
        tg_start_param=tg_start_param,
    )
    db.add(click)
    db.commit()

    # Preserve arbitrary query parameters when redirecting.
    destination = link.destination_url
    if query:
        parsed = urlparse(destination)
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
        existing.update(query)
        destination = urlunparse(
            parsed._replace(query=urlencode(existing, doseq=True))
        )

    return RedirectResponse(destination, status_code=307)


@app.get("/health")
def health():
    return {"status": "ok"}
