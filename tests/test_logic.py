import random
from datetime import date, datetime

import pytest

from tarpit.analysis import keyword_cloud, lexicon_counts, parse_analysis, response_times, word_counts
from tarpit.charts import daily_series, grouped_bars, hbars, word_cloud
from tarpit.prompts import build_messages
from tarpit.referrals import extract_candidates
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



def test_extract_candidates():
    found = extract_candidates(
        "Schreib meinem Manager @AnnaInvest oder https://t.me/crypto_boss, Tel +44 7911 123456. "
        "Gruppe: t.me/+abc t.me/joinchat/xyz, Bot @helpbot, Mail a@b.de, Betrag 1.000.000"
    )
    assert [(c.kind, c.value) for c in found] == [
        ("username", "annainvest"), ("username", "crypto_boss"), ("phone", "+447911123456"),
    ]
    assert extract_candidates("0049 171 2345678")[0].value == "+491712345678"
    assert extract_candidates("hallo, wie gehts?") == []


def test_word_counts_and_cloud():
    msgs = [
        {"sender": "them", "text": "Bitcoin Bitcoin Rendite [Foto] und die Rendite"},
        {"sender": "ai", "text": "bitcoin? brille brille brille"},
        {"sender": "note", "text": "Bitcoin intern"},
    ]
    counts = word_counts(msgs)
    assert ("bitcoin", 2, 1) in counts and ("brille", 0, 3) in counts
    assert not any(w in ("und", "die", "foto", "intern") for w, *_ in counts)
    html = word_cloud(counts, "leer")
    assert "w-them" in html and "w-ai" in html and "Rendite".lower() in html
    assert html.index("bitcoin") >= 0 and word_cloud([], "leer").endswith("leer</p>")
    assert word_cloud(counts, "x") == word_cloud(counts, "x")  # stabile Anordnung



def test_prepare_upload_strips_metadata(tmp_path):
    import io

    from PIL import Image

    from tarpit.media import prepare_upload

    img = Image.new("RGB", (3000, 2000), "orange")
    exif = Image.Exif()
    exif[0x010F] = "Handyhersteller"          # Make
    exif[0x8825] = {2: (52.0, 31.0, 12.0)}    # GPS-Info
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    assert b"Handyhersteller" in buf.getvalue()

    out = prepare_upload(buf.getvalue())
    with Image.open(io.BytesIO(out)) as cleaned:
        assert cleaned.format == "JPEG" and max(cleaned.size) == 1600
        assert len(cleaned.getexif()) == 0
    assert b"Handyhersteller" not in out
    with pytest.raises(ValueError):
        prepare_upload(b"kein bild")


def test_image_markers_and_photo_request():
    from tarpit.media import asks_for_photo, image_marker_ids, strip_image_markers

    assert image_marker_ids("[BILD:3] hier\n[ bild : 12 ]") == [3, 12]
    assert strip_image_markers("[BILD:3]  da ist sie") == "da ist sie"
    assert asks_for_photo("Kannst du mir ein Foto schicken?") and asks_for_photo("send me a pic")
    assert not asks_for_photo("Wie geht es dir?")


def test_usage_report_projection(tmp_path):
    from tarpit.db import Database
    from tarpit.usage import usage_report

    db = Database(tmp_path / "t.db")
    today = date(2026, 9, 27)
    for back in range(1, 8):  # letzte 7 Tage je 1000 Tokens und $0.01
        ts = datetime(2026, 9, 27 - back, 12).timestamp()
        db.add_usage("m", "reply", 800, 100, 200, 0.01, ts=ts)
    db.add_usage("m", "analysis", 400, 0, 100, 0.005, ts=datetime(2026, 9, 27, 9).timestamp())
    report = usage_report(db, db.settings(), today=today)
    assert report["today_tokens"] == 500 and report["avg_tokens"] == 1000
    # 26 Tage bis gestern ergeben hier 7000 + heute 500 + Rest von heute (500) + 3 Resttage * 1000
    assert report["mtd_tokens"] == 7500 and report["projection_tokens"] == 7500 + 500 + 3000
    assert report["calls"]["reply"] == 7 and report["calls"]["analysis"] == 1
    assert report["per_reply"] == round((7 * 800 + 400 + 7 * 200 + 100) / 7)
    assert "s-c1" in report["chart_tokens"] and "s-c2" in report["chart_tokens"]
