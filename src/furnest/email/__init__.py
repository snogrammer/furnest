"""Email: SMTP sending (:mod:`.smtp`, :mod:`.send`), inquiries, IMAP reply polling (:mod:`.imap`).

Outbound mail is logged in ``emails`` (:mod:`furnest.db.email_store`) and sent by a
queued job, so a flaky SMTP server only delays it.
"""
