"""
Конфигурация из переменных окружения.
Всё чувствительное (ключи, пароли) — только через env, никогда в коде.
"""
import os

# --- База данных ---
# На Timeweb App Platform файловая система эфемерная -> для прода нужен PostgreSQL.
# Локально по умолчанию SQLite, чтобы можно было запустить без БД.
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./course.db")

# --- ЮKassa (отдельный магазин курса) ---
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")

# --- Цены курсов (в рублях) ---
PRICE_MINI = int(os.getenv("PRICE_MINI", "10000"))
PRICE_EXTENDED = int(os.getenv("PRICE_EXTENDED", "50000"))

# --- Адреса ---
# Куда ЮKassa вернёт пользователя после оплаты (страница "спасибо")
FRONTEND_RETURN_URL = os.getenv("FRONTEND_RETURN_URL", "https://tanyatats.ru/thanks.html")
# Базовый адрес самого бэкенда (для ссылки на курс в письме)
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "https://tanyatats.ru")

# --- SMTP для отправки писем ученикам ---
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "tanya.tats")

# --- Unisender Go (отправка писем по HTTPS API, обход блокировки SMTP) ---
UNISENDER_API_KEY = os.getenv("UNISENDER_API_KEY", "")
# Адрес отправителя (должен быть на подтверждённом в Unisender домене)
MAIL_FROM_EMAIL = os.getenv("MAIL_FROM_EMAIL", "info@tanyatats.ru")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "tanya.tats")

# --- Прочее ---
# Разрешённые источники (CORS) — твой сайт
CORS_ORIGINS = os.getenv("CORS_ORIGINS", "https://tanyatats.ru,https://www.tanyatats.ru").split(",")
