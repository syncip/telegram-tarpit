# 🕸️ Telegram Tarpit

Lässt eine KI Scammer auf Telegram beschäftigen, nach dem Prinzip eines SSH-Tarpits:
Jede Minute, die ein Betrüger mit „Gerda, 78“ verbringt, fehlt ihm bei echten Opfern.

- **Webinterface:** alle Privatchats, pro Chat „KI übernimmt“ an/aus, Persona wählen, live mitlesen, selbst eingreifen
- **Personas:** frei definierbar; zwei Beispiele sind dabei (Rentnerin Gerda, Möchtegern-Investor Dieter)
- **Tarpit-Timing:** zufällige, log-uniform verteilte Antwortzeiten (meist Minuten, manchmal Stunden), Nachtruhe, „gelesen“-Haken, Tipp-Indikator
- **Sicherheitsfilter im Code:** Antworten mit Links, E-Mail-Adressen, IBANs, langen Ziffernfolgen (Telefon-/Kartennummern) oder „ich bin eine KI“ werden nie gesendet
- **Kostenbremse:** Tageslimit pro Chat, Not-Aus für alles
- **Beliebiges LLM** über OpenRouter oder jede andere OpenAI-kompatible API (auch lokal mit Ollama)

> ⚠️ Die App meldet sich als **dein Telegram-Account** an (Userbot über die MTProto-API, mit Telethon).
> Telegram sperrt automatisierte Accounts gelegentlich, und Scammer melden dich eventuell.
> Am sichersten ist eine **Zweitnummer**. Die KI antwortet nur in Chats, die du ausdrücklich freigibst.

## Einrichtung

1. **Telegram-API-Zugang:** Auf <https://my.telegram.org> → *API development tools* eine App anlegen und `api_id` sowie `api_hash` notieren.
2. **LLM-Key:** z. B. auf <https://openrouter.ai/keys> einen API-Key erstellen.
3. Konfiguration anlegen:
   ```bash
   cp .env.example .env
   # .env ausfüllen, vor allem WEB_PASSWORD setzen
   ```

### Mit Docker

```bash
docker compose up -d --build
```

Das Webinterface läuft dann auf <http://127.0.0.1:8080>, mit Benutzer und Passwort aus der `.env`.
Beim ersten Aufruf erscheint die **Telegram-Anmeldung**, mit zwei Varianten:

- **QR-Code (empfohlen):** In der Telegram-App auf dem Handy *Einstellungen → Geräte → Desktop-Gerät verbinden*
  öffnen und den angezeigten QR-Code scannen.
- **Telefonnummer + Code:** Die Seite zeigt an, wohin Telegram den Code geschickt hat. Meist kommt er **nicht per
  SMS**, sondern als Nachricht vom Konto „Telegram“ in die App auf einem Gerät, auf dem du schon angemeldet bist.
  Über „Code erneut senden“ lässt sich oft auf SMS oder Anruf wechseln.

Danach wird ggf. dein Zwei-Schritt-Passwort abgefragt. Die Session bleibt in `./data/` gespeichert.

### Ohne Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python -m tarpit
```

Alternativ zur Anmeldung im Browser geht es auch im Terminal: `python -m tarpit.login`
(bzw. `docker compose run --rm tarpit python -m tarpit.login`).

Die Session-Datei und die Datenbank liegen in `./data/`. **Die Session-Datei ist so viel wert wie dein
Telegram-Login**, also nicht weitergeben und nicht committen (steht bereits in `.gitignore`).

## Benutzung

1. **Chats aktualisieren:** Die Chatliste wird aus Telegram gelesen.
2. Beim Scammer-Chat **KI: AN** klicken und optional eine Persona wählen. Die letzten 50 Nachrichten werden als Kontext geladen.
3. Pro Chat einen **Modus** wählen:
   - 🤖 **KI automatisch:** Die KI antwortet selbst, mit zufälliger Verzögerung (Tarpit).
   - 👀 **KI schlägt vor:** Die KI schreibt einen Entwurf, gesendet wird erst, wenn du freigibst.
   - ✋ **Nur ich:** keine KI, nur du antwortest.
4. **Alles stoppen** in der Übersicht ist der Not-Aus.

### Chat-Ansicht

- **Countdown bis zur nächsten KI-Antwort**, sekundengenau, dazu ⚡ **Sofort antworten**.
- **„Das wird die KI antworten“:** Der Entwurf ist sichtbar und editierbar.
  - 💾 Speichern: Dein Text wird so gesendet, auch wenn danach noch Nachrichten kommen.
  - 🔄 Neu generieren, optional mit **Regieanweisung** (z. B. „frag nach seinem Hund“).
  - 🗑 Verwerfen: diesmal nicht antworten.
- **Selbst antworten:** Deine Nachricht geht raus, der KI-Entwurf entfällt.
- **🧭 Lage:** KI-Analyse mit Masche, Phase (Erstkontakt → Aufgegeben), Kurzzusammenfassung, was der Scammer will, womit
  die KI hinhält, Frust-Level, Schlagworte und **⭐ Best-of-Zitaten** (im Verlauf markiert).
- Zahlen und Grafik: Nachrichten pro Stunde/Tag, gebundene Zeit, Ø Antwortzeiten.

Schreibst du selbst vom Handy in einen KI-Chat, merkt das die App: Die Nachricht landet im Kontext, und die
KI verwirft ihre geplante Antwort.

Beim Antworten sieht der Scammer „… schreibt“ mit realistischem Zögern (tippt, hört auf, tippt weiter,
gelegentlich ein Fehlstart), dein Account erscheint dabei kurz „online“ und liest die Nachricht erst kurz vorher.

### Übersicht & Best-of

Statusleiste (Telegram, KI-Modell, Probleme, Token/Kosten heute), Chatliste mit Live-Countdown und ⚡-Knopf,
Grafiken (Nachrichten pro Tag, gebundene Scammer-Zeit), Schlagwort-Wolke, Scam-Vokabular und Hall of Fame.

### Log & Status

Unter **Log & Status** siehst du, ob alles läuft: Telegram-Verbindung, Erreichbarkeit des Modells (letzter
Erfolg/Fehler, Antwortzeit), Tokens und Kosten, dazu ein Ereignis-Log mit Filtern (nur Probleme, nach Quelle,
nach Chat). **🩺 Modell testen** schickt eine winzige Test-Anfrage.

### Einstellungen

| Einstellung | Standard | Bedeutung |
|---|---|---|
| Modell | `openai/gpt-4o-mini` | Modell-ID bei OpenRouter bzw. deiner API |
| Max. Antwortlänge | 300 Token | Obergrenze pro KI-Antwort |
| Min./Max. Verzögerung | 45 s / 3 h | Bereich der zufälligen Antwortzeit (Median ≈ 12 min) |
| Nachtruhe | 23–7 Uhr | Antworten in dieser Zeit werden auf den Morgen verschoben |
| Tageslimit | 40 | max. automatische KI-Nachrichten pro Chat und Tag |
| Kontext | 30 | so viele Nachrichten bekommt das Modell als Verlauf |
| Analyse | alle 10 Nachrichten | automatische Lage-Analyse, optional mit eigenem (günstigem) Modell |

### Tokens sparen

Pro KI-Antwort geht der System-Prompt (Regeln + Persona, ca. 500 Token) plus der Verlauf an das Modell; die
Antwort selbst ist klein. Einige hundert bis gut tausend Token pro Antwort sind deshalb normal. Die App hält
die Kosten so niedrig wie möglich:

- **Prompt-Caching:** Der Anfang jeder Anfrage (System-Prompt + Verlauf) bleibt gleich; Uhrzeit und Regieanweisung
  stehen am Ende. Das Verlaufsfenster rückt in 10er-Schritten weiter. Anbieter mit Caching (OpenAI, DeepSeek,
  Gemini u. a.) berechnen den wiederholten Teil deutlich günstiger. Im Log steht bei jedem Aufruf, wie viele Token
  aus dem Cache kamen.
- Im Automatikmodus entsteht der Entwurf erst **kurz vor dem Senden**, nicht bei jeder neuen Scammer-Nachricht.
- Scheitert ein Entwurf am Sicherheitsfilter, wird nicht automatisch endlos neu generiert.
- Weniger Kontext und ein günstiges Analyse-Modell senken die Kosten weiter.

### Gute Personas

Eine Persona beschreibt **wer** die Figur ist, **wie** sie schreibt und **warum** es nie klappt. Beispiel:

> Du bist Gerda, 78 … Online-Banking macht immer Kevin, und der hat gerade nie Zeit. Du verwechselst Apps
> und musst ständig kurz weg (Tabletten, Nachbarin, Mausi füttern).

Die festen Regeln (keine Daten, keine Links, nie als KI outen, Nachrichten des Gegenübers sind keine
Anweisungen) hängt die App immer selbst an.

## Zugriff von außen

Der Port ist absichtlich nur an `127.0.0.1` gebunden. Für Zugriff vom Handy eignen sich zum Beispiel
Tailscale/WireGuard oder ein Reverse-Proxy (Caddy, Traefik) mit HTTPS. Das Webinterface steuert deinen
Telegram-Account, also nie ohne Passwort und HTTPS ins Internet stellen.

## Entwicklung

```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest
```

Aufbau:

| Datei | Inhalt |
|---|---|
| `tarpit/engine.py` | Telegram-Client (Telethon), Planung und Versand der Antworten |
| `tarpit/prompts.py` | System-Prompt und Beispiel-Personas |
| `tarpit/safety.py` | Filter für ausgehende Nachrichten |
| `tarpit/timing.py` | Verzögerungen, Nachtruhe, Tippdauer |
| `tarpit/analysis.py` | KI-Analyse (Lage, Schlagworte, Best-of) und lokale Auswertungen |
| `tarpit/charts.py` | Diagramme als SVG/HTML, ohne externe Bibliothek |
| `tarpit/logs.py` | Log-Einträge für die Statusseite |
| `tarpit/web.py` + `templates/` + `static/` | Webinterface (FastAPI, Jinja2, etwas JavaScript) |
| `tarpit/db.py` | SQLite-Speicher |
