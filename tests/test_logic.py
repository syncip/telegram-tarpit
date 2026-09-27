import random
from datetime import datetime

from datetime import date

from tarpit.analysis import keyword_cloud, lexicon_counts, parse_analysis, response_times
from tarpit.charts import daily_series, grouped_bars, hbars
from tarpit.prompts import build_messages
from tarpit.safety import check_reply, clean_reply, split_reply
from tarpit.timing import in_quiet_hours, postpone_quiet_hours, sample_delay, typing_duration, typing_plan


def test_check_reply_allows_normal_text():
    assert check_reply("ach herrje... wo ist denn der knopf zum überweisen?") is None
    assert check_reply("ich hab 500 euro auf der sparkasse, reicht das?") is None


def test_check_reply_blocks_dangerous_content():
    assert check_reply("schau mal hier https://example.com") == "Link"
    assert check_reply("geh auf t.me/irgendwas") == "Link"
    assert check_reply("schreib mir an gerda@web.de") == "E-Mail-Adresse"
    assert check_reply("meine iban ist DE89 3704 0044 0532 0130 00") == "IBAN"
    assert check_reply("ruf an: 0171 234 5678 9") == "lange Ziffernfolge"
    assert check_reply("Als KI kann ich das leider nicht") == "KI-Enttarnung"
    assert check_reply("I'm an AI language model") == "KI-Enttarnung"
    assert check_reply("   ") == "leere Antwort"
    assert check_reply("a" * 5000) == "Antwort zu lang"


def test_clean_reply():
    assert clean_reply('"hallo du"') == "hallo du"
    assert clean_reply("„hallo du“") == "hallo du"
    assert clean_reply("Assistant: **ja** gerne") == "ja gerne"


def test_split_reply():
    assert split_reply("eins\n---\nzwei") == ["eins", "zwei"]
    assert split_reply("nur eine") == ["nur eine"]
    assert split_reply("a\n---\nb\n---\nc\n---\nd") == ["a", "b", "c\nd"]


def test_sample_delay_within_bounds():
    rng = random.Random(1)
    values = [sample_delay(45, 10800, rng) for _ in range(1000)]
    assert all(45 <= v <= 10800 for v in values)
    # log-uniform: Median deutlich unter dem arithmetischen Mittel der Grenzen
    assert sorted(values)[500] < 3000
    assert sample_delay(100, 50) == 100


def test_quiet_hours():
    assert in_quiet_hours(23, 23, 7)
    assert in_quiet_hours(3, 23, 7)
    assert not in_quiet_hours(7, 23, 7)
    assert not in_quiet_hours(12, 23, 7)
    assert in_quiet_hours(13, 12, 14)
    assert not in_quiet_hours(3, 5, 5)

    rng = random.Random(1)
    night = datetime(2026, 9, 27, 23, 30)
    moved = postpone_quiet_hours(night, 23, 7, rng)
    assert moved.date() == datetime(2026, 9, 28).date() and 7 <= moved.hour <= 8

    early = datetime(2026, 9, 27, 3, 0)
    moved = postpone_quiet_hours(early, 23, 7, rng)
    assert moved.date() == early.date() and 7 <= moved.hour <= 8

    day = datetime(2026, 9, 27, 14, 0)
    assert postpone_quiet_hours(day, 23, 7, rng) == day


def test_typing_duration_capped():
    assert 2 <= typing_duration("hi") < 5
    assert typing_duration("x" * 10000) == 30.0


def test_build_messages_merges_roles_and_skips_notes():
    history = [
        {"sender": "them", "text": "hallo"},
        {"sender": "them", "text": "bist du da?"},
        {"sender": "note", "text": "intern"},
        {"sender": "ai", "text": "ja"},
        {"sender": "me", "text": "moment"},
        {"sender": "them", "text": "schick geld"},
    ]
    msgs = build_messages("Du bist Gerda.", history, now=datetime(2026, 9, 27, 12, 0))
    assert msgs[0]["role"] == "system" and "Du bist Gerda." in msgs[0]["content"]
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant", "user", "system"]
    assert msgs[1]["content"] == "hallo\nbist du da?"
    assert msgs[2]["content"] == "ja\nmoment"
    assert "Sonntag, 27.09.2026 12:00" in msgs[-1]["content"]


def test_system_prompt_is_stable_for_caching():
    """Der Anfang der Anfrage darf sich nicht mit der Uhrzeit oder Regieanweisung ändern."""
    history = [{"sender": "them", "text": "hallo"}]
    a = build_messages("Persona", history, now=datetime(2026, 9, 27, 12, 0))
    b = build_messages("Persona", history, now=datetime(2026, 9, 28, 18, 30), instruction="frag nach dem Hund")
    assert a[:-1] == b[:-1]
    assert "Regieanweisung" in b[-1]["content"] and "frag nach dem Hund" in b[-1]["content"]


def test_typing_plan():
    rng = random.Random(3)
    for _ in range(200):
        plan = typing_plan("hallo, wie geht das denn mit dem bitcoin?", rng)
        assert plan[-1][0] == "typing" and all(sec > 0 for _, sec in plan)
    assert typing_plan("x" * 500, rng, instant=True) == [("typing", 3.0)]
    kinds = {tuple(k for k, _ in typing_plan("x" * 200, random.Random(i))) for i in range(100)}
    assert ("typing", "pause", "typing") in kinds  # zögert manchmal


def test_parse_analysis_is_robust():
    raw = """Klar! ```json
    {"scam_type": "Romance", "stage": 9, "summary": "x", "frustration": "12",
     "keywords": ["Liebe", "liebe", "#Schatz"],
     "best_of": [{"sender": "Scammer", "text": "ich liebe dich seit gestern"}, {"text": ""}]}
    ```"""
    a = parse_analysis(raw)
    assert a["stage"] == 6 and a["frustration"] == 10
    assert a["keywords"] == ["Liebe", "Schatz"]
    assert a["best_of"] == [{"sender": "them", "text": "ich liebe dich seit gestern"}]


def test_lexicon_and_cloud():
    counts = dict(lexicon_counts(["Investiere in Bitcoin!", "Garantiert 300% Rendite", "hallo"]))
    assert counts["Bitcoin / Krypto"] == 1 and counts["Rendite / Gewinn"] == 1
    assert counts["Vertrauen / Seriös"] == 1
    cloud = keyword_cloud([{"keywords": ["Bitcoin", "Rendite"]}, {"keywords": ["bitcoin"]}])
    assert cloud[0] == ("Bitcoin", 2)


def test_response_times():
    msgs = [
        {"sender": "them", "ts": 0}, {"sender": "ai", "ts": 600},
        {"sender": "them", "ts": 660}, {"sender": "note", "ts": 700}, {"sender": "me", "ts": 1860},
    ]
    t = response_times(msgs)
    assert t["scammer"] == 60 and t["bait"] == 900


def test_charts_render():
    labels, series = daily_series([{"day": "2026-09-27", "sender": "them", "n": 3},
                                   {"day": "2026-09-27", "sender": "ai", "n": 2}], 3, date(2026, 9, 27))
    svg = grouped_bars(labels, series, "Test")
    assert "<svg" in svg and "Scammer: 3" in svg and "Köder (KI/du): 2" in svg and "Als Tabelle" in svg
    assert "bar s-them" in svg and "bar s-ai" in svg
    assert "Leer" in hbars([], "T", "s-ai", "Leer")
    assert "width:100.0%" in hbars([("a", 10, "10"), ("b", 5, "5")], "T", "s-ai", "")
