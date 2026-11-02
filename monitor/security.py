"""URL safety for a publicly reachable deployment.

Anyone who can add a source makes the SERVER fetch that URL, so private/internal targets
(cloud metadata, localhost, internal networks) must be refused. Best effort: the check runs
when a source is added, so a hostname that later re-points, or a redirect to a private
address, is not caught here. Keep the admin behind a login (see admin.py).
"""
import ipaddress
import socket
from urllib.parse import urlparse


def validate_public_url(url: str) -> None:
    """Raise ValueError unless url is http(s) and its host resolves only to public addresses."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("url must start with http:// or https://")
    if not p.hostname:
        raise ValueError("url has no host")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise ValueError(f"cannot resolve host {p.hostname!r}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise ValueError("url points to a private or internal address, which is not allowed")
