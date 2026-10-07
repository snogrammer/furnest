"""The email log; the logic is in ``furnest.db.email_store`` and ``furnest.email``.

Every email furnest sends gets a row here with the exact text that went out, before
it is sent: :func:`furnest.db.email_store.queue_email` inserts it as ``queued`` and
queues an ``email.send`` job in the same transaction. The job moves it to ``sent``,
or to ``failed`` when the server refuses it outright or the queue runs out of
retries. ``error`` keeps the last failure either way.

An inquiry in review mode starts as a ``draft`` (``furnest.email.inquiry``): the
rendered text, waiting in the dashboard's review queue. Sending it turns the same
row ``queued``, so the log keeps one row per inquiry whatever the mode. A listing
with no email address is contacted by hand: its draft is copied into the source's
own messaging and *marked* as sent, which makes the row ``sent`` with no recipient.

``message_id`` is generated once, when the row is created, so every retry sends the
same Message-ID header. Replies quote it in In-Reply-To/References, which is how
the IMAP poller (``furnest.email.imap``) matches them; it logs each reply as an
``inbound`` row with status ``received``, keyed by the reply's own Message-ID.
"""

from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from furnest.db.base import Base, UTCDateTime, utcnow


class EmailDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class EmailStatus(StrEnum):
    DRAFT = "draft"  # an inquiry waiting for review; not sent until someone sends it
    QUEUED = "queued"  # waiting to be sent, or between retries (see ``error``)
    SENT = "sent"
    FAILED = "failed"  # refused permanently, or out of retries
    RECEIVED = "received"  # an inbound reply, found by the IMAP poller


class Email(Base):
    __tablename__ = "emails"
    __table_args__ = (Index("ix_emails_property_id_created_at", "property_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    # NULL for mail about no property (a test send), or once the property is gone.
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id", ondelete="SET NULL")
    )
    direction: Mapped[EmailDirection] = mapped_column(
        Enum(
            EmailDirection,
            name="email_direction",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda directions: [d.value for d in directions],
        ),
        default=EmailDirection.OUTBOUND,
    )
    # NULL only on a draft whose listing has no email address. On an inbound row,
    # the mailbox the reply arrived at.
    to_address: Mapped[str | None] = mapped_column(String(320))
    # NULL only on an inquiry drafted while SMTP wasn't set up (filled in on send),
    # or one that was then marked as sent by hand.
    from_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str] = mapped_column(Text)
    body_rendered: Mapped[str] = mapped_column(Text)
    status: Mapped[EmailStatus] = mapped_column(
        Enum(
            EmailStatus,
            name="email_status",
            native_enum=False,
            create_constraint=True,
            length=16,
            values_callable=lambda statuses: [s.value for s in statuses],
        ),
        default=EmailStatus.QUEUED,
    )
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(default=0)
    # The Message-ID header, angle brackets included.
    message_id: Mapped[str] = mapped_column(String(255), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
