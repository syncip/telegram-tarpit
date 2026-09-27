"""Minimaler Client für OpenAI-kompatible Chat-APIs (OpenRouter, Ollama, ...)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

import httpx


class LLMError(RuntimeError):
    pass


@dataclass
class LLMStats:
    """Laufzeit-Statistik für die Statusanzeige (nur im Speicher)."""

    day: str = field(default_factory=lambda: date.today().isoformat())
    calls_today: int = 0
    errors_today: int = 0
    tokens_today: int = 0
    prompt_tokens_today: int = 0
    cached_tokens_today: int = 0
    completion_tokens_today: int = 0
    cost_today: float = 0.0
    last_ok_at: float | None = None
    last_error_at: float | None = None
    last_error: str | None = None
    last_latency_ms: int | None = None
    last_model: str | None = None

    def _roll(self) -> None:
        today = date.today().isoformat()
        if today != self.day:
            self.day = today
            self.calls_today = self.errors_today = self.tokens_today = 0
            self.prompt_tokens_today = self.cached_tokens_today = self.completion_tokens_today = 0
            self.cost_today = 0.0

    def ok(self, model: str, latency_ms: int, usage: "Usage") -> None:
        self._roll()
        self.calls_today += 1
        self.last_ok_at = time.time()
        self.last_latency_ms = latency_ms
        self.last_model = model
        self.tokens_today += usage.prompt + usage.completion
        self.prompt_tokens_today += usage.prompt
        self.cached_tokens_today += usage.cached
        self.completion_tokens_today += usage.completion
        self.cost_today += usage.cost

    @property
    def cache_rate(self) -> float | None:
        return self.cached_tokens_today / self.prompt_tokens_today if self.prompt_tokens_today else None

    def error(self, message: str) -> None:
        self._roll()
        self.calls_today += 1
        self.errors_today += 1
        self.last_error_at = time.time()
        self.last_error = message

    @property
    def healthy(self) -> bool | None:
        """None = noch nicht benutzt, sonst: war der letzte Aufruf erfolgreich?"""
        if self.last_ok_at is None and self.last_error_at is None:
            return None
        return (self.last_ok_at or 0) >= (self.last_error_at or 0)


@dataclass
class Usage:
    prompt: int = 0
    cached: int = 0
    completion: int = 0
    cost: float = 0.0

    @classmethod
    def from_response(cls, usage: dict | None) -> "Usage":
        usage = usage or {}
        details = usage.get("prompt_tokens_details") or {}
        return cls(
            prompt=int(usage.get("prompt_tokens") or 0),
            cached=int(details.get("cached_tokens") or 0),
            completion=int(usage.get("completion_tokens") or 0),
            cost=float(usage.get("cost") or 0.0),
        )

    def describe(self) -> str:
        text = f"{self.prompt} Token Eingabe"
        if self.cached:
            text += f" (davon {self.cached} aus dem Cache)"
        text += f", {self.completion} Token Ausgabe"
        if self.cost:
            text += f", ${self.cost:.5f}"
        return text


@dataclass
class ChatResult:
    text: str
    usage: Usage
    latency_ms: int


class LLMClient:
    def __init__(self, base_url: str, api_key: str):
        headers = {
            # von OpenRouter empfohlen, damit die App im Dashboard erkennbar ist
            "HTTP-Referer": "https://github.com/syncip/telegram-tarpit",
            "X-Title": "telegram-tarpit",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self.base_url = base_url
        self.stats = LLMStats()
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=120)

    async def chat(
        self, model: str, messages: list[dict[str, str]], temperature: float,
        max_tokens: int | None = None,
    ) -> str:
        return (await self.complete(model, messages, temperature, max_tokens)).text

    async def complete(
        self, model: str, messages: list[dict[str, str]], temperature: float,
        max_tokens: int | None = None,
    ) -> ChatResult:
        payload: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            # OpenRouter liefert damit die Kosten in "usage" mit; andere APIs ignorieren es
            "usage": {"include": True},
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        started = time.monotonic()
        try:
            response = await self._http.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            message = f"LLM-API nicht erreichbar ({self.base_url}): {type(exc).__name__} {exc}"
            self.stats.error(message)
            raise LLMError(message) from exc
        if response.status_code >= 400:
            message = f"LLM-API antwortet mit {response.status_code}: {response.text[:300]}"
            self.stats.error(message)
            raise LLMError(message)
        try:
            data = response.json()
            if "error" in data and not data.get("choices"):
                raise KeyError(data["error"])
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            message = f"Unerwartete Antwort der LLM-API: {response.text[:300]}"
            self.stats.error(message)
            raise LLMError(message) from exc
        latency_ms = int((time.monotonic() - started) * 1000)
        usage = Usage.from_response(data.get("usage"))
        self.stats.ok(model, latency_ms, usage)
        return ChatResult(text=content or "", usage=usage, latency_ms=latency_ms)

    async def aclose(self) -> None:
        await self._http.aclose()
