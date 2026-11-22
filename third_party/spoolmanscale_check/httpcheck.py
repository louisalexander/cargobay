"""What both checkers share: a client that talks like the scale, and a report.

The client sends header auth and the scale's user agent, speaks at most TLS 1.2,
follows no redirects, asks for identity encoding and records every status and
time it saw, so a checker can judge them at the end. A 429 with a Retry-After in
seconds is waited out and asked once more. The report prints one line per check and counts MUST failures and
SHOULD warnings. Standard library only.
"""
from __future__ import annotations

import base64
import json
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from inventory_rules import retry_after_s

# ("must" | "should", message), the shape every rules module returns.
Finding = Tuple[str, str]

MAX_FINDINGS_SHOWN = 5
USER_AGENT = "ESP32HTTPClient"   # what the scale sends (common.md, Transport)
RETRY_AFTER_MAX_S = 30           # a longer Retry-After is cut, the run has to end


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hands a 3xx back as the answer: the scale never follows one."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes):
        self.status = status
        self.headers = {k.lower(): v for k, v in headers.items()}
        self.body = body

    @property
    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def __str__(self) -> str:
        text = self.body.decode("utf-8", "replace").strip()
        return f"HTTP {self.status} {text[:160]}"


def tls_context(insecure: bool) -> ssl.SSLContext:
    """TLS 1.2 at most: the scale has no TLS 1.3 (common.md, Transport)."""
    context = ssl._create_unverified_context() if insecure else ssl.create_default_context()
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    return context


class Client:
    """Talks like the scale: header auth, its user agent, TLS 1.2, no redirects."""

    def __init__(self, base: str, auth: Optional[Dict[str, str]], timeout: float, insecure: bool):
        self.base = base.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.insecure = insecure
        self.seen: List[Tuple[str, str, int]] = []   # (method, path, status)
        self.timed: List[Tuple[str, str, float]] = []   # (method, path, seconds)
        self.limited: List[str] = []   # what answered 429, and its Retry-After
        https = urllib.request.HTTPSHandler(context=tls_context(insecure))
        self.opener = urllib.request.build_opener(NoRedirect(), https)

    def with_secret(self, secret: str) -> "Client":
        """The same server, the same way of sending the secret, another secret."""
        auth = {"kind": self.auth["kind"], "secret": secret} if self.auth else None
        return Client(self.base, auth, self.timeout, self.insecure)

    def _add_auth(self, req: urllib.request.Request) -> None:
        if not self.auth:
            return
        kind, secret = self.auth["kind"], self.auth["secret"]
        if kind == "key":
            req.add_header("X-API-Key", secret)
        elif kind == "bearer":
            req.add_header("Authorization", "Bearer " + secret)
        elif kind == "basic":
            req.add_header("Authorization", "Basic " + base64.b64encode(secret.encode()).decode())

    def request(self, method: str, path: str, body: Any = None, with_auth: bool = True) -> Response:
        req = urllib.request.Request(self.base + path, method=method)
        req.add_header("Accept-Encoding", "identity")
        req.add_header("User-Agent", USER_AGENT)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        if with_auth:
            self._add_auth(req)
        response, seconds = self._timed_send(req, data)
        if response.status == 429:
            response, seconds = self._after_limit(method, path, req, data, response)
        self.seen.append((method, path, response.status))
        self.timed.append((method, path, seconds))
        return response

    def _timed_send(self, req: urllib.request.Request, data: Optional[bytes]) -> Tuple[Response, float]:
        started = time.monotonic()
        response = self._send(req, data)
        return response, time.monotonic() - started

    def _after_limit(self, method: str, path: str, req: urllib.request.Request, data: Optional[bytes],
                     limited: Response) -> Tuple[Response, float]:
        """Notes the 429 and, when it says how long, waits and asks once more."""
        header = limited.headers.get("retry-after")
        self.limited.append(f"{method} {path}: Retry-After {header or 'missing'}")
        wait = retry_after_s(header)
        if wait is None:
            return limited, 0.0
        time.sleep(min(wait, RETRY_AFTER_MAX_S))
        return self._timed_send(req, data)

    def _send(self, req: urllib.request.Request, data: Optional[bytes]) -> Response:
        try:
            with self.opener.open(req, data=data, timeout=self.timeout) as resp:
                return Response(resp.status, dict(resp.headers), resp.read())
        except urllib.error.HTTPError as err:
            return Response(err.code, dict(err.headers), err.read())
        except (urllib.error.URLError, OSError, ValueError) as err:
            return Response(0, {}, str(err).encode("utf-8"))

    def get(self, path: str, with_auth: bool = True) -> Response:
        return self.request("GET", path, None, with_auth)

    def redirects_seen(self) -> List[str]:
        return [f"{m} {p} -> {s}" for m, p, s in self.seen if 300 <= s < 400]


class Report:
    def __init__(self, verbose: bool):
        self.verbose = verbose
        self.failed = 0
        self.warned = 0

    def _line(self, mark: str, number: Any, title: str, detail: Any) -> None:
        print(f"{mark:<5}{str(number):>3}  {title}")
        if detail and (mark != "ok" or self.verbose):
            for line in str(detail).splitlines():
                print(f"          {line}")

    def must(self, number: Any, title: str, ok: bool, detail: Any = "") -> None:
        if not ok:
            self.failed += 1
        self._line("ok" if ok else "FAIL", number, title, detail)

    def should(self, number: Any, title: str, ok: bool, detail: Any = "") -> None:
        if not ok:
            self.warned += 1
        self._line("ok" if ok else "warn", number, title, detail)

    def skip(self, number: Any, title: str, why: str) -> None:
        self._line("skip", number, title, why)

    def note(self, number: Any, title: str, detail: Any = "") -> None:
        """Worth knowing, counts for nothing: a planned rule, a side of a rule."""
        self._line("note", number, title, detail)

    def findings(self, number: Any, title: str, found: List[Finding]) -> None:
        musts = sorted({m for level, m in found if level == "must"})
        shoulds = sorted({m for level, m in found if level == "should"})
        detail = "\n".join((musts + shoulds)[:MAX_FINDINGS_SHOWN])
        if musts:
            self.must(number, title, False, detail)
        elif shoulds:
            self.should(number, title, False, detail)
        else:
            self.must(number, title, True)

    def summary(self) -> str:
        return f"\n{self.failed} MUST failed, {self.warned} SHOULD warnings"


def auth_from(args: Any) -> Optional[Dict[str, str]]:
    """The one credential given on the command line, or None."""
    for kind in ("key", "bearer", "basic"):
        secret = getattr(args, kind, None)
        if secret:
            return {"kind": kind, "secret": secret}
    return None
