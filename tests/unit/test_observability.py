"""Sentry initialisation and job-failure reporting."""

import json
from typing import Any

import pytest
import sentry_sdk
from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import Transport

import furnest
from furnest.config import LogFormat, LoggingSettings, Settings
from furnest.logging import REDACTED, configure_logging
from furnest.observability import capture_job_failure, init_sentry

DSN = "https://public@o0.ingest.example.com/0"
SECRET = "worker-bot-token-xyz"  # noqa: S105 - fake secret under test


class CapturingTransport(Transport):
    """Keeps events in memory instead of sending them."""

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        super().__init__(options)
        self.events: list[dict[str, Any]] = []

    def capture_envelope(self, envelope: Envelope) -> None:
        event = envelope.get_event()
        if event is not None:
            self.events.append(dict(event))


@pytest.fixture
def transport(monkeypatch: pytest.MonkeyPatch) -> CapturingTransport:
    """Make ``sentry_sdk.init`` deliver to an in-memory transport."""
    captured = CapturingTransport()
    real_init = sentry_sdk.init

    def init(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401 - mirrors sentry_sdk.init
        kwargs["transport"] = captured
        return real_init(*args, **kwargs)

    monkeypatch.setattr(sentry_sdk, "init", init)
    return captured


def sentry_settings() -> Settings:
    return Settings.model_validate(
        {
            "app": {"environment": "test"},
            "sentry": {"dsn": DSN},
            "discord": {"bot_token": SECRET},
        }
    )


def test_init_is_a_noop_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        pytest.fail("sentry_sdk.init must not be called without a DSN")

    monkeypatch.setattr(sentry_sdk, "init", fail)

    assert init_sentry("worker", Settings()) is False
    assert not sentry_sdk.get_client().is_active()


def test_capture_job_failure_without_sentry_still_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging(LoggingSettings(format=LogFormat.JSON))

    event_id = capture_job_failure(ValueError("bad page"), job_id=7, job_type="scrape")

    assert event_id is None
    (line,) = [json.loads(x) for x in capsys.readouterr().err.splitlines()]
    assert line["event"] == "job_failed"
    assert (line["job_id"], line["job_type"]) == (7, "scrape")
    assert "ValueError: bad page" in line["exception"]


def test_init_tags_release_environment_and_component(transport: CapturingTransport) -> None:
    assert init_sentry("web", sentry_settings()) is True

    options = sentry_sdk.get_client().options
    assert options["send_default_pii"] is False
    assert options["release"] == f"furnest@{furnest.__version__}"
    assert options["environment"] == "test"

    sentry_sdk.capture_message("hello")
    sentry_sdk.flush()

    (event,) = transport.events
    assert event["tags"]["component"] == "web"


def test_sentry_environment_setting_overrides_app_environment(
    transport: CapturingTransport,
) -> None:
    settings = Settings.model_validate(
        {"sentry": {"dsn": DSN, "environment": "staging"}, "app": {"environment": "production"}}
    )

    init_sentry("worker", settings)

    assert sentry_sdk.get_client().options["environment"] == "staging"


def test_events_are_redacted_before_sending(transport: CapturingTransport) -> None:
    init_sentry("worker", sentry_settings())

    sentry_sdk.set_extra("headers", {"Authorization": "Bearer abc.def.ghi"})
    sentry_sdk.add_breadcrumb(message=f"posting with {SECRET}")
    try:
        raise RuntimeError(f"discord rejected {SECRET}")
    except RuntimeError as exc:
        sentry_sdk.capture_exception(exc)
    sentry_sdk.flush()

    (event,) = transport.events
    payload = json.dumps(event, default=str)
    assert SECRET not in payload
    assert "abc.def.ghi" not in payload
    assert event["extra"]["headers"]["Authorization"] == REDACTED


def test_capture_job_failure_tags_event(transport: CapturingTransport) -> None:
    init_sentry("worker", sentry_settings())

    event_id = capture_job_failure(
        RuntimeError("timeout"), job_id=12, job_type="scrape", source="example-source"
    )
    sentry_sdk.flush()

    assert event_id is not None
    (event,) = transport.events
    assert event["tags"] == {
        "component": "worker",
        "job_id": "12",
        "job_type": "scrape",
        "source": "example-source",
    }
