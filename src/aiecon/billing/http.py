"""Bounded HTTP access for provider report APIs (PLAN.md §7.4).

30 second timeout, at most three retries on 429/5xx/transport errors honouring
``Retry-After``, and immediate failure on authentication errors. Response bodies are never
logged; errors carry the status code and the request description only.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

RETRY_STATUSES = {429, 500, 502, 503, 504}
AUTH_STATUSES = {401, 403}


class ProviderReportError(Exception):
    """A provider report request failed for good (after retries or on auth errors)."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    if response is not None:
        header = response.headers.get("retry-after")
        if header:
            try:
                return max(0.0, float(header))
            except ValueError:
                pass
    return float(2**attempt)


def fetch_json(
    client: httpx.Client,
    url: str,
    *,
    params: Mapping[str, Any] | list[tuple[str, Any]] | None,
    headers: Mapping[str, str],
    max_retries: int = 3,
    sleeper: Callable[[float], None] = time.sleep,
    describe: str = "",
) -> dict[str, Any]:
    attempt = 0
    while True:
        response: httpx.Response | None = None
        try:
            response = client.get(url, params=params, headers=dict(headers))
        except httpx.TransportError as exc:
            if attempt >= max_retries:
                raise ProviderReportError(
                    f"{describe or url}: transport error {type(exc).__name__} "
                    f"after {attempt} retries"
                ) from None
            sleeper(_retry_delay(None, attempt))
            attempt += 1
            continue
        if response.status_code in AUTH_STATUSES:
            raise ProviderReportError(
                f"{describe or url}: authentication failed (HTTP {response.status_code}); "
                "check the admin key and its permissions",
                status_code=response.status_code,
            )
        if response.status_code in RETRY_STATUSES:
            if attempt >= max_retries:
                raise ProviderReportError(
                    f"{describe or url}: HTTP {response.status_code} after {attempt} retries",
                    status_code=response.status_code,
                )
            sleeper(_retry_delay(response, attempt))
            attempt += 1
            continue
        if response.status_code >= 400:
            raise ProviderReportError(
                f"{describe or url}: HTTP {response.status_code}", status_code=response.status_code
            )
        try:
            data = response.json()
        except ValueError:
            raise ProviderReportError(f"{describe or url}: response is not JSON") from None
        if not isinstance(data, dict):
            raise ProviderReportError(f"{describe or url}: unexpected response shape")
        return data
