"""
Отправка письма ученику после оплаты — через SMTP.
С таймаутом и перебором портов (465 SSL / 587 STARTTLS).
Письмо содержит ссылку на курс (с токеном) и PDF во вложении.
"""
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from . import config

ASSETS = Path(__file__).resolve().parent.parent / "assets"
PDF_MINI = ASSETS / "handpoke-mini-course.pdf"
PDF_EXTENDED = ASSETS / "handpoke-mini-course.pdf"


def _course_link(access_token: str) -> str:
    return f"{config.BACKEND_BASE_URL}/course.html?t={access_token}"


def build_email(to_email: str, product: str, access_token: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{config.SMTP_FROM_NAME} <{config.SMTP_FROM}>"
    msg["To"] = to_email
    link = _course_link(access_token)

    if product == "extended":
        msg["Subject"] = "Доступ к расширенному курсу handpoke — tanya.tats"
        body = (
            "Спасибо за покупку расширенного курса handpoke!\n\n"
            "Я свяжусь с тобой лично, чтобы согласовать даты обучения и работу с моделями.\n\n"
            f"Пока изучай материалы курса здесь:\n{link}\n\n"
            "PDF с курсом прикреплён к письму.\n\n— tanya.tats"
        )
        pdf_path = PDF_EXTENDED
    else:
        msg["Subject"] = "Доступ к мини-курсу handpoke — tanya.tats"
        body = (
            "Спасибо за покупку мини-курса handpoke!\n\n"
            f"Твой доступ к курсу:\n{link}\n\n"
            "PDF с курсом прикреплён к этому письму.\n\n"
            "Учись, пробуй, и не спеши с первой работой.\n— tanya.tats"
        )
        pdf_path = PDF_MINI

    msg.set_content(body)
    if pdf_path.exists():
        msg.add_attachment(pdf_path.read_bytes(), maintype="application",
                           subtype="pdf", filename="handpoke-course.pdf")
    return msg


def send_course_email(to_email: str, product: str, access_token: str) -> None:
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD):
        raise RuntimeError("SMTP не настроен: заполни SMTP_* переменные")

    msg = build_email(to_email, product, access_token)
    context = ssl.create_default_context()
    TIMEOUT = 15

    def _via_ssl(port):
        with smtplib.SMTP_SSL(config.SMTP_HOST, port, context=context, timeout=TIMEOUT) as s:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)

    def _via_starttls(port):
        with smtplib.SMTP(config.SMTP_HOST, port, timeout=TIMEOUT) as s:
            s.ehlo(); s.starttls(context=context); s.ehlo()
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)

    if config.SMTP_PORT == 465:
        attempts = [("ssl", 465), ("starttls", 587)]
    elif config.SMTP_PORT == 587:
        attempts = [("starttls", 587), ("ssl", 465)]
    else:
        attempts = [("starttls", config.SMTP_PORT), ("ssl", 465), ("starttls", 587)]

    errors = []
    for mode, port in attempts:
        try:
            _via_ssl(port) if mode == "ssl" else _via_starttls(port)
            return
        except Exception as e:
            errors.append(f"{mode}:{port} -> {type(e).__name__}: {e}")
    raise RuntimeError("SMTP failed. " + " | ".join(errors))
