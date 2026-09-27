"""Minimaler Client für OpenAI-kompatible Chat-APIs (OpenRouter, Ollama, ...)."""

from __future__ import annotations

import httpx


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, base_url: str, api_key: str):
        headers = {
            # von OpenRouter empfohlen, damit die App im Dashboard erkennbar ist
            "HTTP-Referer": "https://github.com/syncip/telegram-tarpit",
            "X-Title": "telegram-tarpit",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._http = httpx.AsyncClient(base_url=base_url, headers=headers, timeout=120)

    async def chat(self, model: str, messages: list[dict[str, str]], temperature: float) -> str:
        try:
            response = await self._http.post(
                "/chat/completions",
                json={"model": model, "messages": messages, "temperature": temperature},
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"Verbindung zur LLM-API fehlgeschlagen: {exc}") from exc
        if response.status_code >= 400:
            raise LLMError(f"LLM-API antwortet mit {response.status_code}: {response.text[:300]}")
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError(f"Unerwartete Antwort der LLM-API: {response.text[:300]}") from exc
        return content or ""

    async def aclose(self) -> None:
        await self._http.aclose()
