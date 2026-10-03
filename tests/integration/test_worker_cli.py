"""``furnest worker`` as a real process: it heartbeats, then exits cleanly on SIGTERM."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from furnest.db.models import WorkerHeartbeat
from furnest.db.session import session_scope

pytestmark = pytest.mark.integration


def test_worker_process_heartbeats_and_stops_on_sigterm(
    db_url: str, session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("SENTRY_", "WORKER_"))}
    env |= {"DB_URL": db_url, "LOG_FORMAT": "json", "WORKER_POLL_INTERVAL_SECONDS": "0.1"}
    process = subprocess.Popen(
        [sys.executable, "-m", "furnest", "worker"],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20
        while True:
            with session_scope(session_factory) as session:
                beats = session.scalar(select(func.count()).select_from(WorkerHeartbeat))
            if beats or process.poll() is not None or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        assert beats == 1, process.communicate(timeout=5)

        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0, stderr
    for event in ("worker_started", "worker_stop_requested", "worker_stopped"):
        assert f'"event": "{event}"' in stderr or f'"event":"{event}"' in stderr
