"""Which repository an application name means.

People name applications, not clone URLs. `sample_app` is what is
written in a ticket, said in a handover and typed into a form; the URL is a
detail of where that application's code happens to live, and looking it up by
hand is a browser tab and a copy-paste per repository -- worse for a change
that spans several.

An application catalog already knows the mapping, so this asks it:

    GET /webapi/service/fetch_detail?code=<application>
    -> {"status": 0, "data": {"git_path": "git@git...:group/sample-app.git", ...}}

The browser authenticates with a session cookie, which a service running
unattended does not have. An access key is presented as a bearer token
instead: connect to the catalog, keep the target host in the `Host` header
and in TLS SNI. Set `DFA_CATALOG_BASE_URL` to the catalog. An optional
`DFA_SSO_RESOLVE_IP` sends that connection to a fronting address while the
TLS name stays the catalog host.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

#: The gateway that accepts an access key in place of a session.
DEFAULT_RESOLVE_IP = ""
DEFAULT_BASE_URL = "https://catalog.example.com"

#: A transport is (method, url, body, headers) -> (status, body), so a test can
#: answer without a socket.
Transport = Callable[[str, str, Optional[bytes], Dict[str, str]], Tuple[int, bytes]]


class LookupFailed(RuntimeError):
    """The application could not be resolved to a repository."""


class _GatewayConnection(http.client.HTTPSConnection):
    """Reach the gateway's address while still speaking to the target host."""

    def __init__(self, host: str, *, resolve_ip: str, **kwargs: Any):
        super().__init__(host, **kwargs)
        self.resolve_ip = resolve_ip

    def connect(self) -> None:
        sock = socket.create_connection(
            (self.resolve_ip or self.host, self.port), self.timeout, self.source_address
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


@dataclass(frozen=True)
class Service:
    """The parts of an application record this pipeline uses."""

    code: str
    git_url: str
    description: str = ""
    app_type: str = ""


class Chongxiao:
    """Application lookups, by access key rather than by session cookie."""

    def __init__(
        self,
        *,
        access_token: str,
        base_url: str = DEFAULT_BASE_URL,
        resolve_ip: str = DEFAULT_RESOLVE_IP,
        timeout: int = 15,
        transport: Optional[Transport] = None,
    ):
        self.access_token = (access_token or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.resolve_ip = (resolve_ip or DEFAULT_RESOLVE_IP).strip()
        self.timeout = timeout
        self.transport = transport

    def service(self, code: str) -> Service:
        """The application record for ``code``."""
        name = (code or "").strip()
        if not name:
            raise LookupFailed("no application name given")
        if not self.access_token:
            raise LookupFailed(
                "no access key configured: set DFA_SSO_ACCESS_TOKEN to look up "
                "applications by name"
            )
        query = urllib.parse.urlencode({"code": name})
        status, body = self._get(f"/webapi/service/fetch_detail?{query}")
        if status in (401, 403):
            raise LookupFailed(f"the access key was refused for {name} (HTTP {status})")
        if status != 200:
            raise LookupFailed(f"catalog answered HTTP {status} for {name}")
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise LookupFailed(f"catalog did not answer with JSON for {name}") from exc
        if not isinstance(payload, dict) or payload.get("status") not in (0, "0"):
            message = (payload or {}).get("msg") if isinstance(payload, dict) else ""
            raise LookupFailed(f"catalog rejected {name}: {message or 'unknown error'}")
        data = payload.get("data")
        if not isinstance(data, dict) or not data.get("git_path"):
            raise LookupFailed(f"{name} has no repository recorded in catalog")
        return Service(
            code=str(data.get("code") or name),
            git_url=str(data["git_path"]),
            description=str(data.get("description") or ""),
            app_type=str(data.get("app_type") or ""),
        )

    def repo_url(self, code: str) -> str:
        """The clone URL for ``code``, in the HTTPS form a token can use."""
        from agent_core.git.identity import url_for_access_token

        return url_for_access_token(self.service(code).git_url)

    def _get(self, path: str) -> Tuple[int, bytes]:
        host = urllib.parse.urlsplit(self.base_url).hostname or ""
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.access_token}",
            # Both matter: the connection goes to the gateway's address, and
            # the gateway routes on the name.
            "Host": host,
            "User-Agent": "dev-flow-agent/1.0",
        }
        if self.transport is not None:
            return self.transport("GET", f"{self.base_url}{path}", None, headers)
        conn = _GatewayConnection(host, resolve_ip=self.resolve_ip, timeout=self.timeout)
        try:
            conn.request("GET", path, None, headers)
            response = conn.getresponse()
            return response.status, response.read()
        except OSError as exc:
            raise LookupFailed(f"could not reach catalog: {exc}") from exc
        finally:
            conn.close()


def looks_like_repo(text: str) -> bool:
    """Whether this is already a repository, rather than an application name.

    Application codes are `lower_snake` words; anything with a scheme, an SSH
    shape or a path separator is a repository someone typed out in full.
    """
    value = (text or "").strip()
    if not value:
        return False
    return "://" in value or value.startswith(("/", ".", "~")) or ":" in value or "/" in value
