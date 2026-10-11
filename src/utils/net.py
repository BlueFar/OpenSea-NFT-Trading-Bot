"""
Network helpers. Some home networks advertise IPv6 but can't actually reach the internet over it.
requests (urllib3) tries IPv6 first, so every call to OpenSea then stalls until it times out.
OpenSea works over IPv4, so by default the bot only uses IPv4 (runtime.ipv4_only).
"""
import socket

import urllib3.util.connection as urllib3_connection

_original_family = urllib3_connection.allowed_gai_family


def use_ipv4_only(enabled: bool = True) -> None:
    """Makes requests/urllib3 connect over IPv4 only (enabled) or restores the default."""
    if enabled:
        urllib3_connection.allowed_gai_family = lambda: socket.AF_INET
    else:
        urllib3_connection.allowed_gai_family = _original_family


def can_connect(host: str, port: int = 443, timeout: float = 5.0, ipv4_only: bool = True) -> bool:
    """True if a TCP connection to host:port can be opened."""
    family = socket.AF_INET if ipv4_only else socket.AF_UNSPEC
    try:
        addresses = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
    except OSError:
        return False
    for af, socktype, proto, _, addr in addresses:
        try:
            with socket.socket(af, socktype, proto) as s:
                s.settimeout(timeout)
                s.connect(addr)
                return True
        except OSError:
            continue
    return False
