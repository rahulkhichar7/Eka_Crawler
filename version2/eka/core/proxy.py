"""Pluggable proxy support.

The pipeline uses NO proxy by default. Every HTTP request asks a ProxyProvider
which proxy to use (or None), so attaching a proxy is one command-line flag:

    --proxy none                                    (default) direct connection
    --proxy http://user:pass@1.2.3.4:8080           one fixed proxy
    --proxy socks5://1.2.3.4:1080                   needs: pip install requests[socks]
    --proxy file:proxies.txt                        rotate through a list (one per line)
    --proxy "file:proxies.txt?strategy=round_robin&reload=600&max_failures=3&cooldown=300"
    --proxy examples/custom_proxy.py:ApiProxyPool   your own class (file path or module)
    --proxy "mymodule:MyProxy?api_url=https://...&key=abc"   kwargs after '?'

To write your own provider, subclass ProxyProvider and implement get().
See examples/custom_proxy.py.
"""
import importlib
import importlib.util
import logging
import os
import random
import re
import threading
import time
from urllib.parse import parse_qsl

log = logging.getLogger("eka.proxy")


class ProxyProvider:
    """Base class. Return a proxy URL (e.g. 'http://host:port') or None for direct."""

    name = "base"

    def get(self, url):
        raise NotImplementedError

    def report_success(self, proxy, url):
        """Called after a request through `proxy` worked. Optional."""

    def report_failure(self, proxy, url, error):
        """Called after a request through `proxy` failed (connection/timeout/407). Optional."""

    def describe(self):
        return self.name

    # used by HttpClient
    def requests_proxies(self, url):
        p = self.get(url)
        return (p, {"http": p, "https": p}) if p else (None, None)


class NoProxy(ProxyProvider):
    name = "none"

    def get(self, url):
        return None


class SingleProxy(ProxyProvider):
    name = "single"

    def __init__(self, proxy_url):
        self.proxy_url = _with_scheme(proxy_url)

    def get(self, url):
        return self.proxy_url

    def describe(self):
        return f"single ({_mask(self.proxy_url)})"


class ProxyPool(ProxyProvider):
    """Rotate through a list. Proxies that fail `max_failures` times in a row are
    benched for `cooldown` seconds. Thread-safe."""

    name = "pool"

    def __init__(self, proxies, strategy="random", max_failures=3, cooldown=300):
        if strategy not in ("random", "round_robin"):
            raise ValueError("strategy must be random or round_robin")
        self.strategy = strategy
        self.max_failures = int(max_failures)
        self.cooldown = float(cooldown)
        self._lock = threading.Lock()
        self._rr = 0
        self._fails = {}
        self._benched = {}
        self._set(proxies)

    def _set(self, proxies):
        with self._lock:
            self.proxies = [_with_scheme(p) for p in proxies if p and p.strip()]

    def get(self, url):
        with self._lock:
            now = time.monotonic()
            live = [p for p in self.proxies if self._benched.get(p, 0) <= now]
            if not live:
                live = self.proxies  # everything benched: use them anyway
            if not live:
                return None
            if self.strategy == "random":
                return random.choice(live)
            self._rr = (self._rr + 1) % len(live)
            return live[self._rr]

    def report_success(self, proxy, url):
        with self._lock:
            self._fails.pop(proxy, None)

    def report_failure(self, proxy, url, error):
        with self._lock:
            n = self._fails.get(proxy, 0) + 1
            self._fails[proxy] = n
            if n >= self.max_failures:
                self._benched[proxy] = time.monotonic() + self.cooldown
                self._fails[proxy] = 0
                log.warning(f"proxy {_mask(proxy)} benched for {self.cooldown:.0f}s after {n} failures")

    def describe(self):
        return f"{self.name} ({len(self.proxies)} proxies, {self.strategy})"


class FileProxyPool(ProxyPool):
    """ProxyPool read from a text file (one proxy per line, '#' comments).
    Re-reads the file every `reload` seconds, so another process can refresh it."""

    name = "file"

    def __init__(self, path, reload=600, **kw):
        self.path = path
        self.reload = float(reload)
        self._loaded_at = 0.0
        super().__init__(self._read(), **kw)
        self._loaded_at = time.monotonic()

    def _read(self):
        if not os.path.exists(self.path):
            log.warning(f"proxy file not found: {self.path} (direct connection)")
            return []
        with open(self.path, encoding="utf-8") as f:
            return [l.split("#", 1)[0].strip() for l in f if l.split("#", 1)[0].strip()]

    def get(self, url):
        if self.reload > 0 and time.monotonic() - self._loaded_at > self.reload:
            fresh = self._read()
            if fresh:
                self._set(fresh)
            self._loaded_at = time.monotonic()
        return super().get(url)

    def describe(self):
        return f"file {self.path} ({len(self.proxies)} proxies, {self.strategy})"


def load_proxy(spec):
    """Build a ProxyProvider from a --proxy spec string (see module docstring)."""
    if spec is None or str(spec).strip().lower() in ("", "none", "off", "direct"):
        return NoProxy()
    spec = str(spec).strip()
    target, _, query = spec.partition("?")
    kwargs = dict(parse_qsl(query))
    if target.startswith("file:"):
        return FileProxyPool(target[5:], **kwargs)
    if "://" in target or re.fullmatch(r"(?:[^@/\s]+@)?[\w.-]+:\d{2,5}", target):
        return SingleProxy(target)   # http://h:p, socks5://h:p, or bare host:port / user:pw@host:port
    if ":" in target:
        return _load_custom(target, kwargs)
    raise ValueError(f"unknown --proxy spec: {spec}")


def _load_custom(target, kwargs):
    where, _, cls_name = target.rpartition(":")
    if where.endswith(".py") or os.path.sep in where or "/" in where:
        mod_spec = importlib.util.spec_from_file_location("eka_custom_proxy", where)
        if mod_spec is None:
            raise ValueError(f"cannot load proxy module {where}")
        module = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(module)
    else:
        module = importlib.import_module(where)
    cls = getattr(module, cls_name)
    provider = cls(**kwargs)
    if not isinstance(provider, ProxyProvider):
        raise TypeError(f"{cls_name} must subclass eka.core.proxy.ProxyProvider")
    return provider


def _with_scheme(p):
    p = p.strip()
    return p if "://" in p else "http://" + p


def _mask(p):
    """Hide credentials in logs."""
    if "@" in p:
        scheme, _, rest = p.partition("://")
        return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
    return p
