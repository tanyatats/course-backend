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
    promo_code = (req.promo or "").strip().upper()
    applied_promo = None
    if promo_code:
        promo = db.query(PromoCode).filter(PromoCode.code == promo_code).first()
        if promo is None or not promo.active:
            raise HTTPException(400, "Промокод не найден")
        if promo.limit and promo.used >= promo.limit:
            raise HTTPException(400, "Промокод больше не действует")
        amount = round(amount * (100 - promo.discount_percent) / 100)
        applied_promo = promo

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

    # засчитываем применение промокода только после успешного сохранения
    if applied_promo is not None:
        applied_promo.used += 1
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


# ---------- СЛУЖЕБНЫЙ: вручную обработать уже прошедший платёж ----------
# Нужен, если оплата прошла, а webhook в тот момент не был настроен.
# Защищён секретным словом ADMIN_KEY (переменная окружения).
import os as _os

@app.post("/api/admin/fulfill")
def admin_fulfill(payment_id: str, key: str, db: Session = Depends(get_db)):
    return _do_fulfill(payment_id, key, db)


# GET-версия того же — чтобы можно было просто открыть ссылку в браузере
@app.get("/api/admin/fulfill")
def admin_fulfill_get(payment_id: str, key: str, db: Session = Depends(get_db)):
    return _do_fulfill(payment_id, key, db)


def _do_fulfill(payment_id: str, key: str, db: Session):
    # проверка секрета
    admin_key = _os.getenv("ADMIN_KEY", "")
    if not admin_key or key != admin_key:
        raise HTTPException(403, "forbidden")
    if not _YOOKASSA_READY:
        raise HTTPException(503, "yookassa not configured")

    # находим платёж в ЮKassa и убеждаемся, что он оплачен
    try:
        verified = Payment.find_one(payment_id)
    except Exception as e:
        raise HTTPException(502, f"cannot find payment: {e}")
    if verified.status != "succeeded":
        raise HTTPException(400, f"payment status is {verified.status}, not succeeded")

    meta = verified.metadata or {}
    email = meta.get("email", "")
    product = meta.get("product", "mini")
    if not email:
        raise HTTPException(400, "no email in payment metadata")

    # находим или создаём запись
    rec = db.query(Purchase).filter(Purchase.payment_id == payment_id).first()
    if rec is None:
        rec = Purchase(
            payment_id=payment_id,
            product=product,
            email=email,
            amount=config.PRICE_MINI if product == "mini" else config.PRICE_EXTENDED,
            access_token=meta.get("access_token") or secrets.token_urlsafe(24),
            status="pending",
        )
        db.add(rec)
    rec.status = "paid"
    rec.paid_at = datetime.utcnow()
    db.commit()

    # отправляем письмо с курсом
    from .mailer import send_course_email
    try:
        send_course_email(rec.email, rec.product, rec.access_token)
        rec.email_sent = True
        db.commit()
        return {"ok": True, "sent_to": rec.email, "product": rec.product,
                "course_link": f"{config.BACKEND_BASE_URL}/course.html?t={rec.access_token}"}
    except Exception as e:
        raise HTTPException(500, f"mail send failed: {e}")


# ---------- СЛУЖЕБНЫЙ: завести/обновить промокод через браузер ----------
# Защищён тем же ADMIN_KEY. Пример:
# /api/admin/promo?key=СЕКРЕТ&code=TANYA20&discount=20&limit=50
@app.get("/api/admin/promo")
def admin_add_promo(code: str, discount: int, key: str,
                    limit: int = 0, active: bool = True,
                    db: Session = Depends(get_db)):
    admin_key = _os.getenv("ADMIN_KEY", "")
    if not admin_key or key != admin_key:
        raise HTTPException(403, "forbidden")
    code = code.strip().upper()
    if not code:
        raise HTTPException(400, "empty code")
    if not (0 <= discount <= 100):
        raise HTTPException(400, "discount must be 0..100")

    rec = db.query(PromoCode).filter(PromoCode.code == code).first()
    if rec is None:
        rec = PromoCode(code=code, discount_percent=discount,
                        limit=limit, used=0, active=active)
        db.add(rec)
    else:
        # обновляем существующий код, счётчик used не трогаем
        rec.discount_percent = discount
        rec.limit = limit
        rec.active = active
    db.commit()
    return {"ok": True, "code": rec.code, "discount_percent": rec.discount_percent,
            "limit": rec.limit, "used": rec.used, "active": rec.active}


# просмотр всех кодов
@app.get("/api/admin/promo/list")
def admin_list_promo(key: str, db: Session = Depends(get_db)):
    admin_key = _os.getenv("ADMIN_KEY", "")
    if not admin_key or key != admin_key:
        raise HTTPException(403, "forbidden")
    codes = db.query(PromoCode).all()
    return [{"code": c.code, "discount_percent": c.discount_percent,
             "limit": c.limit, "used": c.used, "active": c.active} for c in codes]
