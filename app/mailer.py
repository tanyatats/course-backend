"""
Отправка письма ученику после оплаты.
Письмо содержит: ссылку на курс (с токеном) и PDF во вложении.
Работает с любым SMTP (Gmail, почта Timeweb на домене и т.п.).
"""
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from . import config

# Пути к PDF-файлам курса (лежат рядом, в assets/)
ASSETS = Path(__file__).resolve().parent.parent / "assets"
PDF_MINI = ASSETS / "handpoke-mini-course.pdf"
PDF_EXTENDED = ASSETS / "handpoke-mini-course.pdf"  # для расширенного пока тот же материал; замени при необходимости


def _course_link(access_token: str) -> str:
    """Ссылка на страницу курса с токеном доступа."""
    return f"{config.BACKEND_BASE_URL}/course.html?t={access_token}"


def build_email(to_email: str, product: str, access_token: str) -> EmailMessage:
    """Собирает письмо в зависимости от купленного продукта."""
    msg = EmailMessage()
    from_name = config.SMTP_FROM_NAME
    msg["From"] = f"{from_name} <{config.SMTP_FROM}>"
    msg["To"] = to_email

    link = _course_link(access_token)

    if product == "extended":
        msg["Subject"] = "Доступ к расширенному курсу handpoke — tanya.tats"
        body = (
            "Спасибо за покупку расширенного курса handpoke!\n\n"
            "Я свяжусь с тобой лично, чтобы согласовать даты обучения "
            "и работу с моделями.\n\n"
            f"Пока изучай материалы курса здесь:\n{link}\n\n"
            "PDF с базой прикреплён к письму.\n\n"
            "До связи!\n— tanya.tats"
        )
        pdf_path = PDF_EXTENDED
    else:  # mini
        msg["Subject"] = "Доступ к мини-курсу handpoke — tanya.tats"
        body = (
            "Спасибо за покупку мини-курса handpoke!\n\n"
            f"Твой доступ к курсу:\n{link}\n\n"
            "PDF с курсом прикреплён к этому письму — можешь скачать и "
            "изучать в удобном темпе.\n\n"
            "Учись, пробуй, и не спеши с первой работой.\n— tanya.tats"
        )
        pdf_path = PDF_MINI

    msg.set_content(body)

    # Прикладываем PDF, если файл на месте
    if pdf_path.exists():
        data = pdf_path.read_bytes()
        msg.add_attachment(
            data,
            maintype="application",
            subtype="pdf",
            filename="handpoke-course.pdf",
        )
    return msg


def send_course_email(to_email: str, product: str, access_token: str) -> None:
    """
    Отправляет письмо через SMTP.
    Бросает исключение при ошибке — вызывающий код решает, что делать.
    """
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD):
        raise RuntimeError("SMTP не настроен: заполни SMTP_* переменные окружения")

    msg = build_email(to_email, product, access_token)

    context = ssl.create_default_context()
    # Порт 465 -> SSL, порт 587 -> STARTTLS
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=context) as s:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
            s.starttls(context=context)
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)
