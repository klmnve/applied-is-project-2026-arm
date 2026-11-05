"""The socket peer is authoritative unless it is an explicitly trusted proxy."""

from ipaddress import ip_address, ip_network

from fastapi import Request


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    try:
        address = ip_address(peer)
    except ValueError:
        return "unknown"
    networks = [
        ip_network(value.strip(), strict=False)
        for value in request.app.state.settings.trusted_proxies.split(",")
        if value.strip()
    ]
    if not any(address in network for network in networks):
        return str(address)
    forwarded = request.headers.get("x-forwarded-for", "")
    try:
        chain = [ip_address(value.strip()) for value in forwarded.split(",") if value.strip()]
    except ValueError:
        return str(address)
    # Walk right-to-left so an untrusted client cannot prepend a forged address.
    for candidate in reversed(chain):
        if not any(candidate in network for network in networks):
            return str(candidate)
    return str(chain[0]) if chain else str(address)
