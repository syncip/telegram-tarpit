"""Webinterface zur Steuerung (FastAPI + Jinja2, ohne JS-Framework)."""

from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Config
from .db import INT_SETTINGS, Database
from .engine import Tarpit

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "–"
    dt = datetime.fromtimestamp(ts)
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M")
    return dt.strftime("%d.%m. %H:%M")


def _fmt_duration(seconds: float | None) -> str:
    if not seconds or seconds <= 0:
        return "–"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


templates.env.filters["ts"] = _fmt_ts
templates.env.filters["duration"] = _fmt_duration


def create_app(config: Config) -> FastAPI:
    security = HTTPBasic()

    def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
        user_ok = secrets.compare_digest(credentials.username.encode(), config.web_user.encode())
        pw_ok = secrets.compare_digest(credentials.password.encode(), config.web_password.encode())
        if not (user_ok and pw_ok):
            raise HTTPException(401, "Falsche Zugangsdaten", headers={"WWW-Authenticate": "Basic"})

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(config.db_path)
        tarpit = Tarpit(config, db)
        await tarpit.start()
        app.state.db = db
        app.state.tarpit = tarpit
        try:
            yield
        finally:
            await tarpit.stop()
            db.close()

    app = FastAPI(lifespan=lifespan, dependencies=[Depends(require_auth)], docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.middleware("http")
    async def same_origin_posts(request: Request, call_next):
        # Basic-Auth-Zugangsdaten schickt der Browser automatisch mit. Damit
        # fremde Webseiten keine Formulare hierher absenden können, müssen
        # POST-Requests von derselben Origin kommen.
        if request.method == "POST":
            origin = request.headers.get("origin") or request.headers.get("referer")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return HTMLResponse("Cross-Origin-Request blockiert", status_code=403)
        return await call_next(request)

    def db(request: Request) -> Database:
        return request.app.state.db

    def tarpit(request: Request) -> Tarpit:
        return request.app.state.tarpit

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def safe_next(next_url: str, default: str = "/") -> str:
        return next_url if next_url.startswith("/") and not next_url.startswith("//") else default

    # --- Übersicht ---------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        d, t = db(request), tarpit(request)
        chats = d.chats_with_stats()
        totals = {
            "active": sum(1 for c in chats if c["enabled"]),
            "ai_msgs": sum(c["n_ai"] for c in chats),
            "baited": sum(c["n_them_baited"] for c in chats),
            "wasted": sum(
                max(0.0, c["last_them"] - c["first_ai"])
                for c in chats
                if c["first_ai"] and c["last_them"]
            ),
        }
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "chats": chats,
                "personas": d.personas(),
                "settings": d.settings(),
                "pending": t.pending,
                "totals": totals,
                "me": t.me,
            },
        )

    @app.post("/sync")
    async def sync(request: Request):
        await tarpit(request).sync_dialogs()
        return back("/")

    @app.post("/global")
    async def toggle_global(request: Request, enabled: str = Form("0")):
        d, t = db(request), tarpit(request)
        on = enabled == "1"
        d.set_setting("global_enabled", on)
        if on:
            t.reschedule_all()
        else:
            t.cancel_all()
        return back("/")

    # --- Einzelne Chats ----------------------------------------------------

    def get_chat_or_404(request: Request, chat_id: int):
        chat = db(request).chat(chat_id)
        if chat is None:
            raise HTTPException(404, "Chat nicht gefunden")
        return chat

    @app.get("/chats/{chat_id}", response_class=HTMLResponse)
    async def chat_page(request: Request, chat_id: int):
        d, t = db(request), tarpit(request)
        chat = get_chat_or_404(request, chat_id)
        if d.message_count(chat_id) == 0:
            try:
                await t.import_history(chat_id)
            except Exception:
                log.exception("Verlauf konnte nicht geladen werden")
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                "chat": chat,
                "messages": d.messages(chat_id),
                "personas": d.personas(),
                "persona": d.persona_for_chat(chat),
                "pending": t.pending.get(chat_id),
                "ai_today": d.ai_sent_today(chat),
                "settings": d.settings(),
                "now": time.time(),
            },
        )

    @app.get("/chats/{chat_id}/messages", response_class=HTMLResponse)
    async def chat_messages(request: Request, chat_id: int):
        get_chat_or_404(request, chat_id)
        return templates.TemplateResponse(
            request,
            "_messages.html",
            {
                "messages": db(request).messages(chat_id),
                "pending": tarpit(request).pending.get(chat_id),
            },
        )

    @app.post("/chats/{chat_id}/toggle")
    async def chat_toggle(
        request: Request, chat_id: int, field: str = Form(...), value: str = Form("0"),
        next: str = Form("/"),
    ):
        d, t = db(request), tarpit(request)
        get_chat_or_404(request, chat_id)
        if field not in ("enabled", "paused"):
            raise HTTPException(400, "Ungültiges Feld")
        on = value == "1"
        d.set_chat_flag(chat_id, field, on)
        active = d.chat(chat_id)["enabled"] and not d.chat(chat_id)["paused"]
        if active:
            if field == "enabled" and d.message_count(chat_id) < 5:
                await t.import_history(chat_id)
            t.maybe_schedule(chat_id)
        else:
            t.cancel(chat_id)
        return back(safe_next(next))

    @app.post("/chats/{chat_id}/persona")
    async def chat_persona(
        request: Request, chat_id: int, persona_id: str = Form(""), next: str = Form("/")
    ):
        get_chat_or_404(request, chat_id)
        db(request).set_chat_persona(chat_id, int(persona_id) if persona_id else None)
        return back(safe_next(next))

    @app.post("/chats/{chat_id}/reply-now")
    async def chat_reply_now(request: Request, chat_id: int):
        get_chat_or_404(request, chat_id)
        tarpit(request).reply_now(chat_id)
        return back(f"/chats/{chat_id}")

    @app.post("/chats/{chat_id}/cancel")
    async def chat_cancel(request: Request, chat_id: int):
        tarpit(request).cancel(chat_id)
        return back(f"/chats/{chat_id}")

    @app.post("/chats/{chat_id}/import")
    async def chat_import(request: Request, chat_id: int):
        get_chat_or_404(request, chat_id)
        await tarpit(request).import_history(chat_id)
        return back(f"/chats/{chat_id}")

    @app.post("/chats/{chat_id}/send")
    async def chat_send(request: Request, chat_id: int, text: str = Form(...)):
        get_chat_or_404(request, chat_id)
        if text.strip():
            await tarpit(request).send_manual(chat_id, text.strip())
        return back(f"/chats/{chat_id}")

    # --- Personas ----------------------------------------------------------

    @app.get("/personas", response_class=HTMLResponse)
    async def personas_page(request: Request, edit: int | None = None):
        d = db(request)
        return templates.TemplateResponse(
            request,
            "personas.html",
            {"personas": d.personas(), "editing": d.persona(edit) if edit else None},
        )

    @app.post("/personas")
    async def persona_save(
        request: Request, name: str = Form(...), prompt: str = Form(...), persona_id: str = Form("")
    ):
        if not name.strip() or not prompt.strip():
            raise HTTPException(400, "Name und Beschreibung dürfen nicht leer sein")
        db(request).save_persona(int(persona_id) if persona_id else None, name.strip(), prompt.strip())
        return back("/personas")

    @app.post("/personas/{persona_id}/delete")
    async def persona_delete(request: Request, persona_id: int):
        db(request).delete_persona(persona_id)
        return back("/personas")

    # --- Einstellungen -----------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: int = 0):
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"settings": db(request).settings(), "saved": saved, "llm_base_url": config.llm_base_url},
        )

    @app.post("/settings")
    async def settings_save(request: Request):
        d = db(request)
        form = await request.form()
        model = str(form.get("model", "")).strip()
        if model:
            d.set_setting("model", model)
        try:
            temperature = float(str(form.get("temperature", "0.9")).replace(",", "."))
            ints = {key: int(str(form.get(key, ""))) for key in INT_SETTINGS}
        except ValueError:
            raise HTTPException(400, "Bitte nur Zahlen eintragen")
        d.set_setting("temperature", min(max(temperature, 0.0), 2.0))
        if ints["min_delay"] > ints["max_delay"]:
            ints["min_delay"], ints["max_delay"] = ints["max_delay"], ints["min_delay"]
        ints["quiet_start"] %= 24
        ints["quiet_end"] %= 24
        ints["history_limit"] = max(4, ints["history_limit"])
        for key, value in ints.items():
            d.set_setting(key, max(0, value))
        return back("/settings?saved=1")

    return app
