"""Deadline reminders on Telegram (same bot as the tender alerts).

Obligations live in config/obligations.json (in git, father-editable): monthly items with a
day of month, and one-off items with a date (bank guarantees, contract ends, DSC). `build`
refreshes the one-off items from the catalog and keeps every `verified` flag and manual edit.

    uv run python -m apps.reminders build            # refresh one-off items from data
    uv run python -m apps.reminders upcoming [--days 60]
    uv run python -m apps.reminders run [--dry-run]  # daily (launchd): send what is due
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OBLIGATIONS = REPO / "config" / "obligations.json"
STATE_DB = Path(os.environ.get("WORK_DIR", REPO / "work")) / "reminders.db"
ICON = {"wages": "💰", "gstr1": "🧾", "gstr3b": "🏛️", "pf": "👷", "esi": "🏥", "rabills": "📑",
        "bg": "🏦", "end": "📆", "close": "🏁", "dsc": "🔐", "manual": "📌"}
RULE = "━━━━━━━━━━━━━━━━━━━━"
MONTHLY_LEADS = (3, 0)        # days before a monthly deadline
ONE_OFF_LEADS = (60, 30, 7)   # days before an expiry

MONTHLY = [  # from kb/CALENDAR.md (evidence there); father verifies
    {"id": "wages", "day": 4, "title_en": "Pay workers' wages for last month",
     "title_or": "ଶ୍ରମିକଙ୍କ ଗତ ମାସର ମଜୁରି ଦେବା ଶେଷ ତାରିଖ",
     "evidence": "NALCO email 07-08-2026: wages by 4th of each month",
     "label": "VERIFIED"},
    {"id": "gstr1", "day": 11, "title_en": "File GSTR-1 (sales return) for last month",
     "title_or": "ଗତ ମାସର GSTR-1 ରିଟର୍ଣ୍ଣ ଦାଖଲ ଶେଷ ତାରିଖ",
     "evidence": "GST portal ARN emails 11-10-2026, 10-11-2026", "label": "VERIFIED"},
    {"id": "pf", "day": 15, "title_en": "File PF return (ECR) and pay PF for last month",
     "title_or": "ଗତ ମାସର PF (ECR) ଦାଖଲ ଓ ଜମା ଶେଷ ତାରିଖ",
     "evidence": "PF payment receipts: paid 14th–15th every month May–Sep 2026; EPFO rule",
     "label": "VERIFIED"},
    {"id": "esi", "day": 15, "title_en": "Pay ESI contribution for last month",
     "title_or": "ଗତ ମାସର ESI ଜମା ଶେଷ ତାରିଖ",
     "evidence": "ESIC online challan Aug-2026 paid 15-09-2026; ESIC rule", "label": "VERIFIED"},
    {"id": "gstr3b", "day": 20, "title_en": "File GSTR-3B and pay GST for last month",
     "title_or": "ଗତ ମାସର GSTR-3B ଦାଖଲ ଓ GST ଜମା ଶେଷ ତାରିଖ",
     "evidence": "GST portal ARN emails: filed on the 20th most months",
     "label": "VERIFIED"},
    {"id": "rabills", "day": 5, "title_en": "Submit monthly RA bill (with e-invoice) for each "
     "running contract", "title_or": "ସବୁ ଚାଲୁ କାମର ମାସିକ RA ବିଲ୍ (e-invoice ସହ) ଦାଖଲ",
     "evidence": "RA bills / e-invoices roughly monthly per contract", "label": "INFERRED"},
]


# ---------- dates ----------

def monthly_due(day: int, today: date) -> date:
    """Next occurrence of `day` on or after today (clamped to month length)."""
    def on(y, m):
        return date(y, m, min(day, calendar.monthrange(y, m)[1]))
    d = on(today.year, today.month)
    if d < today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        d = on(y, m)
    return d


def period_end(text: str) -> date | None:
    """Contract end from a folder period: '04.23 TO 03.27', '7.25 TO 6.27', '1.6.21 TO 31.5.23'.
    Year-only forms ('25-27', '2026-28') give no month → None (ask father)."""
    if m := re.search(r"TO\s*(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b", text, re.IGNORECASE):
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        y = y + 2000 if y < 100 else y
        try:
            return date(y, mo, d)
        except ValueError:
            return None
    if m := re.search(r"TO\s*(\d{1,2})\.(\d{2})\b", text, re.IGNORECASE):
        mo, y = int(m[1]), 2000 + int(m[2])
        if 1 <= mo <= 12:
            return date(y, mo, calendar.monthrange(y, mo)[1])
    return None


# ---------- obligations ----------

def from_catalog(db: Path, today: date) -> list[dict]:
    """One-off items found in the data: live bank guarantees, contract ends, DSC expiry."""
    out: list[dict] = []
    if not db.exists():
        return out
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    best: dict[str, dict] = {}
    for w in c.execute("SELECT ref, title, status, period, folders, facts FROM works"
                       " WHERE status='running'"):
        f = json.loads(w["facts"])
        for b in f["bank_guarantees"]:
            if b["expiry"] and b["expiry"] >= today.isoformat() and \
                    b["expiry"] > best.get(b["bg_no"], {}).get("date", ""):
                amt = f" ₹{b['amount_paise'] / 100:,.0f}" if b.get("amount_paise") else ""
                best[b["bg_no"]] = {
                    "id": f"bg:{b['bg_no']}", "date": b["expiry"],
                    "title_en": f"Bank guarantee {b['bg_no']}{amt} ({w['ref']}) expires",
                    "title_or": f"ବ୍ୟାଙ୍କ ଗ୍ୟାରେଣ୍ଟି {b['bg_no']} ({w['ref']}) ର ମିଆଦ ଶେଷ ହେଉଛି",
                    "evidence": f"{b['evidence']} (OCR — check the PDF)", "label": "VERIFIED"}
        names = " ".join(Path(p).name for p in json.loads(w["folders"]))
        end = period_end(names)
        if end:
            out.append({"id": f"end:{w['ref']}", "date": end.isoformat(),
                        "title_en": f"Contract {w['ref']} ends — plan extension or new bid",
                        "title_or": f"{w['ref']} କଣ୍ଟ୍ରାକ୍ଟ ଶେଷ ହେଉଛି — ମିଆଦ ବୃଦ୍ଧି ବା ନୂଆ ଟେଣ୍ଡର ଦେଖନ୍ତୁ",
                        "evidence": f"folder name '{names[:80]}'", "label": "INFERRED"})
            # NALCO rule (GCC cl. 38.1.2; letter on OMC-5577, 02-01-2026): workers' full & final
            # payment and the final bill within 3 months of the end, else NALCO pays them + 25 %
            out.append({"id": f"close:{w['ref']}",
                        "date": (end + timedelta(days=90)).isoformat(),
                        "title_en": f"{w['ref']}: last day for workers' full & final payment and "
                                    "the final bill (3 months after contract end)",
                        "title_or": f"{w['ref']}: ଶ୍ରମିକଙ୍କ ପୂରା ଓ ଶେଷ ଟଙ୍କା ଓ ଫାଇନାଲ୍ ବିଲ୍‌ର "
                                    "ଶେଷ ଦିନ (କାମ ଶେଷର ୩ ମାସ ପରେ)",
                        "evidence": "NALCO letter 02-01-2026 (GCC cl. 38.1.2); end date from "
                                    f"folder name '{names[:60]}'", "label": "INFERRED"})
    # second pass: any document carrying a BG number and an expiry date, wherever it lies
    ref_rx = re.compile(r"\b(OMC|MC)\s*-?\s*(\d{4})\b|(ASH MECH[^/]*)", re.IGNORECASE)
    for fid, bg, exp in c.execute(
            "SELECT b.file_id, b.normalized, MAX(e.normalized) FROM entities b JOIN entities e"
            " ON e.file_id=b.file_id AND e.kind='expiry_date' WHERE b.kind='bg_no'"
            " GROUP BY b.file_id, b.normalized"):
        if not re.fullmatch(r"\d{7}BG[0-9A-Z]{5,9}", bg) or exp < today.isoformat() or \
                exp <= best.get(bg, {}).get("date", ""):
            continue
        path = (c.execute("SELECT path FROM locations WHERE file_id=? ORDER BY origin='fs' DESC"
                          " LIMIT 1", (fid,)).fetchone() or [""])[0]
        m = ref_rx.search(path)
        if not m:  # look at other documents carrying the same BG number
            for (p2,) in c.execute(
                    "SELECT l.path FROM entities x JOIN locations l ON l.file_id=x.file_id"
                    " WHERE x.kind='bg_no' AND x.normalized=?", (bg,)):
                if m := ref_rx.search(p2):
                    break
        ref = (f"{m[1].upper()}-{m[2]}" if m and m[1] else (m[3].strip() if m else "contract ?"))
        best[bg] = {"id": f"bg:{bg}", "date": exp,
                    "title_en": f"Bank guarantee {bg} ({ref}) expires",
                    "title_or": f"ବ୍ୟାଙ୍କ ଗ୍ୟାରେଣ୍ଟି {bg} ({ref}) ର ମିଆଦ ଶେଷ ହେଉଛି",
                    "evidence": f"F:{fid} {path[-70:]} (OCR — check the PDF)",
                    "label": "VERIFIED"}
    out += list(best.values())
    out.append({"id": "dsc:owner", "date": "2027-11-10",
                "title_en": "Owner's digital signature (DSC) expires",
                "title_or": "ମାଲିକଙ୍କ ଡିଜିଟାଲ ସିଗ୍ନେଚର (DSC) ର ମିଆଦ ଶେଷ ହେଉଛି",
                "evidence": "folder 'P.F (SMELTER)/DSC REG/19.11.25 TO 10.11.27'",
                "label": "INFERRED"})
    return out


def in_hours(window: str, now: datetime) -> bool:
    """'8-21' → True from 08:00 to 21:59. Reminders are checked every hour; this keeps them
    out of the night, and a dropped GitHub run is simply caught by the next one."""
    lo, hi = (int(x) for x in window.split("-"))
    return lo <= now.hour <= hi


def kind(item_id: str) -> str:
    """'bg:…' → 'bg'; hand-added 'manual:end:OMC-5916' → 'end' (same icon/source as found ones)."""
    parts = item_id.split(":")
    return parts[1] if parts[0] == "manual" and len(parts) > 2 else parts[0]


def merge(old: dict, monthly: list[dict], one_off: list[dict]) -> dict:
    """New data wins on dates/titles; father's `verified`, `disabled`, notes and any manual
    items (ids starting 'manual:') are kept."""
    keep = {i["id"]: i for i in old.get("monthly", []) + old.get("one_off", [])}

    def carry(item):
        prev = keep.get(item["id"], {})
        for k in ("verified", "disabled", "note"):
            if k in prev:
                item[k] = prev[k]
        item.setdefault("verified", False)
        return item

    manual = [i for i in old.get("one_off", []) if i["id"].startswith("manual:")]
    return {"note": "Edit freely. Set verified=true after father checks an item; "
                    "disabled=true to stop its reminders. Rebuild: python -m apps.reminders build",
            "monthly": [carry(dict(m)) for m in monthly],
            "one_off": sorted([carry(dict(o)) for o in one_off] + manual,
                              key=lambda i: i["date"])}


def load() -> dict:
    return json.loads(OBLIGATIONS.read_text()) if OBLIGATIONS.exists() else {}


# ---------- reminders ----------

def due_items(obl: dict, today: date) -> list[dict]:
    """Every (item, due date) with its reminder tier; one reminder per item per tier."""
    out = []
    for m in obl.get("monthly", []):
        if not m.get("disabled"):
            out.append({**m, "due": monthly_due(m["day"], today), "leads": MONTHLY_LEADS})
    for o in obl.get("one_off", []):
        d = date.fromisoformat(o["date"])
        if not o.get("disabled") and d >= today:
            out.append({**o, "due": d, "leads": ONE_OFF_LEADS})
    return out


def to_send(obl: dict, today: date, sent: set[str]) -> list[tuple[dict, int, str]]:
    """Items whose reminder window is open and not yet reminded. If the Mac was off and
    several tiers passed, only the nearest one is sent (no burst of old reminders)."""
    out = []
    for it in due_items(obl, today):
        left = (it["due"] - today).days
        open_tiers = [lead for lead in it["leads"] if left <= lead]
        if not open_tiers:
            continue
        tier = min(open_tiers)
        key = f"{it['id']}|{it['due'].isoformat()}|{tier}"
        if key not in sent and not any(f"{it['id']}|{it['due'].isoformat()}|{t}" in sent
                                       for t in it["leads"] if t <= tier):
            out.append((it, left, key))
    return out


def digest(obl: dict, today: date, days: int = 7) -> str:
    """Daily status card: what is due in the next week, else the next deadline."""
    items = sorted((i for i in due_items(obl, today) if not i.get("disabled")),
                   key=lambda i: i["due"])
    week = [i for i in items if (i["due"] - today).days <= days]
    lines = [f"☀️ <b>TODAY'S STATUS</b> · {today.strftime('%a, %d %b %Y')}", RULE]
    if week:
        lines.append(f"📋 <b>Due in the next {days} days</b>")
        for i in week:
            left = (i["due"] - today).days
            dot = "🔴" if left <= 2 else ("🟠" if left <= 7 else "🟢")
            when = "today" if left == 0 else f"{left} day{'s' if left != 1 else ''}"
            lines.append(f"{dot} {ICON.get(kind(i['id']), '📌')} {html.escape(i['title_en'])}"
                         f" — <b>{i['due'].strftime('%d %b')}</b> ({when})")
    else:
        lines.append("✅ <b>Nothing due this week</b>")
        if items:
            n = items[0]
            lines.append(f"Next: {ICON.get(kind(n['id']), '📌')} {html.escape(n['title_en'])}"
                         f" — <b>{n['due'].strftime('%d %b')}</b>")
    lines += ["", "<i>ଆଜିର ସ୍ଥିତି — ଆଗାମୀ ୭ ଦିନର ଶେଷ ତାରିଖ</i>"]
    return "\n".join(lines)


def message(it: dict, left: int) -> str:
    """Card layout: icon + English title, Odia title, due date, urgency, verification note."""
    def e(s: str) -> str:
        return html.escape(s, quote=False)  # keep apostrophes readable

    icon = ICON.get(kind(it["id"]), "📌")
    dot = "🔴" if left <= 2 else ("🟠" if left <= 7 else "🟢")
    tail = "due today" if left == 0 else ("1 day left" if left == 1 else f"{left} days left")
    lines = ["⏰ <b>REMINDER</b>", RULE, f"{icon} <b>{e(it['title_en'])}</b>",
             f"<i>{e(it['title_or'])}</i>", "",
             f"📅 <b>Due</b>     {it['due'].strftime('%a, %d %b %Y')}", f"{dot} <b>{tail}</b>"]
    score, why = confidence(it)
    bar = "🟩" * (score // 20) + "⬜" * (5 - score // 20)
    lines += ["", f"🎯 <b>Confidence</b>  {score}%  {bar}", f"<i>   {why}</i>"]
    return "\n".join(lines)


CONFIDENCE = {  # label → (score, plain reason)
    "VERIFIED": (90, "seen in official documents / emails"),
    "INFERRED": (70, "worked out from past files"),
    "ASSUMED": (50, "general rule, not yet seen in our files"),
}


def confidence(it: dict) -> tuple[int, str]:
    if it.get("verified"):
        return 100, "confirmed by father"
    return CONFIDENCE.get(it.get("label", ""), (50, "source unclear"))


def bot_prefix(env: dict) -> str:
    """Reminder bot if configured, else fall back to the tender bot."""
    return "REMINDER" if env.get("REMINDER_BOT_TOKEN") and env.get("REMINDER_CHAT_IDS") \
        else "TELEGRAM"


SOURCE_OR = {  # how we know, in simple Odia, for the father's check sheet
    "wages": "ନାଲକୋ GM (ମେକାନିକାଲ) ଙ୍କ ଇମେଲ, 07-08-2026",
    "gstr1": "GST ପୋର୍ଟାଲର ଇମେଲ (ଅକ୍ଟୋବର ଓ ନଭେମ୍ବର 2026)",
    "pf": "PF ରସିଦ: ମେ–ସେପ୍ଟେମ୍ବର 2026 ପ୍ରତି ମାସ 14–15 ତାରିଖରେ ଜମା",
    "esi": "ESIC ଚାଲାଣ: ଅଗଷ୍ଟ 2026 ର ଜମା 15-09-2026",
    "gstr3b": "GST ପୋର୍ଟାଲ ଇମେଲ: ଅଧିକାଂଶ ମାସ 20 ତାରିଖ (ଦୁଇ ଥର ବିଳମ୍ବ)",
    "rabills": "ପୁରୁଣା ଫାଇଲ: ପ୍ରତି ମାସ RA ବିଲ୍",
    "bg": "ବ୍ୟାଙ୍କ ଗ୍ୟାରେଣ୍ଟି କାଗଜ (ସ୍କାନ — PDF ଦେଖି ମିଳାନ୍ତୁ)",
    "end": "କଣ୍ଟ୍ରାକ୍ଟ ଫୋଲ୍ଡରର ନାମ",
    "close": "NALCO ଚିଠି 02-01-2026: କାମ ଶେଷର ୩ ମାସ ଭିତରେ, ନହେଲେ ୨୫% ଅଧିକ କଟେ",
    "dsc": "DSC ଫୋଲ୍ଡରର ନାମ",
}


def check_sheet(obl: dict, today: date) -> str:
    """Printable Odia/English page: every obligation with source and ✔/✘ boxes."""
    e = lambda s: html.escape(str(s), quote=False)
    box = "☐ ଠିକ୍ &nbsp; ☐ ଭୁଲ"
    rows_m = "".join(
        f"<tr><td class='c'>{m['day']}</td><td>{e(m['title_or'])}<div class='en'>"
        f"{e(m['title_en'])}</div></td><td class='src'>{e(SOURCE_OR.get(m['id'], ''))}</td>"
        f"<td class='bx'>{'✔ ଯାଞ୍ଚ ହୋଇଛି' if m.get('verified') else box}</td><td></td></tr>"
        for m in obl.get("monthly", []))
    rows_o = "".join(
        f"<tr><td class='c'>{date.fromisoformat(o['date']).strftime('%d-%m-%Y')}</td>"
        f"<td>{e(o['title_or'])}<div class='en'>{e(o['title_en'])}</div></td>"
        f"<td class='src'>{e(o.get('source_or') or SOURCE_OR.get(kind(o['id']), ''))}</td>"
        f"<td class='bx'>{'✔ ଯାଞ୍ଚ ହୋଇଛି' if o.get('verified') else box}</td><td></td></tr>"
        for o in obl.get("one_off", []) if o["date"] >= today.isoformat())
    return f"""<!DOCTYPE html><html lang="or"><head><meta charset="utf-8">
<title>ମାସିକ ଶେଷ ତାରିଖ — ଯାଞ୍ଚ ପାଇଁ</title><style>
@page {{ size: A4 landscape; margin: 12mm; }}
body {{ font-family: "Noto Sans Oriya", sans-serif; font-size: 12pt; line-height: 1.45; }}
h1 {{ font-size: 17pt; margin: 0 0 2pt; }} h2 {{ font-size: 14pt; margin: 12pt 0 4pt;
  break-after: avoid; }}
table {{ border-collapse: collapse; width: 100%; }} tr {{ break-inside: avoid; }}
td, th {{ border: 1px solid #888; padding: 3pt 6pt; vertical-align: top; }}
th {{ background: #e8eef5; text-align: left; }} .c {{ white-space: nowrap; text-align: center;
  font-family: "Noto Sans", Helvetica, sans-serif; }}
.en {{ font-family: "Noto Sans", Helvetica, sans-serif; font-size: 9.5pt; color: #444; }}
.src {{ font-size: 10.5pt; color: #333; width: 24%; }} .bx {{ white-space: nowrap; width: 12%; }}
.sub {{ font-size: 10.5pt; color: #444; }} .lines {{ border-bottom: 1px solid #999; height: 18pt; }}
</style></head><body>
<h1>ଆମର ମାସିକ ଶେଷ ତାରିଖ ଓ ମିଆଦ — ଦୟାକରି ଯାଞ୍ଚ କରନ୍ତୁ</h1>
<div class="sub">AMC deadline calendar for verification · ପ୍ରସ୍ତୁତି {today.strftime('%d-%m-%Y')} ·
ପ୍ରତ୍ୟେକ ଧାଡ଼ିରେ "ଠିକ୍" ବା "ଭୁଲ" ଟିକ୍ କରନ୍ତୁ। ଭୁଲ ହେଲେ ଠିକ୍ ତାରିଖ ଶେଷ ସ୍ତମ୍ଭରେ ଲେଖନ୍ତୁ।
ଏହି ତାରିଖର କିଛି ଦିନ ଆଗରୁ ଫୋନ୍‌ରେ (Telegram) ମନେ ପକାଇବା ସନ୍ଦେଶ ଆସିବ।</div>
<h2>୧. ପ୍ରତି ମାସ (ମାସର ତାରିଖ)</h2>
<table><tr><th>ତାରିଖ</th><th>କାମ</th><th>କିପରି ଜାଣିଲୁ</th><th>ଠିକ୍ କି?</th><th>ସଂଶୋଧନ</th></tr>
{rows_m}</table>
<h2>୨. ଗୋଟିଏ ଥର (ମିଆଦ ଶେଷ)</h2>
<table><tr><th>ତାରିଖ</th><th>କାମ</th><th>କିପରି ଜାଣିଲୁ</th><th>ଠିକ୍ କି?</th><th>ସଂଶୋଧନ</th></tr>
{rows_o}</table>
<h2>୩. ଆଉ କିଛି ଶେଷ ତାରିଖ ଅଛି କି? (ଯେପରି: ଲେବର ଲାଇସେନ୍ସ ନବୀକରଣ, TDS, ପ୍ରଫେସନାଲ ଟ୍ୟାକ୍ସ,
ଇନ୍ସୁରେନ୍ସ, ଗାଡ଼ି କାଗଜ)</h2>
<div class="lines"></div><div class="lines"></div><div class="lines"></div><div class="lines"></div>
</body></html>"""


def state() -> sqlite3.Connection:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(STATE_DB)
    c.execute("CREATE TABLE IF NOT EXISTS sent (key TEXT PRIMARY KEY, at TEXT)")
    return c


def main(argv: list[str] | None = None) -> int:
    from apps.bidwatch import IST, channels_configured, load_env, send, setup

    ap = argparse.ArgumentParser(prog="reminders")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    u = sub.add_parser("upcoming")
    u.add_argument("--days", type=int, default=60)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--hours", default="",
                   help="send only between these IST hours, e.g. 8-21 (GitHub runs it hourly)")
    sub.add_parser("sheet", help="printable check sheet for father → kb/calendar/")
    sub.add_parser("setup", help="register chats that pressed Start on the reminder bot")
    a = ap.parse_args(argv)
    today = datetime.now(IST).date()
    if a.cmd == "build":
        obl = merge(load(), MONTHLY, from_catalog(REPO / "work" / "catalog.db", today))
        OBLIGATIONS.parent.mkdir(exist_ok=True)
        OBLIGATIONS.write_text(json.dumps(obl, indent=2, ensure_ascii=False) + "\n")
        print(f"{OBLIGATIONS}: {len(obl['monthly'])} monthly, {len(obl['one_off'])} one-off")
        return 0
    obl = load()
    if a.cmd == "sheet":
        import subprocess
        out = REPO / "kb" / "calendar"
        out.mkdir(parents=True, exist_ok=True)
        h = out / "calendar_check.html"
        h.write_text(check_sheet(obl, today), encoding="utf-8")
        pdf = out / "calendar_check.pdf"
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-first-run",
                        f"--user-data-dir={REPO / 'work' / 'tmp' / 'chrome_pdf'}",
                        "--no-pdf-header-footer", f"--print-to-pdf={pdf}", h.as_uri()],
                       capture_output=True, timeout=120, check=False)
        print(pdf if pdf.exists() else "PDF not produced (Chrome missing?)", h, sep="\n")
        return 0
    if a.cmd == "upcoming":
        for it in sorted(due_items(obl, today), key=lambda i: i["due"]):
            if (it["due"] - today).days <= a.days:
                print(f"{it['due']}  {'✔' if it.get('verified') else ' '}  {it['title_en']}")
        return 0
    env = load_env()
    if a.cmd == "setup":
        print(*setup(env, "REMINDER", "✅ <b>AMC reminders are connected</b>\n" + RULE +
                     "\nYou'll get reminders here before every deadline: wages, PF, ESI, GST, "
                     "RA bills, bank guarantees and other expiries."), sep="\n")
        return 0
    from apps.bidwatch import HOSTED_MSG, hosted_elsewhere

    if not a.dry_run and hosted_elsewhere(env):
        print(HOSTED_MSG, file=sys.stderr)
        return 4
    if a.hours and not in_hours(a.hours, datetime.now(IST)):
        print(f"outside sending hours {a.hours} IST — nothing sent")
        return 0
    prefix = bot_prefix(env)
    if not a.dry_run and not channels_configured(env, prefix):
        print("No Telegram recipient configured (python -m apps.reminders setup)",
              file=sys.stderr)
        return 3
    c = state()
    sent = {k for (k,) in c.execute("SELECT key FROM sent")}
    for it, left, key in to_send(obl, today, sent):
        res = send(message(it, left), env, dry_run=a.dry_run, prefix=prefix)
        print(key, res)
        if not a.dry_run and any(": 200" in x for x in res):
            c.execute("INSERT OR IGNORE INTO sent VALUES (?, ?)", (key, today.isoformat()))
            c.commit()
    # once a day: a short status, so a silent day still shows the bot is alive
    dkey = f"digest|{today.isoformat()}"
    if a.hours and dkey not in sent and datetime.now(IST).hour >= 9:
        res = send(digest(obl, today), env, dry_run=a.dry_run, prefix=prefix)
        print(dkey, res)
        if not a.dry_run and any(": 200" in x for x in res):
            c.execute("INSERT OR IGNORE INTO sent VALUES (?, ?)", (dkey, today.isoformat()))
            c.commit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
