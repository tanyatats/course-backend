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
from .models import Base, Purchase

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
    return {"status": "ok", "yookassa": _YOOKASSA_READY, "db": db_ok}


# ---------- создание платежа ----------
@app.post("/api/pay")
def create_payment(req: PayRequest, db: Session = Depends(get_db)):
    if req.product not in ("mini", "extended"):
        raise HTTPException(400, "unknown product")
    if not _YOOKASSA_READY:
        raise HTTPException(503, "оплата временно недоступна: ЮKassa не настроена")

    amount = config.PRICE_MINI if req.product == "mini" else config.PRICE_EXTENDED
    title = "Мини-курс handpoke" if req.product == "mini" else "Расширенный курс handpoke"

    access_token = secrets.token_urlsafe(24)

    payment = Payment.create({
        "amount": {"value": f"{amount}.00", "currency": "RUB"},
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": config.FRONTEND_RETURN_URL,
        },
        "description": title,
        "metadata": {
            "product": req.product,
            "email": req.email,
            "access_token": access_token,
        },
        # чек для самозанятого/54-ФЗ: email покупателя
        "receipt": {
            "customer": {"email": req.email},
            "items": [{
                "description": title,
                "quantity": "1.00",
                "amount": {"value": f"{amount}.00", "currency": "RUB"},
                "vat_code": 1,
                "payment_mode": "full_payment",
                "payment_subject": "service",
            }],
        },
    }, secrets.token_hex(16))  # idempotence key

    # сохраняем как pending
    rec = Purchase(
        payment_id=payment.id,
        product=req.product,
        email=req.email,
        amount=amount,
        access_token=access_token,
        status="pending",
    )
    db.merge(rec)
    db.commit()

    confirmation_url = payment.confirmation.confirmation_url
    return {"confirmation_url": confirmation_url, "payment_id": payment.id}


# ---------- webhook от ЮKassa ----------
@app.post("/api/webhooks/yookassa")
async def yookassa_webhook(request: Request, db: Session = Depends(get_db)):
    # 1) проверяем источник по IP
    client_ip = request.headers.get("x-forwarded-for", request.client.host).split(",")[0].strip()
    if not _ip_is_yookassa(client_ip):
        raise HTTPException(403, "forbidden")

    body = await request.json()
    event = body.get("event")
    obj = body.get("object", {})
    payment_id = obj.get("id")

    if event != "payment.succeeded" or not payment_id:
        # игнорируем прочие события, но отвечаем 200, чтобы ЮKassa не долбила повторами
        return {"ok": True}

    # 2) НЕ доверяем телу вслепую — перепроверяем платёж напрямую в ЮKassa
    if _YOOKASSA_READY:
        try:
            verified = Payment.find_one(payment_id)
            if verified.status != "succeeded":
                return {"ok": True}
            meta = verified.metadata or {}
        except Exception:
            raise HTTPException(502, "cannot verify payment")
    else:
        meta = obj.get("metadata", {})

    # 3) находим запись
    rec = db.query(Purchase).filter(Purchase.payment_id == payment_id).first()
    if rec is None:
        # платёж есть в ЮKassa, но не у нас — восстановим из метаданных
        rec = Purchase(
            payment_id=payment_id,
            product=meta.get("product", "mini"),
            email=meta.get("email", ""),
            amount=config.PRICE_MINI if meta.get("product") == "mini" else config.PRICE_EXTENDED,
            access_token=meta.get("access_token") or secrets.token_urlsafe(24),
            status="pending",
        )
        db.add(rec)

    # защита от повторной обработки (ЮKassa может слать webhook несколько раз)
    if rec.status == "paid" and rec.email_sent:
        return {"ok": True}

    rec.status = "paid"
    rec.paid_at = datetime.utcnow()
    db.commit()

    # 4) отправляем письмо с курсом
    from .mailer import send_course_email
    try:
        if rec.email and not rec.email_sent:
            send_course_email(rec.email, rec.product, rec.access_token)
            rec.email_sent = True
            db.commit()
    except Exception as e:
        # не роняем webhook: письмо можно дослать вручную из /admin (или повторным webhook)
        # логируем в stdout — видно в логах приложения
        print(f"[MAIL ERROR] payment={payment_id} email={rec.email}: {e}")

    return {"ok": True}


# ---------- проверка доступа к курсу ----------
@app.get("/api/access/{token}")
def check_access(token: str, db: Session = Depends(get_db)):
    rec = db.query(Purchase).filter(Purchase.access_token == token).first()
    if rec is None or rec.status != "paid":
        raise HTTPException(404, "no access")
    return {"access": True, "product": rec.product}
