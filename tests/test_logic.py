import random
from datetime import datetime

from tarpit.prompts import build_messages
from tarpit.safety import check_reply, clean_reply, split_reply
from tarpit.timing import in_quiet_hours, postpone_quiet_hours, sample_delay, typing_duration


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
    assert [m["role"] for m in msgs[1:]] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == "hallo\nbist du da?"
    assert msgs[2]["content"] == "ja\nmoment"
