"""Bilder und Sprachnachrichten: Upload-Aufbereitung, Bild-/Spracherkennung, Bild-Marker.

Die Persona kann Fotos aus ihrer Sammlung schicken. Die KI schreibt dafür
``[BILD:<nr>]`` in ihre Antwort; die Engine ersetzt das durch das echte Foto.
"""

from __future__ import annotations

import base64
import io
import re

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_IMAGE_SIDE = 1600
IMAGE_MARKER_RE = re.compile(r"\[\s*BILD\s*:\s*(\d+)\s*\]", re.IGNORECASE)
PHOTO_REQUEST_RE = re.compile(
    r"\b(bild|bilder|foto|fotos|selfie|pic|pics|picture|pictures|photo|photos|zeig dich|send me a pic)\b",
    re.IGNORECASE,
)

VISION_PROMPT = (
    "Beschreibe dieses Bild kurz und sachlich auf Deutsch (1-3 Sätze). Gib sichtbare Texte wörtlich "
    "wieder, z. B. bei Screenshots von Apps, Chats oder Kontoständen. Keine Einleitung."
)
PERSONA_IMAGE_PROMPT = (
    "Beschreibe dieses Foto in einem kurzen Satz (höchstens 12 Wörter) auf Deutsch, so dass man "
    "entscheiden kann, wann es in einem Chat passt. Keine Einleitung."
)
STT_PROMPT = "Transkribiere diese Sprachnachricht wörtlich in ihrer Originalsprache. Gib nur den Text aus."


def prepare_upload(data: bytes) -> bytes:
    """Prüft ein hochgeladenes Bild und speichert es neu als JPEG.

    Das Neu-Speichern entfernt alle Metadaten (EXIF, inkl. GPS-Position) und
    verkleinert sehr große Fotos.
    """
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("Bild ist größer als 15 MB")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)  # Drehung übernehmen, bevor EXIF wegfällt
            img = img.convert("RGB")
            img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
            out = io.BytesIO()
            img.save(out, "JPEG", quality=85, optimize=True)
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("Keine gültige Bilddatei (erlaubt: JPEG, PNG, WebP, …)") from exc
    return out.getvalue()


def data_url(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def build_vision_messages(image: bytes, prompt: str = VISION_PROMPT, mime: str = "image/jpeg") -> list[dict]:
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url(image, mime)}},
        ],
    }]


def build_stt_messages(audio: bytes, audio_format: str = "ogg") -> list[dict]:
    """Spracherkennung über die Chat-API (Modelle mit Audio-Eingang)."""
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": STT_PROMPT},
            {"type": "input_audio", "input_audio": {"data": base64.b64encode(audio).decode(), "format": audio_format}},
        ],
    }]


def image_marker_ids(text: str) -> list[int]:
    return [int(m) for m in IMAGE_MARKER_RE.findall(text or "")]


def strip_image_markers(text: str) -> str:
    return re.sub(r"[ \t]{2,}", " ", IMAGE_MARKER_RE.sub("", text or "")).strip()


def asks_for_photo(text: str) -> bool:
    return bool(PHOTO_REQUEST_RE.search(text or ""))
