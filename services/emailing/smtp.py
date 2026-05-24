"""
Email sender using Django's built-in SMTP backend (Gmail App Password).
"""
import logging
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from django.conf import settings
from django.core.mail import send_mail, EmailMultiAlternatives

from apps.predictions.models import EmailReport, FetchRun

logger = logging.getLogger(__name__)


def send_report(subject: str, html_body: str, plain_body: str, run: FetchRun) -> bool:
    """
    Sends the HTML prediction report via Gmail SMTP.
    Logs the result to the EmailReport model.
    Returns True if sent successfully.
    """
    recipient = settings.REPORT_RECIPIENT
    if not recipient:
        logger.error("REPORT_RECIPIENT is not set in .env")
        return False

    report = EmailReport(run=run, recipient=recipient, subject=subject)

    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=plain_body,
            from_email=settings.EMAIL_HOST_USER,
            to=[recipient],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send()
        report.success = True
        logger.info("Report sent to %s", recipient)
        run.email_sent = True
        run.save(update_fields=["email_sent"])
    except Exception as exc:
        report.error_message = str(exc)
        logger.error("Failed to send email: %s", exc)
    finally:
        report.save()

    return report.success
