"""Low-level Battle.net API client.

Defensive by design: the Forever endpoints are undocumented and change, so `get()`
returns a response object for any HTTP status instead of raising on 403/404.
Only transport errors and exhausted retries raise.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import httpx

OAUTH_URL = "https://oauth.battle.net/token"
RETRY_STATUSES = {429, 500, 502, 503, 504}


class BlizzardAuthError(RuntimeError):
    pass


@dataclass
class ApiResponse:
    url: str
    status: int
    headers: httpx.Headers
    content: bytes
    elapsed_s: float
    _json: Any = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    def json(self) -> Any:
        if self._json is None and self.content:
            import json

            self._json = json.loads(self.content)
        return self._json


class BlizzardClient:
    def __init__(
        self,
        region: str,
        client_id: str,
        client_secret: str,
        *,
        locale: str = "en_US",
        http: httpx.Client | None = None,
        max_retries: int = 3,
        backoff_s: float = 1.0,
    ) -> None:
        self.region = region
        self.locale = locale
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = http or httpx.Client(timeout=60.0, headers={"Accept-Encoding": "gzip"})
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._token: str | None = None
        self._token_expires_at = 0.0

    @property
    def base_url(self) -> str:
        return f"https://{self.region}.api.blizzard.com"

    # -- auth -----------------------------------------------------------------

    def _ensure_token(self) -> str:
        # Refresh a minute early to avoid using a token that expires mid-request.
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        resp = self._http.post(
            OAUTH_URL,
            data={"grant_type": "client_credentials"},
            auth=(self._client_id, self._client_secret),
        )
        if resp.status_code != 200:
            raise BlizzardAuthError(
                f"Token request failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        payload = resp.json()
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    # -- requests ---------------------------------------------------------------

    def get(
        self,
        path: str,
        namespace: str,
        *,
        params: dict[str, Any] | None = None,
        if_modified_since: str | None = None,
    ) -> ApiResponse:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        query = {"namespace": namespace, "locale": self.locale, **(params or {})}
        headers = {"Authorization": f"Bearer {self._ensure_token()}"}
        if if_modified_since:
            headers["If-Modified-Since"] = if_modified_since

        attempt = 0
        while True:
            start = time.monotonic()
            resp = self._http.get(url, params=query, headers=headers)
            elapsed = time.monotonic() - start
            if resp.status_code == 401 and attempt == 0:
                # Token revoked/expired early: refresh once.
                self._token = None
                headers["Authorization"] = f"Bearer {self._ensure_token()}"
                attempt += 1
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                retry_after = resp.headers.get("retry-after")
                delay = float(retry_after) if retry_after else self._backoff_s * 2**attempt
                time.sleep(delay)
                attempt += 1
                continue
            return ApiResponse(
                url=str(resp.request.url),
                status=resp.status_code,
                headers=resp.headers,
                content=resp.content,
                elapsed_s=elapsed,
            )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> BlizzardClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
