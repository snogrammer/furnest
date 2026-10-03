"""The suite is hermetic: pytest-socket refuses network sockets in every test."""

import socket

import pytest
from pytest_socket import SocketBlockedError

pytestmark = pytest.mark.unit


@pytest.mark.filterwarnings("ignore:A test tried to use socket:UserWarning")
def test_outbound_network_is_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("example.com", 80), timeout=1)


def test_unix_socketpairs_still_work() -> None:
    # asyncio's event loop needs these; see addopts in pyproject.toml.
    left, right = socket.socketpair()
    with left, right:
        left.sendall(b"ping")
        assert right.recv(4) == b"ping"
