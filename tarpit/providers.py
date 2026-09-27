"""Vorlagen für KI-Anbieter. Alle werden über die OpenAI-kompatible Schnittstelle angesprochen."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Preset:
    kind: str
    name: str
    base_url: str
    needs_key: bool
    hint: str
    example_models: tuple[str, ...] = ()


PRESETS: dict[str, Preset] = {
    p.kind: p
    for p in [
        Preset("openrouter", "OpenRouter", "https://openrouter.ai/api/v1", True,
               "Hunderte Modelle über einen Schlüssel, meldet die echten Kosten.",
               ("openai/gpt-4o-mini", "google/gemini-2.5-flash", "meta-llama/llama-3.3-70b-instruct")),
        Preset("openai", "OpenAI", "https://api.openai.com/v1", True,
               "Direkt bei OpenAI. Auch für Whisper-Spracherkennung (Modell whisper-1).",
               ("gpt-4o-mini", "whisper-1")),
        Preset("google", "Google Gemini", "https://generativelanguage.googleapis.com/v1beta/openai", True,
               "Gemini über Googles OpenAI-kompatiblen Endpunkt. Schlüssel aus Google AI Studio.",
               ("gemini-2.5-flash",)),
        Preset("groq", "Groq", "https://api.groq.com/openai/v1", True,
               "Sehr schnell, gut für Spracherkennung (Whisper) und kleine Modelle.",
               ("whisper-large-v3",)),
        Preset("ollama", "Ollama (lokal)", "http://host.docker.internal:11434/v1", False,
               "Lokale Modelle, kostenlos. Ohne Docker: http://localhost:11434/v1. "
               "Modell vorher laden, z. B. ollama pull gemma3:4b",
               ("gemma3:4b", "llama3.2:3b", "qwen2.5:3b")),
        Preset("lmstudio", "LM Studio (lokal)", "http://host.docker.internal:1234/v1", False,
               "Lokale Modelle mit LM Studio. Ohne Docker: http://localhost:1234/v1.", ()),
        Preset("custom", "Eigene (OpenAI-kompatibel)", "", False,
               "Jede API mit /chat/completions, z. B. vLLM, llama.cpp-Server, LocalAI.", ()),
    ]
}

LOCAL_KINDS = {"ollama", "lmstudio"}


def is_openrouter(base_url: str) -> bool:
    return "openrouter.ai" in base_url


def mask_key(key: str) -> str:
    if not key:
        return "–"
    return "••••" + key[-4:] if len(key) > 8 else "••••"
