"""Minimal asynchronous Telegram Bot API client.

Raw JSON updates are intentional: reply_markup is passed through unchanged so
button layout, URL/callback fields and Bot API button style attributes are not
reconstructed or accidentally dropped by an outdated wrapper library.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class TelegramAPIError(RuntimeError):
    def __init__(
        self,
        method: str,
        error_code: int,
        description: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"Telegram API {method} failed ({error_code}): {description}")
        self.method = method
        self.error_code = error_code
        self.description = description
        self.retry_after = retry_after


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(70.0, connect=15.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        try:
            response = await self.client.post(f"{self.base_url}/{method}", json=payload or {})
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            try:
                body = exc.response.json()
            except ValueError:
                raise RuntimeError(f"Telegram HTTP error {exc.response.status_code}") from exc
            parameters = body.get("parameters") or {}
            raise TelegramAPIError(
                method,
                int(body.get("error_code", exc.response.status_code)),
                str(body.get("description", "Unknown Telegram API error")),
                _safe_int(parameters.get("retry_after")),
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Network/response error while calling Telegram {method}: {exc}") from exc

        if not body.get("ok"):
            parameters = body.get("parameters") or {}
            raise TelegramAPIError(
                method,
                int(body.get("error_code", 0)),
                str(body.get("description", "Unknown Telegram API error")),
                _safe_int(parameters.get("retry_after")),
            )
        return body.get("result")

    async def call_with_retry(
        self,
        method: str,
        payload: dict[str, Any],
        attempts: int = 5,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self.call(method, payload)
            except TelegramAPIError as exc:
                last_error = exc
                if exc.retry_after is not None:
                    await asyncio.sleep(max(1, exc.retry_after) + 1)
                    continue
                # Retry transient Telegram server failures, not invalid requests.
                if exc.error_code >= 500 and attempt < attempts - 1:
                    await asyncio.sleep(min(2 ** attempt, 15))
                    continue
                raise
            except RuntimeError as exc:
                last_error = exc
                if attempt < attempts - 1:
                    await asyncio.sleep(min(2 ** attempt, 15))
                    continue
                raise
        if last_error:
            raise last_error
        raise RuntimeError(f"Telegram {method} failed without an error detail")


def _safe_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
