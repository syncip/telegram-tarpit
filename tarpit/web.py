"""Webinterface zur Steuerung (FastAPI + Jinja2, ohne JS-Framework)."""

from __future__ import annotations

import logging
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlparse

import segno
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    RPCError,
)

from .config import Config, storage_status
from .analysis import STAGES, STAGES_SHORT, keyword_cloud, lexicon_counts, response_times, word_counts
from .charts import daily_series, grouped_bars, hbars, hourly_series, tag_cloud, word_cloud
from .prompts import WEEKDAYS
from .referrals import Candidate
from .db import BOOL_SETTINGS, FLOAT_SETTINGS, INT_SETTINGS, MODES, REFERRAL_MODES, STT_BACKENDS, Database
from .engine import Tarpit
from .logs import SOURCES, DatabaseLogHandler
from .media import MAX_UPLOAD_BYTES, prepare_upload
from .providers import PRESETS, mask_key
from .usage import usage_report

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


def _login_error(exc: Exception) -> str:
    messages = {
        PhoneNumberInvalidError: "Ungültige Telefonnummer. Bitte im Format +49… eingeben.",
        PhoneCodeInvalidError: "Der Code ist falsch.",
        PhoneCodeExpiredError: "Der Code ist abgelaufen. Bitte neu anfordern.",
        PasswordHashInvalidError: "Das 2FA-Passwort ist falsch.",
    }
    for exc_type, message in messages.items():
        if isinstance(exc, exc_type):
            return message
    if isinstance(exc, FloodWaitError):
        return f"Zu viele Versuche. Telegram verlangt {exc.seconds} Sekunden Wartezeit."
    return f"Telegram meldet: {exc}"


def _fmt_ts_full(ts: float | None) -> str:
    if not ts:
        return "–"
    dt = datetime.fromtimestamp(ts)
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%d.%m. %H:%M:%S")


def _fmt_ago(ts: float | None) -> str:
    if not ts:
        return "nie"
    return "vor " + (_fmt_duration(time.time() - ts) if time.time() - ts >= 60 else "< 1 min")


templates.env.filters["ts"] = _fmt_ts
templates.env.filters["tsfull"] = _fmt_ts_full
templates.env.filters["ago"] = _fmt_ago
templates.env.filters["usd"] = lambda v: f"${v:.2f}" if v >= 1 else f"${v:.4f}" if v >= 0.01 else f"${v:.5f}"
templates.env.globals["server_now"] = time.time
templates.env.filters["mb"] = lambda b: f"{(b or 0) / 1_000_000:.1f} MB"
templates.env.filters["num"] = lambda v: f"{int(v or 0):,}".replace(",", ".")
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
        handler = DatabaseLogHandler(db)
        logging.getLogger().addHandler(handler)
        # App-eigene INFO-Meldungen sollen im Log landen, egal wie das Logging sonst eingestellt ist
        if logging.getLogger("tarpit").getEffectiveLevel() > logging.INFO:
            logging.getLogger("tarpit").setLevel(logging.INFO)
        tarpit = Tarpit(config, db)
        app.state.db = db
        app.state.tarpit = tarpit
        app.state.model_test = None
        app.state.provider_tests = {}  # Anbieter-ID (0 = .env) -> letztes Testergebnis inkl. Modellliste
        log.info("Telegram Tarpit startet, Daten in %s", config.data_dir, extra={"source": "system"})
        for warning in config.warnings:
            log.warning(warning, extra={"source": "system"})
        storage = storage_status(config)
        if not storage["persistent"]:
            log.error("%s ist kein eingebundenes Volume: Login, Personas und Bilder gehen beim nächsten "
                      "Neubau des Containers verloren. In docker-compose.yml muss unter volumes "
                      "stehen: ./data:/data", config.data_dir, extra={"source": "system"})
        elif not storage["writable"]:
            log.error("In %s kann nicht geschrieben werden (Rechte prüfen)", config.data_dir,
                      extra={"source": "system"})
        try:
            await tarpit.start()
        except Exception:
            log.exception("Start der Telegram-Verbindung fehlgeschlagen", extra={"source": "telegram"})
        try:
            yield
        finally:
            await tarpit.stop()
            logging.getLogger().removeHandler(handler)
            db.close()

    app = FastAPI(lifespan=lifespan, dependencies=[Depends(require_auth)], docs_url=None, redoc_url=None)
    # Warnbanner auf jeder Seite, falls nicht dauerhaft gespeichert wird
    templates.env.globals["storage"] = lambda: storage_status(config)
    templates.env.globals["config_warnings"] = config.warnings
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

    @app.middleware("http")
    async def require_telegram_login(request: Request, call_next):
        path = request.url.path
        tarpit_ = getattr(request.app.state, "tarpit", None)
        if (
            tarpit_ is not None
            and not tarpit_.authorized
            and not path.startswith(("/login", "/static"))
        ):
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    def db(request: Request) -> Database:
        return request.app.state.db

    def tarpit(request: Request) -> Tarpit:
        return request.app.state.tarpit

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def safe_next(next_url: str, default: str = "/") -> str:
        return next_url if next_url.startswith("/") and not next_url.startswith("//") else default

    # --- Telegram-Login ----------------------------------------------------

    def login_page(request: Request, step: str, error: str | None = None, status: int = 200):
        t = tarpit(request)
        context = {
            "step": step,
            "error": error,
            "phone": t.login_phone,
            "code_hint": t.login_code_hint,
            "resend_hint": t.login_resend_hint,
        }
        return templates.TemplateResponse(request, "login.html", context, status_code=status)

    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request):
        await tarpit(request).refresh_login()
        if tarpit(request).authorized:
            return back("/")
        return login_page(request, "phone")

    @app.post("/login/phone")
    async def login_phone(request: Request, phone: str = Form(...)):
        phone = phone.strip().replace(" ", "")
        try:
            await tarpit(request).request_login_code(phone)
        except (RPCError, ValueError) as exc:
            return login_page(request, "phone", _login_error(exc), 400)
        return login_page(request, "code")

    @app.post("/login/resend")
    async def login_resend(request: Request):
        t = tarpit(request)
        if not t.login_phone:
            return back("/login")
        try:
            await t.request_login_code(t.login_phone)
        except (RPCError, ValueError) as exc:
            return login_page(request, "code", _login_error(exc), 400)
        return login_page(request, "code")

    @app.post("/login/qr")
    async def login_qr_start(request: Request):
        try:
            await tarpit(request).start_qr_login()
        except RPCError as exc:
            return login_page(request, "phone", _login_error(exc), 400)
        return back("/login/qr")

    @app.get("/login/qr", response_class=HTMLResponse)
    async def login_qr(request: Request):
        t = tarpit(request)
        await t.refresh_login()
        if t.authorized:
            return back("/")
        if t.qr_state is None:
            return back("/login")
        if t.qr_state == "password":
            return login_page(request, "password")
        if t.qr_state == "error":
            return login_page(request, "phone", f"QR-Login fehlgeschlagen: {t.qr_error}", 400)
        svg = None
        if t.qr_state == "waiting" and t.qr_url is not None:
            svg = segno.make(t.qr_url, error="l").svg_inline(scale=6, border=2, dark="#000", light="#fff")
        return templates.TemplateResponse(request, "login_qr.html", {"qr_svg": svg})

    @app.post("/login/code")
    async def login_code(request: Request, code: str = Form(...)):
        try:
            done = await tarpit(request).submit_login_code(code)
        except (RPCError, ValueError) as exc:
            return login_page(request, "code", _login_error(exc), 400)
        return back("/") if done else login_page(request, "password")

    @app.post("/login/password")
    async def login_password(request: Request, password: str = Form(...)):
        try:
            await tarpit(request).submit_login_password(password)
        except (RPCError, ValueError) as exc:
            return login_page(request, "password", _login_error(exc), 400)
        return back("/")

    @app.post("/logout")
    async def logout(request: Request):
        await tarpit(request).logout()
        return back("/login")

    # --- Übersicht ---------------------------------------------------------

    def wasted_seconds(c) -> float:
        return max(0.0, c["last_them"] - c["first_ai"]) if c["first_ai"] and c["last_them"] else 0.0

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        d, t = db(request), tarpit(request)
        chats = d.chats_with_stats()
        totals = {
            "active": sum(1 for c in chats if c["enabled"]),
            "ai_msgs": sum(c["n_ai"] for c in chats),
            "baited": sum(c["n_them_baited"] for c in chats),
            "wasted": sum(wasted_seconds(c) for c in chats),
        }
        labels, series = daily_series(d.message_counts_by_day(time.time() - 15 * 86400), 14)
        top_wasted = sorted(
            ((c["title"], wasted_seconds(c), _fmt_duration(wasted_seconds(c)), f"/chats/{c['chat_id']}")
             for c in chats if wasted_seconds(c) > 0),
            key=lambda item: item[1], reverse=True,
        )[:8]
        lexicon = [(name, n, f"{n}×") for name, n in lexicon_counts(d.scammer_texts())[:10]]
        analyses = d.analyses()
        active_ids = {c["chat_id"] for c in chats if c["enabled"]}
        active_messages = [m for cid in active_ids for m in d.messages(cid, limit=2000, include_notes=False)]
        hall_of_fame = [
            {"chat": row, "quote": quote}
            for row, a in analyses
            for quote in a.get("best_of", [])[:2]
        ][:10]
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "chats": chats,
                "personas": d.personas(),
                "settings": d.settings(),
                "totals": totals,
                "me": t.me,
                "health": t.health(),
                "analyses": {row["chat_id"]: a for row, a in analyses},
                "stages": STAGES,
                "sending": t.sending,
                "chart_activity": grouped_bars(labels, series, "Nachrichten pro Tag (14 Tage)"),
                "chart_wasted": hbars(top_wasted, "Gebundene Scammer-Zeit", "s-ai",
                                      "Noch keine Daten. Sobald die KI antwortet, erscheint hier die Rangliste."),
                "chart_lexicon": hbars(lexicon, "Scam-Vokabular (Nachrichten mit Treffer)", "s-them",
                                       "Noch keine Scammer-Nachrichten in KI-Chats."),
                "cloud": tag_cloud(keyword_cloud(a for _, a in analyses),
                                   "Noch keine Analysen. Sie entstehen automatisch nach ein paar Nachrichten."),
                "hall_of_fame": hall_of_fame,
                "wordcloud": word_cloud(word_counts(active_messages), "Noch zu wenige Nachrichten in KI-Chats.",
                                        limit=70, min_count=3),
                "referrals": d.referrals(limit=10),
                "approvals": [c for c in chats if c["enabled"] and c["mode"] == "review" and c["draft_text"]],
                "chat_titles": {c["chat_id"]: c["title"] for c in chats},
                "candidate_label": lambda r: Candidate(r["kind"], r["target"]).label,
                "now": time.time(),
            },
        )

    @app.get("/api/status")
    async def api_status(request: Request):
        d, t = db(request), tarpit(request)
        health = t.health()
        return JSONResponse({
            "now": time.time(),
            "global_enabled": d.settings()["global_enabled"],
            "chats": {
                str(c["chat_id"]): {
                    "due_at": c["due_at"],
                    "mode": c["mode"],
                    "sending": c["chat_id"] in t.sending,
                    "generating": c["chat_id"] in t.drafting and not t.drafting[c["chat_id"]].done(),
                }
                for c in d.enabled_chats()
            },
            "health": {
                "telegram_connected": health["telegram_connected"],
                "llm_healthy": health["llm_healthy"],
                "problems_24h": health["problems_24h"],
            },
        })

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
            log.info("KI global aktiviert", extra={"source": "engine"})
            t.resume_all()
        else:
            log.warning("KI global gestoppt (Not-Aus)", extra={"source": "engine"})
            t.stop_all()
        return back("/")

    # --- Einzelne Chats ----------------------------------------------------

    def get_chat_or_404(request: Request, chat_id: int):
        chat = db(request).chat(chat_id)
        if chat is None:
            raise HTTPException(404, "Chat nicht gefunden")
        return chat

    def best_of_ids(messages, analysis: dict | None) -> set[int]:
        """Welche Nachrichten sind im Best-of der Analyse zitiert?"""
        if not analysis:
            return set()
        quotes = [q["text"].lower().strip(" .!?\"'„“") for q in analysis.get("best_of", [])]
        quotes = [q for q in quotes if len(q) >= 6]
        return {
            m["id"] for m in messages
            if m["sender"] != "note" and any(q in m["text"].lower() for q in quotes)
        }

    def messages_context(request: Request, chat_id: int) -> dict:
        d = db(request)
        chat = d.chat(chat_id)
        messages = d.messages(chat_id)
        return {"messages": messages, "best_of": best_of_ids(messages, d.analysis(chat))}

    @app.get("/chats/{chat_id}", response_class=HTMLResponse)
    async def chat_page(request: Request, chat_id: int):
        d, t = db(request), tarpit(request)
        chat = get_chat_or_404(request, chat_id)
        if d.message_count(chat_id) == 0:
            try:
                await t.import_history(chat_id)
            except Exception:
                log.warning("Verlauf konnte nicht geladen werden", exc_info=True,
                            extra={"source": "telegram", "chat_id": chat_id})
        t.ensure_preview(chat_id)
        context = messages_context(request, chat_id)
        return templates.TemplateResponse(
            request,
            "chat.html",
            {
                **context,
                **insights(request, chat_id, context["messages"]),
                "personas": d.personas(),
                "persona": d.persona_for_chat(chat),
                "status": t.chat_status(chat_id),
                "events": d.events(limit=15, chat_id=chat_id),
                "sendable_images": list(t.sendable_images(chat_id).values()),
            },
        )

    def insights(request: Request, chat_id: int, messages) -> dict:
        """Alles für Lage, Zahlen und Grafiken eines Chats (Chat-Seite und Verlaufsseite)."""
        d = db(request)
        chat = d.chat(chat_id)
        real = [m for m in messages if m["sender"] != "note"]
        span = (real[-1]["ts"] - real[0]["ts"]) if len(real) > 1 else 0
        if real and span <= 48 * 3600:
            rows = d.message_counts_by_hour(time.time() - 49 * 3600, chat_id)
            hours = max(12, min(48, int((time.time() - real[0]["ts"]) // 3600) + 2))
            labels, series = hourly_series(rows, hours)
            chart_title = "Nachrichten pro Stunde"
        else:
            labels, series = daily_series(d.message_counts_by_day(time.time() - 15 * 86400, chat_id), 14)
            chart_title = "Nachrichten pro Tag (14 Tage)"
        row = next((c for c in d.chats_with_stats() if c["chat_id"] == chat_id), None)
        return {
            "chat": chat,
            "settings": d.settings(),
            "analysis": d.analysis(chat),
            "stages": STAGES,
            "stages_short": STAGES_SHORT,
            "times": response_times(real),
            "row": row,
            "wasted": wasted_seconds(row) if row else 0,
            "chart": grouped_bars(labels, series, chart_title, height=170),
            "referrals_out": d.referrals(source_chat_id=chat_id),
            "referrals_in": d.referrals(target_chat_id=chat_id),
            "source_chat": d.chat(chat["referred_from"]) if chat["referred_from"] else None,
            "analyzing": chat_id in tarpit(request).analyzing and not tarpit(request).analyzing[chat_id].done(),
            "candidate_label": lambda r: Candidate(r["kind"], r["target"]).label,
        }

    @app.get("/chats/{chat_id}/lage", response_class=HTMLResponse)
    async def chat_lage(request: Request, chat_id: int):
        get_chat_or_404(request, chat_id)
        messages = db(request).messages(chat_id)
        return templates.TemplateResponse(request, "_lage.html", insights(request, chat_id, messages))

    def report_context(request: Request, chat_id: int) -> dict:
        d = db(request)
        chat = d.chat(chat_id)
        messages = d.messages(chat_id, limit=1_000_000)
        days: list[tuple[str, list]] = []
        for m in messages:
            dt = datetime.fromtimestamp(m["ts"])
            day = f"{WEEKDAYS[dt.weekday()]}, {dt:%d.%m.%Y}"
            if not days or days[-1][0] != day:
                days.append((day, []))
            days[-1][1].append(m)
        return {
            **insights(request, chat_id, messages),
            "days": days,
            "best_of": best_of_ids(messages, d.analysis(chat)),
            "cloud": word_cloud(word_counts(messages), "Noch zu wenige Nachrichten für eine Wortwolke.",
                                min_count=1 if len(messages) < 40 else 2),
            "history": d.analysis_history(chat_id),
            "status": tarpit(request).chat_status(chat_id),
            "total_messages": sum(1 for m in messages if m["sender"] != "note"),
        }

    @app.get("/chats/{chat_id}/verlauf", response_class=HTMLResponse)
    async def chat_report(request: Request, chat_id: int):
        get_chat_or_404(request, chat_id)
        return templates.TemplateResponse(request, "report.html", report_context(request, chat_id))

    @app.get("/chats/{chat_id}/verlauf/body", response_class=HTMLResponse)
    async def chat_report_body(request: Request, chat_id: int):
        get_chat_or_404(request, chat_id)
        return templates.TemplateResponse(request, "_report_body.html", report_context(request, chat_id))

    @app.post("/chats/{chat_id}/summary")
    async def chat_summary(request: Request, chat_id: int, next: str = Form("")):
        get_chat_or_404(request, chat_id)
        tarpit(request).maybe_analyze(chat_id, force=True)
        return back(safe_next(next, f"/chats/{chat_id}"))

    @app.post("/referrals/{ref_id}/accept")
    async def referral_accept(request: Request, ref_id: int, next: str = Form("/")):
        ref = db(request).referral(ref_id)
        if ref is None:
            raise HTTPException(404, "Weiterleitung nicht gefunden")
        try:
            await tarpit(request).accept_referral(ref_id)
        except Exception as exc:
            log.warning("Weiterleitung fehlgeschlagen: %s", exc, exc_info=True,
                        extra={"source": "engine", "chat_id": ref["source_chat_id"]})
            db(request).update_referral(ref_id, status="failed", reason=str(exc))
        return back(safe_next(next))

    @app.post("/referrals/{ref_id}/ignore")
    async def referral_ignore(request: Request, ref_id: int, next: str = Form("/")):
        if db(request).referral(ref_id) is None:
            raise HTTPException(404, "Weiterleitung nicht gefunden")
        tarpit(request).ignore_referral(ref_id)
        return back(safe_next(next))

    @app.get("/chats/{chat_id}/messages", response_class=HTMLResponse)
    async def chat_messages(request: Request, chat_id: int):
        get_chat_or_404(request, chat_id)
        return templates.TemplateResponse(request, "_messages.html", messages_context(request, chat_id))

    @app.get("/chats/{chat_id}/status")
    async def chat_status(request: Request, chat_id: int):
        get_chat_or_404(request, chat_id)
        return JSONResponse(tarpit(request).chat_status(chat_id))

    @app.post("/chats/{chat_id}/enabled")
    async def chat_enabled(request: Request, chat_id: int, value: str = Form("0"), next: str = Form("/")):
        d, t = db(request), tarpit(request)
        get_chat_or_404(request, chat_id)
        on = value == "1"
        if on and d.message_count(chat_id) < 5:
            try:
                await t.import_history(chat_id)
            except Exception:
                log.warning("Verlauf konnte nicht geladen werden", exc_info=True,
                            extra={"source": "telegram", "chat_id": chat_id})
        t.set_enabled(chat_id, on)
        return back(safe_next(next))

    @app.post("/chats/{chat_id}/mode")
    async def chat_mode(request: Request, chat_id: int, mode: str = Form(...), next: str = Form("")):
        get_chat_or_404(request, chat_id)
        if mode not in MODES:
            raise HTTPException(400, "Ungültiger Modus")
        tarpit(request).set_mode(chat_id, mode)
        return back(safe_next(next, f"/chats/{chat_id}"))

    @app.post("/chats/{chat_id}/persona")
    async def chat_persona(
        request: Request, chat_id: int, persona_id: str = Form(""), next: str = Form("/")
    ):
        get_chat_or_404(request, chat_id)
        db(request).set_chat_persona(chat_id, int(persona_id) if persona_id else None)
        return back(safe_next(next))

    @app.post("/chats/{chat_id}/reply-now")
    async def chat_reply_now(request: Request, chat_id: int, next: str = Form("")):
        get_chat_or_404(request, chat_id)
        tarpit(request).reply_now(chat_id)
        return back(safe_next(next, f"/chats/{chat_id}"))

    @app.post("/chats/{chat_id}/draft")
    async def chat_draft_save(request: Request, chat_id: int, text: str = Form(...), action: str = Form("save")):
        get_chat_or_404(request, chat_id)
        t = tarpit(request)
        if text.strip():
            current = db(request).chat(chat_id)["draft_text"] or ""
            if text.strip() != current.strip():
                t.save_draft(chat_id, text.strip())
        if action == "send":
            t.reply_now(chat_id)
        return back(f"/chats/{chat_id}")

    @app.post("/chats/{chat_id}/draft/regenerate")
    async def chat_draft_regenerate(
        request: Request, chat_id: int, instruction: str = Form(""), next: str = Form(""),
    ):
        get_chat_or_404(request, chat_id)
        tarpit(request).regenerate_draft(chat_id, instruction)
        return back(safe_next(next, f"/chats/{chat_id}"))

    @app.post("/chats/{chat_id}/draft/discard")
    async def chat_draft_discard(request: Request, chat_id: int, next: str = Form("")):
        get_chat_or_404(request, chat_id)
        tarpit(request).discard_draft(chat_id)
        return back(safe_next(next, f"/chats/{chat_id}"))

    @app.post("/chats/{chat_id}/import")
    async def chat_import(request: Request, chat_id: int):
        get_chat_or_404(request, chat_id)
        await tarpit(request).import_history(chat_id)
        return back(f"/chats/{chat_id}")

    @app.post("/chats/{chat_id}/analyze")
    async def chat_analyze(request: Request, chat_id: int, next: str = Form("")):
        get_chat_or_404(request, chat_id)
        tarpit(request).maybe_analyze(chat_id, force=True)
        return back(safe_next(next, f"/chats/{chat_id}"))

    @app.post("/chats/{chat_id}/send")
    async def chat_send(request: Request, chat_id: int, text: str = Form(...)):
        get_chat_or_404(request, chat_id)
        if text.strip():
            await tarpit(request).send_manual(chat_id, text.strip())
        return back(f"/chats/{chat_id}")

    # --- Log & Status ------------------------------------------------------

    def logs_context(request: Request, level: str, source: str, chat: str) -> dict:
        d = db(request)
        chat_id = int(chat) if chat.lstrip("-").isdigit() else None
        titles = {c["chat_id"]: c["title"] for c in d.chats_with_stats()}
        return {
            "events": d.events(limit=300, level=level or None, source=source or None, chat_id=chat_id),
            "titles": titles,
            "sources": SOURCES,
        }

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_page(request: Request, level: str = "", source: str = "", chat: str = ""):
        t = tarpit(request)
        return templates.TemplateResponse(
            request,
            "logs.html",
            {
                **logs_context(request, level, source, chat),
                "health": t.health(),
                "settings": db(request).settings(),
                "filters": {"level": level, "source": source, "chat": chat},
                "model_test": request.app.state.model_test,
            },
        )

    @app.get("/logs/rows", response_class=HTMLResponse)
    async def logs_rows(request: Request, level: str = "", source: str = "", chat: str = ""):
        return templates.TemplateResponse(request, "_log_rows.html", logs_context(request, level, source, chat))

    @app.post("/logs/test-model")
    async def logs_test_model(request: Request):
        ok, message = await tarpit(request).test_model()
        request.app.state.model_test = {"ok": ok, "message": message, "at": time.time()}
        return back("/logs")

    @app.post("/logs/test-notify")
    async def logs_test_notify(request: Request):
        t = tarpit(request)
        await t.notifier.send(t.client, t.me.id if t.me else None,
                              "🔔 Test: So erreichen dich Benachrichtigungen." + t.notifier.link("/logs"))
        return back("/logs")

    # --- Anbieter ------------------------------------------------------------

    ROLE_NAMES = {"model_provider": "Antworten", "analysis_provider": "Analyse",
                  "vision_provider": "Bilderkennung", "stt_provider": "Spracherkennung"}

    @app.get("/anbieter", response_class=HTMLResponse)
    async def providers_page(request: Request, edit: int | None = None, error: str = ""):
        d = db(request)
        settings = d.settings()
        usage_by_provider: dict[str, list[str]] = {}
        for key, name in ROLE_NAMES.items():
            model_key = {"model_provider": "model", "analysis_provider": "analysis_model",
                         "vision_provider": "vision_model", "stt_provider": "stt_model"}[key]
            if key == "model_provider" or settings[model_key]:
                usage_by_provider.setdefault(settings[key], []).append(name)
        return templates.TemplateResponse(request, "providers.html", {
            "providers": d.providers(),
            "presets": PRESETS,
            "editing": d.provider(edit) if edit else None,
            "tests": request.app.state.provider_tests,
            "env_base_url": config.llm_base_url,
            "env_key": mask_key(config.llm_api_key),
            "mask": mask_key,
            "usage_by_provider": usage_by_provider,
            "error": error,
        })

    @app.post("/anbieter")
    async def provider_save(
        request: Request, name: str = Form(...), kind: str = Form(...), base_url: str = Form(...),
        api_key: str = Form(""), provider_id: str = Form(""),
    ):
        base_url = base_url.strip().rstrip("/")
        if kind not in PRESETS:
            raise HTTPException(400, "Unbekannte Art")
        if not base_url.startswith(("http://", "https://")):
            return back("/anbieter?error=" + quote("Die Adresse muss mit http:// oder https:// beginnen"))
        pid = db(request).save_provider(
            int(provider_id) if provider_id.isdigit() else None, name.strip() or PRESETS[kind].name,
            kind, base_url, api_key.strip() or (None if provider_id else ""),
        )
        request.app.state.provider_tests.pop(pid, None)
        log.info("Anbieter gespeichert: %s (%s)", name, base_url, extra={"source": "llm"})
        return back(f"/anbieter#provider-{pid}")

    @app.post("/anbieter/{provider_id}/delete")
    async def provider_delete(request: Request, provider_id: int):
        db(request).delete_provider(provider_id)
        request.app.state.provider_tests.pop(provider_id, None)
        return back("/anbieter")

    @app.post("/anbieter/{provider_id}/test")
    async def provider_test(request: Request, provider_id: int):
        if provider_id and db(request).provider(provider_id) is None:
            raise HTTPException(404, "Anbieter nicht gefunden")
        ok, message, models = await tarpit(request).test_provider(provider_id)
        request.app.state.provider_tests[provider_id] = {"ok": ok, "message": message, "models": models,
                                                         "at": time.time()}
        return back(f"/anbieter#provider-{provider_id}" if provider_id else "/anbieter")

    @app.get("/verbrauch", response_class=HTMLResponse)
    async def usage_page(request: Request):
        d = db(request)
        return templates.TemplateResponse(request, "usage.html", usage_report(d, d.settings()))

    # --- Personas ----------------------------------------------------------

    @app.get("/personas", response_class=HTMLResponse)
    async def personas_page(request: Request, edit: int | None = None, error: str = ""):
        d = db(request)
        personas = d.personas()
        return templates.TemplateResponse(
            request,
            "personas.html",
            {
                "personas": personas,
                "images": {p["id"]: d.persona_images(p["id"]) for p in personas},
                "editing": d.persona(edit) if edit else None,
                "vision_model": d.settings()["vision_model"],
                "error": error,
            },
        )

    @app.post("/personas/{persona_id}/images")
    async def persona_image_upload(
        request: Request, persona_id: int, file: UploadFile = File(...), description: str = Form(""),
    ):
        d, t = db(request), tarpit(request)
        if d.persona(persona_id) is None:
            raise HTTPException(404, "Persona nicht gefunden")
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            jpeg = prepare_upload(data)
        except ValueError as exc:
            return back(f"/personas?error={quote(str(exc))}#persona-{persona_id}")
        filename = f"{uuid.uuid4().hex}.jpg"
        (config.media_dir / filename).write_bytes(jpeg)
        description = description.strip()
        if not description:
            description = (await t.describe_persona_image(jpeg)) or ""
        d.add_persona_image(persona_id, filename, description[:200])
        log.info("Bild für Persona hochgeladen: %s", description or "(ohne Beschreibung)", extra={"source": "web"})
        return back(f"/personas#persona-{persona_id}")

    @app.post("/personas/images/{image_id}")
    async def persona_image_update(request: Request, image_id: int, description: str = Form("")):
        image = db(request).persona_image(image_id)
        if image is None:
            raise HTTPException(404, "Bild nicht gefunden")
        db(request).update_persona_image(image_id, description.strip()[:200])
        return back(f"/personas#persona-{image['persona_id']}")

    @app.post("/personas/images/{image_id}/delete")
    async def persona_image_delete(request: Request, image_id: int):
        image = db(request).persona_image(image_id)
        if image is None:
            raise HTTPException(404, "Bild nicht gefunden")
        db(request).delete_persona_image(image_id)
        (config.media_dir / image["filename"]).unlink(missing_ok=True)
        return back(f"/personas#persona-{image['persona_id']}")

    @app.get("/media/images/{image_id}")
    async def media_image(request: Request, image_id: int):
        image = db(request).persona_image(image_id)
        path = config.media_dir / image["filename"] if image else None
        if path is None or not path.exists():
            raise HTTPException(404, "Bild nicht gefunden")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

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
        for image in db(request).persona_images(persona_id):
            (config.media_dir / image["filename"]).unlink(missing_ok=True)
        db(request).delete_persona(persona_id)
        return back("/personas")

    # --- Einstellungen -----------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, saved: int = 0):
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"settings": db(request).settings(), "saved": saved, "llm_base_url": config.llm_base_url,
             "notify_channel": tarpit(request).notifier.channel, "providers": db(request).providers(),
             "model_lists": {("env" if pid == 0 else str(pid)): t["models"]
                             for pid, t in request.app.state.provider_tests.items() if t["models"]}},
        )

    @app.post("/settings")
    async def settings_save(request: Request):
        d = db(request)
        form = await request.form()
        model = str(form.get("model", "")).strip()
        if model:
            d.set_setting("model", model)
        for key in ("analysis_model", "vision_model", "stt_model"):
            if key in form:
                d.set_setting(key, str(form.get(key, "")).strip())
        provider_ids = {str(pr["id"]) for pr in d.providers()}
        for key in ("model_provider", "analysis_provider", "vision_provider", "stt_provider"):
            if key in form and (form.get(key) == "" or form.get(key) in provider_ids):
                d.set_setting(key, form.get(key))
        if form.get("stt_backend") in STT_BACKENDS:
            d.set_setting("stt_backend", form.get("stt_backend"))
        current = d.settings()
        try:
            floats = {
                key: float(str(form.get(key) or current[key]).replace(",", ".")) for key in FLOAT_SETTINGS
            }
            temperature = floats.pop("temperature")
            # fehlende Felder behalten ihren Wert
            ints = {key: int(str(form.get(key) or current[key])) for key in INT_SETTINGS}
        except ValueError:
            raise HTTPException(400, "Bitte nur Zahlen eintragen")
        d.set_setting("temperature", min(max(temperature, 0.0), 2.0))
        for key, value in floats.items():
            d.set_setting(key, max(0.0, value))
        for low, high in (("min_delay", "max_delay"), ("referral_min_delay", "referral_max_delay")):
            if ints[low] > ints[high]:
                ints[low], ints[high] = ints[high], ints[low]
        ints["quiet_start"] %= 24
        ints["quiet_end"] %= 24
        ints["history_limit"] = max(4, ints["history_limit"])
        ints["analyze_every"] = max(1, ints["analyze_every"])
        for key, value in ints.items():
            d.set_setting(key, max(0, value))
        for key in BOOL_SETTINGS - {"global_enabled"}:
            d.set_setting(key, form.get(key) == "1")
        if form.get("referral_mode") in REFERRAL_MODES:
            d.set_setting("referral_mode", form.get("referral_mode"))
        log.info("Einstellungen gespeichert", extra={"source": "web"})
        return back("/settings?saved=1")

    return app
