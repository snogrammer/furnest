"""Email: SMTP sending (:mod:`.smtp`, :mod:`.send`), inquiries and, later, IMAP reply polling.

Outbound mail is logged in ``emails`` (:mod:`furnest.db.email_store`) and sent by a
queued job, so a flaky SMTP server only delays it.
"""
