"""SSRF guard: keep scraped URLs from making the worker request internal addresses.

A listing controls the image URLs it carries and the redirects its host sends back.
Left alone, either could aim the worker at loopback, a private network, a cloud
metadata endpoint or a Docker-network service. This module

* resolves a host and refuses any address that isn't globally routable: private,
  loopback, link-local (169.254.169.254 included), multicast, reserved, unspecified
  and carrier-grade NAT, for IPv4 and IPv6, with IPv4-mapped/6to4/NAT64 forms judged
  by the address they embed;
* :class:`GuardedTransport` connects to the address it validated, so DNS rebinding
  can't swap it afterwards. Every redirect hop goes through the transport, so each
  hop is checked;
* :func:`guard_hook` is a request event hook for clients that can't use the pinning
  transport (an injected transport or a proxy, which resolves names itself): it
  checks resolved addresses on every hop without pinning.

``allow_private`` (``SOURCES_ALLOW_PRIVATE_ADDRESSES``) turns the guard off.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from typing import Any

import httpx2

Resolver = Callable[[str, int], list[str]]

_EXTRA_BLOCKED = (
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("fc00::/7"),
)
_EMBEDDED_V4_NETS = (
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64
)


class UnsafeAddressError(Exception):
    """A URL resolves to an address the worker must not request."""


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def is_public_address(address: str) -> bool:
    """Whether ``address`` is a globally routable unicast address."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and any(ip in net for net in _EMBEDDED_V4_NETS):
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            return is_public_address(str(embedded))
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    ):
        return False
    return not any(ip in net for net in _EXTRA_BLOCKED if net.version == ip.version)


def resolve_public(host: str, port: int, resolver: Resolver | None = None) -> list[str]:
    """The addresses of ``host``, all of which must be public.

    Raises :class:`UnsafeAddressError` if any is not (one bad record is enough:
    the connection library may pick any of them) or the name doesn't resolve.
    """
    host = host.strip("[]")
    try:
        addresses = (resolver or default_resolver)(host, port)
    except OSError as exc:
        msg = f"cannot resolve {host}"
        raise UnsafeAddressError(msg) from exc
    if not addresses:
        msg = f"cannot resolve {host}"
        raise UnsafeAddressError(msg)
    for address in addresses:
        if not is_public_address(address):
            msg = f"{host} resolves to non-public address {address}"
            raise UnsafeAddressError(msg)
    return addresses


def _port(url: httpx2.URL) -> int:
    return url.port or (443 if url.scheme == "https" else 80)


class GuardedTransport(httpx2.HTTPTransport):
    """An ``HTTPTransport`` that connects only to validated, pinned addresses.

    The request URL's host is replaced by the validated IP; ``Host`` stays as built
    and TLS SNI and certificate checks still use the original name.
    """

    def __init__(self, *, resolver: Resolver | None = None, **kwargs: Any) -> None:  # noqa: ANN401
        super().__init__(**kwargs)
        self._resolver = resolver

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        host = request.url.host
        addresses = resolve_public(host, _port(request.url), self._resolver)
        request.url = request.url.copy_with(host=addresses[0])
        if request.url.scheme == "https":
            request.extensions = {**request.extensions, "sni_hostname": host}
        return super().handle_request(request)


def guard_hook(resolver: Resolver | None = None) -> Callable[[httpx2.Request], None]:
    """A ``request`` event hook that refuses non-public targets (no pinning)."""

    def hook(request: httpx2.Request) -> None:
        resolve_public(request.url.host, _port(request.url), resolver)

    return hook


def guarded_client_args(
    *,
    transport: httpx2.BaseTransport | None,
    proxy: str | None,
    allow_private: bool,
    resolver: Resolver | None = None,
) -> dict[str, Any]:
    """``httpx2.Client`` keyword arguments (transport, event_hooks) applying the guard."""
    if allow_private:
        return {"transport": transport}
    if transport is None and proxy is None:
        return {"transport": GuardedTransport(resolver=resolver)}
    return {"transport": transport, "event_hooks": {"request": [guard_hook(resolver)]}}
