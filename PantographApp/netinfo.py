"""
This computer's addresses on the local network: the IP to type into iDraw OSC.

No packets are sent. The "primary" address is the one the OS would use to reach
the internet (a UDP socket's connect() only picks a route). The rest come from
the host name. Link-local 169.254.* addresses (no DHCP answer) are hidden:
an iPad can't be told to use one.
"""

import ipaddress
import socket


def _primary() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 80))            # TEST-NET-1: never actually contacted
        return s.getsockname()[0]
    except OSError:
        return None                             # no route at all (offline)
    finally:
        s.close()


def _label(ip: str, primary: bool) -> str:
    if ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10"):
        return "Tailscale"
    return "Wi-Fi / network" if primary else "other adapter (VPN, wired…)"


def local_ips() -> list[dict]:
    """[{"ip", "label", "primary"}], primary first. Empty if the computer has no network."""
    primary = _primary()
    found = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        found = [i[4][0] for i in infos]
    except OSError:
        pass
    if primary:
        found.insert(0, primary)

    out, seen = [], set()
    for ip in found:
        if ip in seen or ip.startswith("127.") or ip.startswith("169.254."):
            continue
        seen.add(ip)
        out.append({"ip": ip, "label": _label(ip, ip == primary), "primary": ip == primary})
    return out
