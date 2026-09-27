"""Nachbauten von Telegram-Client und LLM für die Tests."""

import contextlib
import json
from types import SimpleNamespace

from tarpit.llm import ChatResult, LLMStats, Usage

ME = SimpleNamespace(id=1, first_name="Ich", last_name=None, username="ich", bot=False, access_hash=0)

ANALYSIS_JSON = json.dumps({
    "scam_type": "Krypto-Investment",
    "stage": 3,
    "summary": "Der Scammer bietet Bitcoin-Rendite an, Gerda sucht ihre Brille.",
    "scammer_goal": "Erste Einzahlung",
    "bait_tactic": "Technikprobleme",
    "frustration": 7,
    "keywords": ["Bitcoin", "Rendite", "garantiert"],
    "best_of": [{"sender": "scammer", "text": "willst du reich werden"}],
})


class FakeClient:
    def __init__(self):
        self.sent = []
        self.read = []
        self.typing = 0
        self.status_updates = []
        self.logged_in_user = None
        self.on_login_calls = 0
        self._next_id = 1000

    async def send_read_acknowledge(self, chat_id):
        self.read.append(chat_id)

    @contextlib.asynccontextmanager
    async def action(self, chat_id, kind):
        self.typing += 1
        yield

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        self._next_id += 1
        return SimpleNamespace(id=self._next_id)

    async def __call__(self, request):
        self.status_updates.append(request)

    async def get_me(self):
        return self.logged_in_user

    async def _on_login(self, user):
        self.on_login_calls += 1

    def is_connected(self):
        return True

    async def iter_messages(self, *args, **kwargs):
        return
        yield

    async def iter_dialogs(self, *args, **kwargs):
        return
        yield

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def log_out(self):
        pass


class FakeLLM:
    """Gibt vorgegebene Antworten zurück; Analyse-Anfragen bekommen ein JSON."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.default = "ach herrje, wie geht das denn?"
        self.calls = []
        self.stats = LLMStats()
        self.base_url = "http://fake"

    async def complete(self, model, messages, temperature, max_tokens=None):
        self.calls.append(messages)
        if "Du analysierst" in messages[0]["content"]:
            text = ANALYSIS_JSON
        else:
            text = self.replies.pop(0) if self.replies else self.default
        usage = Usage(prompt=500, cached=300, completion=20, cost=0.0001)
        self.stats.ok(model, 5, usage)
        return ChatResult(text=text, usage=usage, latency_ms=5)

    async def chat(self, model, messages, temperature, max_tokens=None):
        return (await self.complete(model, messages, temperature, max_tokens)).text

    async def aclose(self):
        pass

    @property
    def reply_calls(self):
        return [c for c in self.calls if "Du analysierst" not in c[0]["content"]]
