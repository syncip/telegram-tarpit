// Telegram Tarpit: Tooltips, sekundengenaue Countdowns, Live-Status.
(function () {
  "use strict";

  // Serverzeit als Referenz, damit Countdowns auch bei falsch gehender PC-Uhr stimmen
  let clockOffset = 0; // Sekunden: Server minus Browser
  const serverNow = parseFloat(document.body.dataset.serverNow || "0");
  if (serverNow) clockOffset = serverNow - Date.now() / 1000;
  const now = () => Date.now() / 1000 + clockOffset;

  // --- Tooltips ----------------------------------------------------------------
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.hidden = true;
  document.body.appendChild(tip);
  function showTip(el, x, y) {
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(window.innerWidth - w - 8, Math.max(8, x + 12)) + "px";
    tip.style.top = Math.max(8, y - h - 12) + "px";
  }
  document.addEventListener("mousemove", (e) => {
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (el) showTip(el, e.clientX, e.clientY); else tip.hidden = true;
  });
  document.addEventListener("touchstart", (e) => {
    const el = e.target.closest && e.target.closest("[data-tip]");
    if (el) { const t = e.touches[0]; showTip(el, t.clientX, t.clientY); } else tip.hidden = true;
  }, { passive: true });

  // --- Countdowns --------------------------------------------------------------
  const pad = (n) => String(n).padStart(2, "0");
  function clock(ts) {
    const d = new Date((ts - clockOffset) * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }
  function formatRemaining(seconds) {
    seconds = Math.max(0, Math.round(seconds));
    const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    if (h) return h + " h " + pad(m) + " min " + pad(s) + " s";
    if (m) return m + " min " + pad(s) + " s";
    return s + " s";
  }
  function renderCountdown(el) {
    const due = parseFloat(el.dataset.due || "");
    const state = el.dataset.state || "";
    if (state === "sending") { el.textContent = "⌨️ tippt gerade …"; return; }
    if (!due) { el.textContent = el.dataset.empty || "–"; return; }
    const left = due - now();
    el.textContent = left <= 0 ? "jetzt (" + clock(due) + ")" : "in " + formatRemaining(left) + " · " + clock(due);
  }
  function tick() { document.querySelectorAll(".countdown").forEach(renderCountdown); }
  setInterval(tick, 1000);
  tick();

  async function getJSON(url) {
    const res = await fetch(url, { credentials: "same-origin", cache: "no-store" });
    if (!res.ok) throw new Error(res.status);
    return res.json();
  }

  // --- Übersicht ---------------------------------------------------------------
  if (document.body.dataset.page === "index") {
    setInterval(async () => {
      try {
        const data = await getJSON("/api/status");
        clockOffset = data.now - Date.now() / 1000;
        document.querySelectorAll("[data-chat-row]").forEach((row) => {
          const c = data.chats[row.dataset.chatRow];
          const el = row.querySelector(".countdown");
          if (!el) return;
          el.dataset.due = c && c.due_at ? c.due_at : "";
          el.dataset.state = c && c.sending ? "sending" : "";
          el.dataset.empty = c && c.generating ? "✍️ Entwurf entsteht" : (el.dataset.emptyDefault || "–");
        });
        const tg = document.getElementById("health-telegram");
        if (tg) tg.dataset.ok = data.health.telegram_connected ? "1" : "0";
        tick();
      } catch (e) { /* nächster Versuch */ }
    }, 5000);
  }

  // --- Chat-Seite --------------------------------------------------------------
  const chatRoot = document.querySelector("[data-chat-id]");
  if (chatRoot) {
    const chatId = chatRoot.dataset.chatId;
    const box = document.getElementById("messages");
    const draft = document.getElementById("draft-text");
    const countdown = document.getElementById("next-reply");
    const badge = document.getElementById("reply-state");
    let lastMsgId = chatRoot.dataset.lastMsgId;
    let lastLageKey = null;
    let draftDirty = false;
    let lastDraft = draft ? draft.value : "";
    if (draft) draft.addEventListener("input", () => { draftDirty = draft.value !== lastDraft; });
    if (box) box.scrollTop = box.scrollHeight;

    async function reloadMessages() {
      const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      const res = await fetch("/chats/" + chatId + "/messages", { credentials: "same-origin" });
      if (!res.ok) return;
      box.innerHTML = await res.text();
      if (stick) box.scrollTop = box.scrollHeight;
    }

    function stateText(s) {
      if (!s.enabled) return ["off", "KI ist für diesen Chat aus"];
      if (!s.global_enabled) return ["off", "KI global gestoppt"];
      if (s.sending) return ["busy", "⌨️ KI tippt und sendet gerade"];
      if (s.mode === "manual") return ["off", "✋ Nur du antwortest"];
      if (s.generating) return ["busy", "✍️ KI schreibt einen Entwurf …"];
      if (s.mode === "review" && s.draft_text) return ["wait", "👀 Entwurf wartet auf deine Freigabe"];
      if (s.due_at) return ["ok", "⏳ Antwort ist geplant"];
      return ["idle", "Keine Antwort nötig: du oder die KI habt zuletzt geschrieben"];
    }

    function renderDraftImages(images) {
      const box = document.getElementById("draft-images");
      if (!box) return;
      const key = JSON.stringify(images);
      if (box.dataset.key === key) return;
      box.dataset.key = key;
      box.innerHTML = "";
      images.forEach((img) => {
        const el = document.createElement("div");
        el.className = "thumb" + (img.ok ? "" : " thumb-bad");
        el.dataset.tip = img.ok ? "Wird mitgeschickt: " + img.description
                                : "Wird NICHT geschickt (schon gesendet oder nicht vorhanden): " + img.description;
        if (img.url) { const i = document.createElement("img"); i.src = img.url; i.alt = img.description; el.appendChild(i); }
        const label = document.createElement("span");
        label.className = "chip small";
        label.textContent = (img.ok ? "📷 #" : "⚠️ #") + img.id;
        el.appendChild(label);
        box.appendChild(el);
      });
    }
    document.querySelectorAll("[data-insert-image]").forEach((btn) => {
      btn.addEventListener("click", () => {
        if (!draft) return;
        const marker = "[BILD:" + btn.dataset.insertImage + "]";
        draft.value = (draft.value.trim() ? draft.value.trim() + "\n" : "") + marker;
        draftDirty = true;
        draft.focus();
      });
    });

    async function poll() {
      try {
        const s = await getJSON("/chats/" + chatId + "/status");
        clockOffset = s.now - Date.now() / 1000;
        if (countdown) {
          countdown.dataset.due = s.due_at || "";
          countdown.dataset.state = s.sending ? "sending" : "";
          countdown.dataset.empty = s.mode === "review" ? "wartet auf Freigabe" : "–";
        }
        if (badge) { const [cls, text] = stateText(s); badge.className = "state state-" + cls; badge.textContent = text; }
        document.querySelectorAll("[data-show-if-draft]").forEach((el) => { el.hidden = !s.draft_text; });
        const stale = document.getElementById("draft-stale");
        if (stale) stale.hidden = !(s.draft_text && s.draft_stale);
        const edited = document.getElementById("draft-edited");
        if (edited) edited.hidden = !s.draft_edited;
        renderDraftImages(s.draft_images || []);
        const gen = document.getElementById("draft-generating");
        if (gen) gen.hidden = !s.generating;
        // Entwurf nur aktualisieren, wenn du ihn gerade nicht bearbeitest
        if (draft && !draftDirty && document.activeElement !== draft) {
          const text = s.draft_text || "";
          if (text !== draft.value) { draft.value = text; lastDraft = text; }
        }
        if (String(s.last_msg_id) !== String(lastMsgId)) { lastMsgId = s.last_msg_id; await reloadMessages(); }
        // Lage-Karte neu laden, wenn eine neue Zusammenfassung da ist oder gerade entsteht
        const lageKey = [s.analysis_at, s.analyzing, s.last_msg_id].join("|");
        if (lastLageKey !== null && lageKey !== lastLageKey) await reloadFragment("lage", "/chats/" + chatId + "/lage");
        lastLageKey = lageKey;
        tick();
      } catch (e) { /* nächster Versuch */ }
    }
    poll();
    setInterval(poll, 2000);
  }

  async function reloadFragment(id, url) {
    const el = document.getElementById(id);
    if (!el) return;
    const res = await fetch(url, { credentials: "same-origin" });
    if (res.ok) el.innerHTML = await res.text();
  }

  // --- Verlauf & Auswertung ----------------------------------------------------
  const report = document.querySelector("[data-report-chat-id]");
  if (report) {
    const chatId = report.dataset.reportChatId;
    const search = document.getElementById("report-search");
    const notes = document.getElementById("report-notes");
    let key = [report.dataset.lastMsgId, report.dataset.analysisAt, report.dataset.analyzing].join("|");

    function applyFilter() {
      const q = (search.value || "").trim().toLowerCase();
      const body = document.getElementById("report-body");
      body.classList.toggle("hide-notes", !notes.checked);
      body.querySelectorAll(".transcript .msg").forEach((m) => {
        m.hidden = q !== "" && !(m.dataset.text || "").includes(q);
      });
      body.querySelectorAll(".day-group").forEach((g) => {
        g.hidden = q !== "" && !g.querySelector(".msg:not([hidden])");
      });
    }
    search.addEventListener("input", applyFilter);
    notes.addEventListener("change", applyFilter);
    applyFilter();

    setInterval(async () => {
      try {
        const s = await getJSON("/chats/" + chatId + "/status");
        const next = [s.last_msg_id, s.analysis_at || "", s.analyzing ? "1" : ""].join("|");
        if (next !== key) {
          key = next;
          const y = window.scrollY;
          await reloadFragment("report-body", "/chats/" + chatId + "/verlauf/body");
          applyFilter();
          window.scrollTo(0, y);
        }
        const stamp = document.getElementById("report-updated");
        if (stamp) stamp.textContent = clock(s.now);
      } catch (e) { /* nächster Versuch */ }
    }, 4000);
  }

  // --- Log-Seite ---------------------------------------------------------------
  const logRows = document.getElementById("log-rows");
  if (logRows) {
    setInterval(async () => {
      if (document.getElementById("log-autorefresh") && !document.getElementById("log-autorefresh").checked) return;
      try {
        const res = await fetch("/logs/rows" + window.location.search, { credentials: "same-origin" });
        if (res.ok) logRows.innerHTML = await res.text();
      } catch (e) { /* nächster Versuch */ }
    }, 5000);
  }
})();
