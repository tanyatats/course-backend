"""
Бэкенд курса tanya.tats.

Поток:
1. Пользователь на сайте вводит email и жмёт "Купить" -> POST /api/pay
   -> создаём платёж в ЮKassa, возвращаем ссылку на оплату.
2. Пользователь платит на стороне ЮKassa.
3. ЮKassa шлёт webhook -> POST /api/webhooks/yookassa
   -> проверяем, что платёж реально succeeded (запрашиваем ЮKassa напрямую),
   -> отправляем письмо с PDF и ссылкой на курс.
4. Пользователь открывает course.html?t=<token> -> фронт спрашивает
   GET /api/access/<token> -> отвечаем, валиден ли доступ.
"""
import secrets
import ipaddress
from datetime import datetime

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session

from . import config
from .models import Base, Purchase, PromoCode

# --- ЮKassa SDK ---
try:
    from yookassa import Configuration, Payment
    if config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY:
        Configuration.account_id = config.YOOKASSA_SHOP_ID
        Configuration.secret_key = config.YOOKASSA_SECRET_KEY
    _YOOKASSA_READY = bool(config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY)
except Exception:
    _YOOKASSA_READY = False

# --- БД ---
import logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("course")

connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

# Создание таблиц НЕ должно ронять старт приложения:
# если БД на миг недоступна, приложение всё равно поднимется,
# а таблицы создадутся при первом успешном обращении.
_DB_READY = False
def init_db():
    global _DB_READY
    try:
        Base.metadata.create_all(bind=engine)
        _DB_READY = True
        log.info("DB tables ready")
    except Exception as e:
        log.error(f"DB init failed (will retry on first request): {e}")

init_db()


def get_db():
    if not _DB_READY:
        init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


app = FastAPI(title="tanya.tats course backend", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Официальные IP-сети, с которых ЮKassa шлёт уведомления.
# Проверяем источник webhook, чтобы никто не подделал "оплату".
YOOKASSA_NETWORKS = [
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "77.75.154.128/25",
    "2a02:5180::/32",
]
_YK_NETS = [ipaddress.ip_network(n) for n in YOOKASSA_NETWORKS]


def _ip_is_yookassa(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _YK_NETS)


# ---------- схемы запросов ----------
class PayRequest(BaseModel):
    product: str          # "mini" | "extended"
    email: EmailStr
    promo: str | None = None


# ---------- служебные ----------
@app.get("/api/health")
def health():
    # проверяем живость БД прямо сейчас
    db_ok = False
    try:
        from sqlalchemy import text
        with engine.connect() as c:
            c.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        log.error(f"DB health check failed: {e}")
    return {"status": "ok", "yookassa": _YOOKASSA_READY, "db": db_ok,
            "mail": bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)}


# ---------- проверка промокода (для фронта, до оплаты) ----------
class PromoCheckRequest(BaseModel):
    product: str
    promo: str


@app.post("/api/promo/check")
def check_promo(req: PromoCheckRequest, db: Session = Depends(get_db)):
    """Проверяет код и возвращает пересчитанную цену — чтобы фронт показал скидку."""
    if req.product not in ("mini", "extended"):
        raise HTTPException(400, "unknown product")
    base = config.PRICE_MINI if req.product == "mini" else config.PRICE_EXTENDED
    code = (req.promo or "").strip().upper()
    if not code:
        raise HTTPException(400, "пустой промокод")
    promo = db.query(PromoCode).filter(PromoCode.code == code).first()
    if promo is None or not promo.active:
        raise HTTPException(400, "Промокод не найден")
    if promo.limit and promo.used >= promo.limit:
        raise HTTPException(400, "Промокод больше не действует")
    new_price = round(base * (100 - promo.discount_percent) / 100)
    return {"ok": True, "base_price": base, "new_price": new_price,
            "discount_percent": promo.discount_percent}


# ---------- создание платежа ----------
@app.post("/api/pay")
def create_payment(req: PayRequest, db: Session = Depends(get_db)):
    if req.product not in ("mini", "extended"):
        raise HTTPException(400, "unknown product")
    if not _YOOKASSA_READY:
        raise HTTPException(503, "оплата временно недоступна: ЮKassa не настроена")

    amount = config.PRICE_MINI if req.product == "mini" else config.PRICE_EXTENDED
    title = "Мини-курс handpoke" if req.product == "mini" else "Расширенный курс handpoke"

    # --- применяем промокод, если введён ---
    promo_code = (req.promo
