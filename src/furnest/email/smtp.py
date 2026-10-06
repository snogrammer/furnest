"""Building and sending one email over SMTP (``SMTP_*`` settings).

:class:`SmtpSender` opens a connection per message: inquiries are rare, and a fresh
connection never trips over a server's idle timeout. ``SMTP_SECURITY`` picks
implicit TLS (``ssl``, usually port 465), STARTTLS (``starttls``, usually 587) or a
plain connection (``none``, for a local relay). Both TLS modes verify the server's
certificate.

:func:`is_permanent` sorts failures for the send job: a 5xx reply (bad recipient,
rejected sender, bad credentials) or a message the server can't accept as written
won't succeed on retry; anything else (4xx, dropped connections, timeouts) might.
"""

import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, formatdate

from furnest.config import SmtpSecurity, SmtpSettings


def build_message(
    *,
    from_address: str,
    from_name: str | None,
    to: str,
    subject: str,
    body: str,
    message_id: str,
) -> EmailMessage:
    """A plain-text message with the headers furnest sets on every email."""
    message = EmailMessage()
    message["From"] = formataddr((from_name, from_address)) if from_name else from_address
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False, usegmt=True)
    message["Message-ID"] = message_id
    message.set_content(body, charset="utf-8")
    return message


def is_permanent(error: BaseException) -> bool:
    """True when retrying ``error`` can't help."""
    if isinstance(error, smtplib.SMTPRecipientsRefused):
        # Every recipient was refused; permanent unless one was only deferred (4xx).
        return all(code >= 500 for code, _ in error.recipients.values())
    if isinstance(error, smtplib.SMTPResponseException):
        return error.smtp_code >= 500
    # A malformed header or an address the server can't take (non-ASCII without
    # SMTPUTF8) fails the same way every time.
    return isinstance(error, ValueError | smtplib.SMTPNotSupportedError)


class SmtpSender:
    def __init__(self, settings: SmtpSettings) -> None:
        if settings.host is None:
            msg = "SMTP_HOST is not set"
            raise ValueError(msg)
        self._settings = settings
        self._host = settings.host

    def send(self, message: EmailMessage) -> None:
        """Deliver ``message`` to the server, or raise an ``smtplib``/``OSError`` error."""
        settings = self._settings
        timeout = settings.timeout_seconds
        smtp: smtplib.SMTP
        if settings.security is SmtpSecurity.SSL:
            smtp = smtplib.SMTP_SSL(
                self._host, settings.port, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            smtp = smtplib.SMTP(self._host, settings.port, timeout=timeout)
        with smtp:
            smtp.ehlo()
            if settings.security is SmtpSecurity.STARTTLS:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if settings.username is not None:
                password = settings.password.get_secret_value() if settings.password else ""
                smtp.login(settings.username, password)
            smtp.send_message(message)
