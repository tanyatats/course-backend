"""
Модели базы данных для курса tanya.tats.
Таблицы — покупки и промокоды.
"""
from sqlalchemy import Column, String, DateTime, Boolean, Integer
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class Purchase(Base):
    """Одна покупка курса (мини или расширенный)."""
    __tablename__ = "purchases"
    # id платежа в ЮKassa — первичный ключ, чтобы один платёж = одна запись
    payment_id = Column(String, primary_key=True)
    # какой курс купили: "mini" или "extended"
    product = Column(String, nullable=False)
    # email покупателя — куда шлём материалы
    email = Column(String, nullable=False)
    # сумма в рублях (для отчётности)
    amount = Column(Integer, nullable=False)
    # уникальный токен доступа к курсу (случайная строка в ссылке)
    access_token = Column(String, unique=True, index=True, nullable=False)
    # статус: pending -> paid
    status = Column(String, default="pending", nullable=False)
    # письмо с материалами отправлено?
    email_sent = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    paid_at = Column(DateTime, nullable=True)


class PromoCode(Base):
    """Промокод со скидкой в процентах."""
    __tablename__ = "promo_codes"
    # сам код в верхнем регистре — первичный ключ
    code = Column(String, primary_key=True)
    # скидка в процентах: 20 = -20%
    discount_percent = Column(Integer, default=0, nullable=False)
    # сколько раз можно применить (0 = без лимита)
    limit = Column(Integer, default=0, nullable=False)
    # сколько уже применили
    used = Column(Integer, default=0, nullable=False)
    # можно быстро выключить, не удаляя
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
