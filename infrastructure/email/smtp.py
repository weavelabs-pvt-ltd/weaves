"""SMTP and local-console adapters for account email delivery."""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

from application.ports.email import EmailSender
from shared.errors import EmailDeliveryUnavailable

LOGGER = logging.getLogger(__name__)


class SmtpEmailSender:
    """Send plain-text messages through a configured SMTP relay."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str = "",
        password: str = "",
        start_tls: bool = True,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Store relay settings without opening a network connection."""
        self.host = host.strip()
        self.port = port
        self.sender = sender.strip()
        self.username = username
        self.password = password
        self.start_tls = start_tls
        self.timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        """Return whether the relay has the minimum delivery settings."""
        return bool(self.host and self.sender and 1 <= self.port <= 65535)

    def send(self, recipient: str, subject: str, body: str) -> None:
        """Send a message using STARTTLS when enabled."""
        if not self.configured:
            raise EmailDeliveryUnavailable("Email delivery is not configured")
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(
                self.host, self.port, timeout=self.timeout_seconds
            ) as client:
                if self.start_tls:
                    client.starttls(context=ssl.create_default_context())
                if self.username:
                    client.login(self.username, self.password)
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            LOGGER.warning("Account email delivery failed", exc_info=exc)
            raise EmailDeliveryUnavailable(
                "Account email could not be delivered"
            ) from exc


class ConsoleEmailSender:
    """Explicit local-development delivery that prints messages to server logs."""

    @property
    def configured(self) -> bool:
        """Local delivery is always configured when explicitly selected."""
        return True

    def send(self, recipient: str, subject: str, body: str) -> None:
        """Write the one-time code to local logs for development only."""
        LOGGER.warning(
            "Local email delivery: to=%s subject=%s body=%s", recipient, subject, body
        )


class UnavailableEmailSender:
    """Fail safely when neither SMTP nor local development delivery is enabled."""

    @property
    def configured(self) -> bool:
        """Email delivery is unavailable until an adapter is configured."""
        return False

    def send(self, recipient: str, subject: str, body: str) -> None:
        """Reject messages without exposing authentication details."""
        raise EmailDeliveryUnavailable("Email delivery is not configured")


def build_email_sender() -> EmailSender:
    """Select SMTP, explicitly enabled local logging, or an unavailable adapter."""
    if os.environ.get("WEAVES_AUTH_EMAIL_CONSOLE", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        return ConsoleEmailSender()
    host = os.environ.get("WEAVES_SMTP_HOST", "")
    if not host:
        return UnavailableEmailSender()
    return SmtpEmailSender(
        host=host,
        port=int(os.environ.get("WEAVES_SMTP_PORT", "587")),
        sender=os.environ.get("WEAVES_SMTP_FROM", ""),
        username=os.environ.get("WEAVES_SMTP_USERNAME", ""),
        password=os.environ.get("WEAVES_SMTP_PASSWORD", ""),
        start_tls=os.environ.get("WEAVES_SMTP_STARTTLS", "true").lower()
        in {"1", "true", "yes"},
    )
