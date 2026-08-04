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
    Пробует несколько портов (сначала заданный, потом запасные),
    т.к. на некоторых платформах часть SMTP-портов заблокирована.
    Бросает исключение, если ни один способ не сработал.
    """
    if not (config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD):
        raise RuntimeError("SMTP не настроен: заполни SMTP_* переменные окружения")

    msg = build_email(to_email, product, access_token)
    context = ssl.create_default_context()
    TIMEOUT = 15

    def _via_ssl(port):
        with smtplib.SMTP_SSL(config.SMTP_HOST, port, context=context, timeout=TIMEOUT) as s:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)

    def _via_starttls(port):
        with smtplib.SMTP(config.SMTP_HOST, port, timeout=TIMEOUT) as s:
            s.ehlo()
            s.starttls(context=context)
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(msg)

    # порядок попыток: сначала то, что указано в SMTP_PORT, затем запасные
    attempts = []
    if config.SMTP_PORT == 465:
        attempts = [("ssl", 465), ("starttls", 587), ("starttls", 2525), ("starttls", 25)]
    elif config.SMTP_PORT == 587:
        attempts = [("starttls", 587), ("ssl", 465), ("starttls", 2525), ("starttls", 25)]
    else:
        attempts = [("starttls", config.SMTP_PORT), ("ssl", 465), ("starttls", 587), ("starttls", 2525)]

    errors = []
    for mode, port in attempts:
        try:
            if mode == "ssl":
                _via_ssl(port)
            else:
                _via_starttls(port)
            return  # успех
        except Exception as e:
            errors.append(f"{mode}:{port} -> {type(e).__name__}: {e}")
            continue

    # ни один способ не сработал — бросаем сводную ошибку
    raise RuntimeError("SMTP failed on all ports. " + " | ".join(errors))
