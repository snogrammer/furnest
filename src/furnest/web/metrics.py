"""Prometheus text exposition for ``/metrics`` (optional, off by default).

The web and worker are separate processes, so nothing is counted in memory:
every sample is read from the database at scrape time. That keeps the numbers
correct across restarts and needs no extra dependency. The label values are
adapter names and fixed status words, never user data.
"""

from collections.abc import Iterable

from sqlalchemy import Engine, func, select

from furnest.db.models import Email, Job, Listing, Property, Source

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

Labels = dict[str, str]
Sample = tuple[Labels, float]


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _family(name: str, kind: str, help_text: str, samples: Iterable[Sample]) -> list[str]:
    lines = [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        rendered = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(labels.items()))
        lines.append(f"{name}{{{rendered}}} {value:g}" if rendered else f"{name} {value:g}")
    return lines


def render_metrics(engine: Engine) -> str:
    """Current metrics in the Prometheus text format."""
    with engine.connect() as connection:
        sources = connection.execute(select(Source)).all()
        listings = connection.execute(
            select(Listing.source, func.count()).group_by(Listing.source)
        ).all()
        jobs = connection.execute(select(Job.status, func.count()).group_by(Job.status)).all()
        emails = connection.execute(
            select(Email.direction, Email.status, func.count()).group_by(
                Email.direction, Email.status
            )
        ).all()
        notified = connection.execute(
            select(func.count())
            .select_from(Property)
            .where(Property.discord_message_id.is_not(None))
        ).scalar_one()

    def stamp(value: object) -> float:
        return value.timestamp() if value is not None else 0.0  # type: ignore[attr-defined]

    lines: list[str] = []
    for name, help_text, field in (
        ("furnest_source_last_run_timestamp_seconds", "Last run of a source.", "last_run_at"),
        (
            "furnest_source_last_success_timestamp_seconds",
            "Last successful run of a source.",
            "last_success_at",
        ),
    ):
        lines += _family(
            name,
            "gauge",
            help_text,
            [({"source": s.name}, stamp(getattr(s, field))) for s in sources],
        )
    lines += _family(
        "furnest_source_consecutive_failures",
        "gauge",
        "Failed runs of a source since its last success.",
        [({"source": s.name}, s.consecutive_failures) for s in sources],
    )
    lines += _family(
        "furnest_source_last_run_listings",
        "gauge",
        "Listings found by a source's last successful run.",
        [({"source": s.name}, s.listings_found) for s in sources],
    )
    lines += _family(
        "furnest_listings",
        "gauge",
        "Listings ingested, by source.",
        [({"source": source}, count) for source, count in listings],
    )
    lines += _family(
        "furnest_jobs",
        "gauge",
        "Jobs in the queue, by status (queued is the queue depth).",
        [({"status": str(status.value)}, count) for status, count in jobs],
    )
    lines += _family(
        "furnest_notifications_sent",
        "gauge",
        "Properties announced on Discord.",
        [({}, notified)],
    )
    lines += _family(
        "furnest_emails",
        "gauge",
        "Emails by direction and status (outbound sent = emails sent).",
        [({"direction": d.value, "status": s.value}, count) for d, s, count in emails],
    )
    return "\n".join(lines) + "\n"
