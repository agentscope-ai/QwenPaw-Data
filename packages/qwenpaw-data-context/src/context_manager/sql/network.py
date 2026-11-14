"""Reuse OSS net_guard policy and DNS pinning at datasource I/O boundaries.

External SQL origins use the existing exact CALLBACK_ALLOWLIST policy. No
built-in callback exception, ambient proxy, default credential or DNS relookup
is allowed. SDK sessions are request-owned rather than credential-keyed caches.
"""

from __future__ import annotations

from time import monotonic
from urllib.parse import urlsplit, urlunsplit

from .. import net_guard


def resolve_url(url: str):
    parsed = urlsplit(url)
    if parsed.username is not None or parsed.password is not None or not parsed.hostname:
        raise net_guard.CallbackUrlError("invalid datasource endpoint")
    # Validate the origin at a neutral path; fixed internal callback paths do
    # not authorize database or SDK traffic.
    origin = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
    return net_guard._resolve_target(origin)


def resolve_host(host: str, port: int):
    rendered = f"[{host}]" if ":" in host else host
    return resolve_url(f"http://{rendered}:{port}/")


def guarded_async_transport():
    """Provide the same DNS-pinned policy for the Explorer's native model stream."""
    import asyncio
    import httpx

    class GuardedTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self._transports = {}

        async def handle_async_request(self, request):
            target = await asyncio.to_thread(resolve_url, str(request.url))
            key = (request.url.scheme, target.hostname, target.port, target.addresses[0])
            if key not in self._transports:
                if len(self._transports) >= 8:
                    raise ValueError("too many outbound Explorer origins")
                self._transports[key] = httpx.AsyncHTTPTransport(
                    trust_env=False, retries=0, limits=httpx.Limits(max_connections=4, max_keepalive_connections=4))
            extensions = {**request.extensions, "sni_hostname": target.hostname}
            headers = request.headers.copy()
            headers["Host"] = request.url.netloc.decode()
            pinned = httpx.Request(request.method, request.url.copy_with(host=target.addresses[0]),
                                   headers=headers, stream=request.stream, extensions=extensions)
            return await self._transports[key].handle_async_request(pinned)

        async def aclose(self):
            for transport in self._transports.values():
                await transport.aclose()
            self._transports.clear()

    return GuardedTransport()


def guarded_session(session=None, *, deadline=None):
    """Install guarded/pinned HTTP adapters, importing HTTP support on selection."""
    import requests
    from requests.adapters import HTTPAdapter

    class GuardedAdapter(HTTPAdapter):
        def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
            target = resolve_url(request.url)
            host, options = self.build_connection_pool_key_attributes(request, verify, cert)
            host["host"] = target.addresses[0]
            if host["scheme"] == "https":
                options.update(server_hostname=target.hostname, assert_hostname=target.hostname)
            return self.poolmanager.connection_from_host(**host, pool_kwargs=options)

        def send(self, request, **kwargs):
            if kwargs.get("proxies"):
                raise net_guard.CallbackUrlError("datasource proxies are not allowed")
            remaining = deadline() - monotonic() if deadline is not None else 30.0
            if remaining <= 0:
                raise TimeoutError("SQL client deadline exceeded")
            configured = kwargs.get("timeout")
            if isinstance(configured, (int, float)):
                remaining = min(remaining, configured)
            elif isinstance(configured, tuple):
                remaining = min(remaining, *(v for v in configured if v is not None))
            kwargs.update(timeout=remaining, proxies={}, stream=True)
            request = request.copy()
            request.headers = request.headers.copy()
            request.headers["Host"] = urlsplit(request.url).netloc
            response = super().send(request, **kwargs)
            # SDK JSON/XML readers materialize their response. Bound that
            # materialization as well as the socket waiting time.
            content = bytearray()
            try:
                for chunk in response.iter_content(64 * 1024):
                    if deadline is not None and monotonic() >= deadline():
                        raise TimeoutError("SQL download deadline exceeded")
                    content.extend(chunk)
                    if len(content) > 64 * 1024 * 1024:
                        raise ValueError("SQL download exceeds 64 MiB")
                response._content = bytes(content)
                response._content_consumed = True
                return response
            except BaseException:
                response.close()
                raise

    session = session if session is not None else requests.Session()
    session.trust_env = False
    session.mount("https://", GuardedAdapter(pool_connections=4, pool_maxsize=4, max_retries=0, pool_block=True))
    session.mount("http://", GuardedAdapter(pool_connections=4, pool_maxsize=4, max_retries=0, pool_block=True))
    return session
