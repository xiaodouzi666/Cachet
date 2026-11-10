"""Keep TLS verification bound to the announced hostname while Quick Tunnel DNS propagates."""
import ipaddress
import socket
import time

import httpx


class TunnelTransport(httpx.HTTPTransport):
    def __init__(self, origin):
        super().__init__(trust_env=False)
        self.host = httpx.URL(origin).host

    def handle_request(self, request):
        if request.url.scheme != "https" or request.url.host != self.host:
            raise httpx.UnsupportedProtocol("Registered GPU requests must use their announced TLS origin")
        if not self.host.endswith(".trycloudflare.com"):
            return super().handle_request(request)
        try:
            socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            # Some recursive resolvers keep the initial negative reply after the new record exists.
            # Cloudflare's HTTPS resolver provides the current A record. Host and certificate stay unchanged.
            address = resolve_quick_tunnel(self.host)
            if address:
                headers = request.headers.copy()
                headers["host"] = self.host
                request = httpx.Request(
                    request.method, request.url.copy_with(host=address),
                    headers=headers, stream=request.stream,
                    extensions={**request.extensions, "sni_hostname": self.host},
                )
        return super().handle_request(request)


_cache = {}


def resolve_quick_tunnel(host):
    cached = _cache.get(host)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    with httpx.Client(timeout=2.5, trust_env=False, follow_redirects=False) as dns:
        result = dns.get("https://cloudflare-dns.com/dns-query",
                         params={"name":host,"type":"A"}, headers={"Accept":"application/dns-json"})
        result.raise_for_status()
        data = result.json()
    for item in data.get("Answer", []):
        if item.get("type") != 1 or item.get("name","").rstrip(".").lower() != host:
            continue
        ip = ipaddress.IPv4Address(item["data"])
        if ip.is_global:
            if len(_cache) > 32:
                _cache.clear()
            _cache[host] = (time.monotonic()+min(60,max(1,int(item.get("TTL",30)))), str(ip))
            return str(ip)
    return None
