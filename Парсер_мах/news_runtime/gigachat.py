"""Bounded, thread-safe GigaChat requests without logging credentials or news."""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

import requests


class GigaChatError(Exception):
    """A safe, human-readable error which can be shown without HTTP bodies."""


class GigaChatAuthError(GigaChatError):
    """Authentication is unavailable; restart the client after correcting it."""


class GigaChatClient:
    """Reuse access tokens and serialize the entire personal-API request chain.

    Each HTTP operation has at most three transient attempts. A completion may
    refresh its rejected bearer once and replay once. Permanent authentication
    failures latch until a new client is constructed, including across threads.
    """

    def __init__(
        self,
        credentials: str,
        *,
        model: str = "GigaChat",
        scope: str = "GIGACHAT_API_PERS",
        verify: bool | str = True,
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
        base_url: str = "https://gigachat.devices.sberbank.ru/api/v1",
        auth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
    ) -> None:
        if not isinstance(credentials, str) or not credentials.strip():
            raise GigaChatAuthError("Не задан ключ авторизации GigaChat.")
        self._credentials = credentials.strip()
        self._model = model
        self._scope = scope
        self._verify = verify
        self._session = session if session is not None else requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._completion_url = base_url.rstrip("/") + "/chat/completions"
        self._auth_url = auth_url
        self._token: str | None = None
        self._refresh_at = 0.0
        self._auth_failure: str | None = None
        self._lock = threading.Lock()

    def _fail_auth(self, message: str) -> None:
        self._token = None
        self._refresh_at = 0.0
        self._auth_failure = message
        raise GigaChatAuthError(message) from None

    def _post(self, url: str, **kwargs: Any) -> Any:
        for attempt in range(3):
            try:
                response = self._session.post(
                    url, timeout=(10, 60), verify=self._verify,
                    allow_redirects=False, **kwargs,
                )
            except requests.exceptions.SSLError:
                raise GigaChatError(
                    "Не удалось проверить TLS-сертификат GigaChat. Проверьте файл CA."
                ) from None
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise GigaChatError(
                        "GigaChat недоступен после 3 попыток соединения."
                    ) from None
            except requests.RequestException:
                raise GigaChatError("Не удалось отправить запрос GigaChat.") from None
            else:
                if response.status_code != 429 and not 500 <= response.status_code < 600:
                    return response
                if attempt == 2:
                    raise GigaChatError(
                        f"GigaChat временно недоступен (HTTP {response.status_code}, 3 попытки)."
                    )
            self._sleep(2 ** attempt)
        raise AssertionError("Unreachable retry state")

    def _refresh_token(self) -> None:
        response = self._post(
            self._auth_url,
            headers={
                "Authorization": f"Basic {self._credentials}",
                "RqUID": str(uuid.uuid4()),
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"scope": self._scope},
        )
        if response.status_code != 200:
            self._fail_auth(
                f"GigaChat отклонил авторизацию (HTTP {response.status_code}). "
                "Проверьте ключ и scope, затем перезапустите обработку."
            )
        try:
            data = response.json()
            token_value = data["access_token"]
            expires_at = float(data["expires_at"])
            # OAuth returns an absolute Unix timestamp, never a lifetime.
            if expires_at >= 100_000_000_000:
                expires_at /= 1000
            if (not isinstance(token_value, str) or not token_value.strip()
                    or not math.isfinite(expires_at) or expires_at <= self._clock()):
                raise ValueError("Invalid token metadata")
        except (ValueError, TypeError, KeyError, OverflowError):
            self._fail_auth(
                "GigaChat вернул некорректный токен авторизации. Перезапустите обработку позже."
            )
        self._token = token_value
        self._refresh_at = expires_at - 60

    def complete_json(self, system_prompt: str, user_message: str) -> dict:
        with self._lock:
            if self._auth_failure is not None:
                raise GigaChatAuthError(self._auth_failure)
            if self._token is None or self._clock() >= self._refresh_at:
                self._refresh_token()
            payload = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.1,
                "stream": False,
            }
            for replay in range(2):
                response = self._post(
                    self._completion_url,
                    headers={"Authorization": f"Bearer {self._token}",
                             "Content-Type": "application/json"},
                    json=payload,
                )
                if response.status_code == 401:
                    if replay == 1:
                        self._fail_auth(
                            "GigaChat отклонил новый токен (HTTP 401). "
                            "Проверьте ключ и scope, затем перезапустите обработку."
                        )
                    self._token = None
                    self._refresh_token()
                    continue
                if response.status_code == 403:
                    self._fail_auth(
                        "GigaChat запретил доступ (HTTP 403). Проверьте права ключа и scope."
                    )
                if response.status_code != 200:
                    raise GigaChatError(f"GigaChat отклонил запрос (HTTP {response.status_code}).")
                return self._parse_completion(response)
            raise AssertionError("Unreachable authorization replay state")

    @staticmethod
    def _parse_completion(response: Any) -> dict:
        try:
            data = response.json()
            choice = data["choices"][0]
            reason = choice["finish_reason"]
            content = choice["message"]["content"]
        except (ValueError, TypeError, KeyError, IndexError):
            raise GigaChatError("GigaChat вернул некорректную структуру ответа.") from None
        if reason != "stop":
            if reason == "blacklist":
                raise GigaChatError("GigaChat отказался обработать материал.")
            if reason == "length":
                raise GigaChatError("GigaChat обрезал ответ по лимиту длины.")
            raise GigaChatError("GigaChat не завершил текстовый ответ.")
        if not isinstance(content, str) or not content.strip():
            raise GigaChatError("GigaChat вернул пустой ответ.")
        content = content.strip()
        if content.startswith("```") and content.endswith("```"):
            lines = content.splitlines()
            if len(lines) >= 3 and lines[0].strip().lower() in ("```", "```json"):
                content = "\n".join(lines[1:-1])
        try:
            result = json.loads(content)
        except (ValueError, TypeError):
            raise GigaChatError("GigaChat вернул ответ, который не является JSON.") from None
        if not isinstance(result, dict):
            raise GigaChatError("GigaChat вернул JSON без объекта результата.")
        return result
