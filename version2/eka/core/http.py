"""One HTTP client for every pipeline: sessions, retries, proxies, size caps.

    client = HttpClient.from_args(args)          # after add_http_args(parser)
    with client.get(url) as r: ...                # streamed response
    body = client.read_capped(r, max_bytes)       # never more than max_bytes in RAM
    client.download(url, path, max_bytes=...)     # stream straight to disk
"""
import logging
import os
import threading
import time

import requests

from .proxy import NoProxy, load_proxy

log = logging.getLogger("eka.http")

DEFAULT_UA = "EkaCurator/2.0"
RETRY_STATUS = {429, 500, 502, 503, 504}


class TooLarge(Exception):
    pass


class HttpClient:
    def __init__(self, user_agent=DEFAULT_UA, timeout=30.0, verify_ssl=True, retries=1,
                 proxy=None, headers=None, backoff=2.0):
        self.user_agent = user_agent
        self.timeout = float(timeout)
        self.verify_ssl = verify_ssl
        self.retries = int(retries)
        self.proxy = proxy or NoProxy()
        self.headers = dict(headers or {})
        self.backoff = backoff
        self._local = threading.local()
        if not verify_ssl:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    @classmethod
    def from_args(cls, args, **overrides):
        kw = dict(user_agent=args.user_agent, timeout=args.timeout, verify_ssl=args.verify_ssl,
                  retries=args.retries, proxy=load_proxy(args.proxy))
        kw.update(overrides)
        return cls(**kw)

    @property
    def session(self):
        """One requests.Session per thread (Session is not guaranteed thread-safe)."""
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": self.user_agent, **self.headers})
            s.verify = self.verify_ssl
            s.max_redirects = 10
            self._local.session = s
        return s

    def close(self):
        s = getattr(self._local, "session", None)
        if s is not None:
            s.close()
            self._local.session = None

    def get(self, url, stop=None, **kw):
        """Streamed GET with retries on connection errors, timeouts, 429 and 5xx.
        Use as a context manager. Raises requests exceptions after the last try."""
        kw.setdefault("timeout", (self.timeout, self.timeout))
        kw.setdefault("stream", True)
        kw.setdefault("allow_redirects", True)
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            proxy, proxies = self.proxy.requests_proxies(url)
            try:
                r = self.session.get(url, proxies=proxies, **kw)
            except requests.exceptions.SSLError:
                raise
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                if proxy:
                    self.proxy.report_failure(proxy, url, e)
                if attempt == attempts or _stopped(stop):
                    raise
                _sleep(self.backoff * attempt, stop)
                continue
            if proxy:
                if r.status_code == 407:
                    self.proxy.report_failure(proxy, url, "407")
                else:
                    self.proxy.report_success(proxy, url)
            if r.status_code in RETRY_STATUS and attempt < attempts and not _stopped(stop):
                wait = _retry_after(r) or self.backoff * attempt
                r.close()
                _sleep(min(wait, 60), stop)
                continue
            return r
        raise requests.exceptions.RetryError(f"retries exhausted for {url}")

    @staticmethod
    def read_capped(r, max_bytes, deadline_s=None, stop=None):
        """Read the body, raising TooLarge past max_bytes or past deadline_s seconds."""
        length = r.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise TooLarge(f"Content-Length {length}")
        deadline = time.monotonic() + deadline_s if deadline_s else None
        buf = bytearray()
        for chunk in r.iter_content(64 * 1024):
            buf += chunk
            if len(buf) > max_bytes:
                raise TooLarge(f"more than {max_bytes} bytes")
            if deadline and time.monotonic() > deadline:
                raise TooLarge("too slow")
            if _stopped(stop):
                raise TooLarge("stopped")
        return bytes(buf)

    def download(self, url, path, max_bytes=None, content_types=None, magic=None,
                 referer=None, stop=None):
        """Stream url to path (via path + '.part', renamed at the end).

        Returns (ok, info): info is bytes written, or the reason it was skipped.
        content_types: accept only if Content-Type contains one of these, OR the
        body starts with `magic` (servers often send PDFs as octet-stream).
        """
        headers = {"Referer": referer} if referer else {}
        tmp = f"{path}.{os.getpid()}-{threading.get_ident()}.part"  # unique per download
        try:
            with self.get(url, headers=headers, stop=stop) as r:
                if r.status_code != 200:
                    return False, f"http_{r.status_code}"
                length = r.headers.get("Content-Length")
                if max_bytes and length and length.isdigit() and int(length) > max_bytes:
                    return False, f"too_large ({length} bytes)"
                ctype = r.headers.get("Content-Type", "").lower()
                type_ok = not content_types or any(t in ctype for t in content_types)
                written = 0
                first = b""
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(256 * 1024):
                        if not chunk:
                            continue
                        if not first:
                            first = chunk[:16]
                            if not type_ok and not (magic and first.startswith(magic)):
                                break
                        f.write(chunk)
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            break
                        if _stopped(stop):
                            break
                if not type_ok and not (magic and first.startswith(magic)):
                    _remove(tmp)
                    return False, f"wrong_type ({ctype or 'none'})"
                if max_bytes and written > max_bytes:
                    _remove(tmp)
                    return False, "too_large"
                if _stopped(stop):
                    _remove(tmp)
                    return False, "stopped"
                if written == 0:
                    _remove(tmp)
                    return False, "empty"
            os.replace(tmp, path)
            return True, written
        except requests.exceptions.SSLError:
            _remove(tmp)
            return False, "ssl_error"
        except requests.exceptions.RequestException as e:
            _remove(tmp)
            return False, type(e).__name__
        except OSError as e:
            _remove(tmp)
            return False, f"os_error: {e}"


def add_http_args(parser, timeout=30.0, retries=1, user_agent=DEFAULT_UA):
    g = parser.add_argument_group("HTTP")
    g.add_argument("--user-agent", default=user_agent, help="User-Agent header.")
    g.add_argument("--timeout", type=float, default=timeout, help="Seconds for connect and each read.")
    g.add_argument("--retries", type=int, default=retries, help="Extra tries on timeout/429/5xx.")
    g.add_argument("--verify-ssl", default=True, action=_bool_action(),
                   help="Check HTTPS certificates (--no-verify-ssl for broken gov sites).")
    g.add_argument("--proxy", default="none",
                   help="none | http://host:port | file:proxies.txt | path.py:Class  (see eka/core/proxy.py)")
    return g


def _bool_action():
    import argparse
    return argparse.BooleanOptionalAction


def _retry_after(r):
    v = r.headers.get("Retry-After", "")
    return float(v) if v.isdigit() else None


def _stopped(stop):
    return stop is not None and stop.is_set()


def _sleep(seconds, stop):
    if stop is not None:
        stop.wait(seconds)
    else:
        time.sleep(seconds)


def _remove(p):
    try:
        os.remove(p)
    except OSError:
        pass
