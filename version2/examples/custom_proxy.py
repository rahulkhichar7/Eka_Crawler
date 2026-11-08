"""Examples of custom proxy providers.

Use one with any command:
    python main.py crawl --proxy examples/custom_proxy.py:ApiProxyPool?api_url=https://my-provider/list
    python main.py download pdfs --in urls.txt --proxy "examples/custom_proxy.py:StickyPerHost?file=proxies.txt"

A provider only needs one method:  get(url) -> "http://host:port" | None
Optional hooks: report_success(proxy, url), report_failure(proxy, url, error).
Keyword arguments come from the ?key=value part of the --proxy string (always strings).
"""
import hashlib
import threading
import time

from eka.core.proxy import FileProxyPool, ProxyPool, ProxyProvider


class ApiProxyPool(ProxyPool):
    """Fetch the proxy list from a provider URL (one proxy per line, or JSON list)
    and refresh it every `refresh` seconds."""

    name = "api"

    def __init__(self, api_url, refresh=1800, **kw):
        self.api_url = api_url
        self.refresh = float(refresh)
        self._t = 0.0
        super().__init__(self._fetch(), **kw)
        self._t = time.monotonic()
        self._refresh_lock = threading.Lock()

    def _fetch(self):
        import requests
        try:
            r = requests.get(self.api_url, timeout=30)
            r.raise_for_status()
            try:
                data = r.json()
                return [str(p) for p in (data if isinstance(data, list) else data.get("proxies", []))]
            except ValueError:
                return [l.strip() for l in r.text.splitlines() if l.strip()]
        except Exception:
            return []

    def get(self, url):
        if time.monotonic() - self._t > self.refresh and self._refresh_lock.acquire(blocking=False):
            try:
                fresh = self._fetch()
                if fresh:
                    self._set(fresh)
                self._t = time.monotonic()
            finally:
                self._refresh_lock.release()
        return super().get(url)


class StickyPerHost(ProxyProvider):
    """Always use the same proxy for the same host (some sites dislike IP changes
    mid-session). Proxies come from a file."""

    name = "sticky"

    def __init__(self, file, **kw):
        self.pool = FileProxyPool(file, **kw)

    def get(self, url):
        proxies = self.pool.proxies
        if not proxies:
            return None
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return proxies[int(hashlib.md5(host.encode()).hexdigest(), 16) % len(proxies)]

    def report_failure(self, proxy, url, error):
        self.pool.report_failure(proxy, url, error)


class OnlyForHosts(ProxyProvider):
    """Use a proxy only for some hosts (e.g. a site that blocks your IP), direct otherwise."""

    name = "only-for"

    def __init__(self, proxy, hosts):
        self.proxy = proxy if "://" in proxy else "http://" + proxy
        self.hosts = tuple(h.strip().lower() for h in hosts.split(",") if h.strip())

    def get(self, url):
        host = url.split("://", 1)[-1].split("/", 1)[0].lower()
        return self.proxy if host.endswith(self.hosts) else None
