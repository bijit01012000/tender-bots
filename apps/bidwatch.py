"""NALCO bid watcher (ADR-009/010): new NALCO tenders → Telegram (name of work + deadline on top).

Source: nalcoindia.com/tenders/active-tenders/ (public; robots.txt allows). One polite request
per run. The watcher only informs: it never bids, logs in anywhere, or messages third parties.

    uv run python -m apps.bidwatch run [--dry-run]   # fetch, store, notify new/changed
    uv run python -m apps.bidwatch list              # bids in the local database
    uv run python -m apps.bidwatch test              # send one test message

Config via `.env` (never committed): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_IDS (comma-separated),
optional CALLMEBOT_PHONE + CALLMEBOT_APIKEY, BIDWATCH_UNITS (regex), BIDWATCH_MIN_SCORE.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("WORK_DIR", REPO / "work"))
SOURCE_URL = "https://nalcoindia.com/tenders/active-tenders/"
USER_AGENT = "ContractorOS-bidwatch/0.1 (polite tender watcher; 1 request per run)"
IST = timezone(timedelta(hours=5, minutes=30))


def _ssl_context():
    """Fully verified TLS that works on macOS and Linux alike: Mozilla roots (certifi) plus the
    public intermediate certificate NALCO's server forgets to send (DigiCert 'RapidSSL TLS RSA
    CA G1', config/certs/). Nothing is skipped; the chain still ends in a trusted root."""
    import ssl

    import certifi

    ctx = ssl.create_default_context(cafile=certifi.where())
    for pem in sorted((REPO / "config" / "certs").glob("*.pem")):
        ctx.load_verify_locations(cafile=str(pem))
    return ctx


DEFAULT_UNITS = r"."  # all units; narrow with BIDWATCH_UNITS=smelter|cpp
MAX_PDF_BYTES = 25 * 2**20
REMIND_DAYS = (3, 1)

# Past work used to rank similarity: config/amc_profile.json "past_work" (kept out of code).


# ---------- parsing ----------

@dataclass
class Tender:
    work: str
    unit: str
    tender_no: str = ""
    gem_bid: str = ""
    nalco_ref: str = ""  # OMC-nnnn / MC-nnnn
    tender_type: str = ""
    published: str = ""  # ISO date
    closes: str = ""  # ISO datetime (IST) or date
    docs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return self.gem_bid or f"{self.unit}|{self.tender_no or self.work}"[:200]


GEM_RX = re.compile(r"GEM\s*/\s*(20\d\d)\s*/\s*B\s*/+\s*(\d{6,8})", re.IGNORECASE)
REF_RX = re.compile(r"\b(OMC|MC)\s*[-.]?\s*(\d{4})\b", re.IGNORECASE)
DATE_RX = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})")
TIME_RX = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def _lines(page: str) -> list[str]:
    page = re.sub(r"<script.*?</script>|<style.*?</style>", "", page, flags=re.DOTALL | re.IGNORECASE)
    page = re.sub(r'<a\s[^>]*href="([^"]+)"[^>]*>', r"\n[LINK \1]\n", page, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "\n", page))
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def _date(s: str) -> str:
    m = DATE_RX.search(s)
    if not m:
        return ""
    try:
        return datetime(int(m[3]), int(m[2]), int(m[1])).date().isoformat()
    except ValueError:
        return ""


def parse(page: str) -> list[Tender]:
    """Tenders from NALCO's active-tenders HTML. Tolerant of format drift; skips junk."""
    lines = _lines(page)
    starts = [i for i, ln in enumerate(lines) if ln.rstrip(":").strip().lower() == "unit"]
    out = []
    for n, i in enumerate(starts):
        end = starts[n + 1] - 1 if n + 1 < len(starts) else min(len(lines), i + 40)
        block = lines[i:end]
        work = lines[i - 1] if i > 0 else ""
        if work.startswith("[LINK") or len(work) < 5:
            continue
        t = Tender(work=re.sub(r"\s+", " ", work), unit=block[1] if len(block) > 1 else "")

        def after(label: str, block: list[str] = block) -> str:
            for j, ln in enumerate(block):
                if ln.lower().startswith(label.lower()):
                    rest = ln[len(label):].strip(" :")
                    return rest or (block[j + 1] if j + 1 < len(block) else "")
            return ""

        t.tender_no = after("Tender Number")
        t.tender_type = after("Tender Type")
        t.published = _date(after("Published Date"))
        for j, ln in enumerate(block):
            if ln.lower().startswith("last date"):
                tail = " ".join(block[j + 1:j + 3])
                d = _date(tail)
                tm = TIME_RX.search(tail)
                t.closes = f"{d}T{int(tm[1]):02d}:{tm[2]}" if d and tm else d
                break
        g = GEM_RX.search(t.tender_no + " " + t.tender_type + " " + work)
        t.gem_bid = f"GEM/{g[1]}/B/{g[2]}" if g else ""
        r = REF_RX.search(t.tender_no + " " + work)
        t.nalco_ref = f"{r[1].upper()}-{r[2]}" if r else ""
        label = ""
        for ln in block:
            if ln.startswith("[LINK "):
                url = ln[6:-1]
                if url.lower().endswith(".pdf"):
                    t.docs.append((label or "Document", url))
            elif ln.lower().startswith("tender") and "document" in ln.lower():
                label = ln
        out.append(t)
    return out


# ---------- ranking ----------

def score(t: Tender) -> tuple[int, str]:
    """0–100 similarity to AMC's past work, and the closest past work."""
    from rapidfuzz import fuzz

    from apps.eligibility import load_profile

    past = (load_profile() or {}).get("past_work") or [""]
    text = t.work.lower()
    best = max(past, key=lambda p: fuzz.token_set_ratio(text, p))
    return int(fuzz.token_set_ratio(text, best)), best


# ---------- storage ----------

SCHEMA = """
CREATE TABLE IF NOT EXISTS bids (
    key TEXT PRIMARY KEY, data TEXT NOT NULL, score INTEGER, similar_to TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, closes TEXT,
    notified_new TEXT, notified_change TEXT, reminded TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    eligibility TEXT
);
"""


def db(memory: bool = False) -> sqlite3.Connection:
    """The bids database; in-memory for dry runs so they never mark anything as sent."""
    if memory:
        c = sqlite3.connect(":memory:")
    else:
        WORK.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(WORK / "bids.db", timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(bids)")}
    if "eligibility" not in cols:
        c.execute("ALTER TABLE bids ADD COLUMN eligibility TEXT")  # db from before 2026-11-11
    if "closes_seen" not in cols:  # every closing date already announced (no flip-flop alerts)
        c.execute("ALTER TABLE bids ADD COLUMN closes_seen TEXT DEFAULT ''")
    return c


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def same_tender(c: sqlite3.Connection, t: Tender, now: datetime) -> sqlite3.Row | None:
    """The same tender stored under another key — e.g. NALCO added (or dropped) the GeM bid
    number after we first saw it. Only tenders seen in the last 45 days (a yearly repeat of the
    same work is a new tender); two different GeM numbers or OMC/MC numbers are two tenders."""
    since = (now - timedelta(days=45)).isoformat(timespec="seconds")
    for r in c.execute("SELECT * FROM bids WHERE last_seen >= ?", (since,)):
        d = json.loads(r["data"])
        if _norm(d.get("unit")) != _norm(t.unit) or (d.get("gem_bid") and t.gem_bid):
            continue
        if d.get("nalco_ref") and t.nalco_ref and d["nalco_ref"] != t.nalco_ref:
            continue
        if (t.nalco_ref and d.get("nalco_ref") == t.nalco_ref) \
                or _norm(d.get("work")) == _norm(t.work):
            return r
    return None


# ---------- messages ----------

RULE = "━━━━━━━━━━━━━━━━━━━━"


def _closing(closes: str, now: datetime) -> tuple[str, int | None]:
    """('Sat, 05 Dec 2026 · 3:00 PM', days left) — days None if no date."""
    if not closes:
        return "not given", None
    dt = datetime.fromisoformat(closes if "T" in closes else closes + "T23:59")
    days = (dt.date() - now.astimezone(IST).date()).days
    when = dt.strftime("%a, %d %b %Y")
    if "T" in closes:
        when += " · " + dt.strftime("%I:%M %p").lstrip("0")
    return when, days


def _left(days: int | None) -> str:
    if days is None:
        return "⚪ closing date not given"
    if days < 0:
        return "⚫ closed"
    dot = "🔴" if days <= 2 else ("🟠" if days <= 7 else "🟢")
    word = "closes today" if days == 0 else ("1 day left" if days == 1 else f"{days} days left")
    return f"{dot} <b>{word}</b>"


def _when(closes: str, now: datetime) -> str:  # plain form, used in reminders/tests
    when, days = _closing(closes, now)
    if days is None:
        return "closing date not given"
    left = "today" if days == 0 else ("tomorrow" if days == 1 else
                                     (f"{days} days left" if days > 0 else "closed"))
    return f"{when.replace(' · ', ', ')} ({left})"


def _type(t: str) -> str:
    s = re.sub(r"\s*/\s*", " · ", t.strip())
    s = re.sub(r"(?i)\s*(at|through)\s+gem\s*portal|\s*at\s+gem\b", " · GeM", s)
    return re.sub(r"(\s·\s)+", " · ", s).strip(" ·")


def message(t: Tender, sc: int, similar: str, now: datetime, kind: str = "new",
            elig: dict | None = None) -> str:
    """Card layout. Order: work → closing + days left → OMC/MC → GeM bid →
    unit → eligibility verdict; then match and documents."""
    head = {"new": "🔔 <b>NEW NALCO TENDER</b>", "change": "✏️ <b>NALCO TENDER CHANGED</b>",
            "remind": "⏰ <b>TENDER CLOSING SOON</b>"}[kind]
    e = lambda x: html.escape(str(x), quote=False)
    when, days = _closing(t.closes, now)
    lines = [head, RULE, f"📌 <b>{e(t.work)}</b>", "",
             f"⏳ <b>Closes</b>   {e(when)}", _left(days), ""]
    if t.nalco_ref:
        lines.append(f"🔖 <b>Ref</b>        {e(t.nalco_ref)}")
    lines.append(f"🆔 <b>GeM</b>      <code>{e(t.gem_bid)}</code>" if t.gem_bid else
                 f"🆔 <b>Tender</b>  <code>{e(t.tender_no)}</code>")
    lines.append(f"🏭 <b>Unit</b>       {e(t.unit.title() if t.unit.isupper() else t.unit)}")
    if t.tender_type:
        lines.append(f"📝 <b>Type</b>      {e(_type(t.tender_type))}")
    if elig:
        lines += ["", f"<b>{e(elig['verdict'])}</b>"] + [
            f"   • {e(r)}" for r in elig["reasons"][:3]]
    lines += ["", f"🎯 <b>Match with our past work:</b> {sc}%", f"<i>   ({e(similar)})</i>"]
    if t.docs:
        lines += ["", "📄 " + "  ·  ".join(f'<a href="{html.escape(u)}">{e(lbl)}</a>'
                                         for lbl, u in t.docs[:3])]
    return "\n".join(lines)


DIGEST_OVER = 5  # more new tenders than this in one run → digest messages


def digest(items: list[tuple], now: datetime) -> list[str]:
    """Numbered cards for many new tenders, soonest closing first, each message under
    Telegram's 4,096-character limit."""
    e = lambda x: html.escape(str(x), quote=False)
    items = sorted(items, key=lambda x: x[0].closes or "9999")
    head = [f"🔔 <b>{len(items)} NEW NALCO TENDERS</b>",
            "<i>Soonest closing first · ✅ likely eligible · ⚠️ check · ❌ probably not</i>", RULE]
    blocks = []
    for n, (t, sc, _, elig) in enumerate(items, 1):
        when, days = _closing(t.closes, now)
        ids = "  ·  ".join(x for x in (f"🔖 {e(t.nalco_ref)}" if t.nalco_ref else "",
                                       f"🆔 <code>{e(t.gem_bid)}</code>" if t.gem_bid else "")
                           if x)
        v = elig["verdict"] if elig else ""
        unit = t.unit.title() if t.unit.isupper() else t.unit
        blocks.append("\n".join(filter(None, [
            f"<b>{n}. {e(t.work[:120])}</b>",
            f"⏳ {e(when)}   {_left(days)}",
            ids,
            f"🏭 {e(unit[:40])}   {e(v)}   🎯 {sc}%"])))
    msgs, cur = [], "\n".join(head)
    for blk in blocks:
        if len(cur) + len(blk) + 2 > 3900:
            msgs.append(cur)
            cur = "🔔 <b>NALCO TENDERS (continued)</b>\n" + RULE
        cur += "\n\n" + blk
    return msgs + [cur]


# ---------- notifiers ----------

def load_env() -> dict:
    env = dict(os.environ)
    p = REPO / ".env"
    if p.exists():
        for ln in p.read_text().splitlines():
            if "=" in ln and not ln.lstrip().startswith("#"):
                k, v = ln.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return env


def _post(url: str, data: dict | None = None, timeout: int = 20) -> int:
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as r:
        return r.status


def send(text: str, env: dict, dry_run: bool = False, prefix: str = "TELEGRAM") -> list[str]:
    """Send via the bot `<prefix>_BOT_TOKEN` to `<prefix>_CHAT_IDS` (TELEGRAM = tender bot,
    REMINDER = reminder bot), plus CallMeBot WhatsApp if configured."""
    if dry_run:
        print(html.unescape(re.sub(r"<[^>]+>", "", text)), "\n" + "-" * 60)
        return ["dry-run"]
    results = []
    token, chats = env.get(f"{prefix}_BOT_TOKEN"), env.get(f"{prefix}_CHAT_IDS", "")
    for chat in filter(None, (c.strip() for c in chats.split(","))):
        try:
            st = _post(f"https://api.telegram.org/bot{token}/sendMessage",
                       {"chat_id": chat, "text": text, "parse_mode": "HTML",
                        "disable_web_page_preview": "true"})
            results.append(f"telegram {chat}: {st}")
        except Exception as e:  # noqa: BLE001 - report, keep other channels going
            results.append(f"telegram {chat}: FAILED {type(e).__name__}")
    phone, key = env.get("CALLMEBOT_PHONE"), env.get("CALLMEBOT_APIKEY")
    if phone and key:
        plain = re.sub(r"<[^>]+>", "", html.unescape(text))
        q = urllib.parse.urlencode({"phone": phone, "text": plain, "apikey": key})
        try:
            results.append(f"whatsapp: {_post('https://api.callmebot.com/whatsapp.php?' + q)}")
        except Exception as e:  # noqa: BLE001
            results.append(f"whatsapp: FAILED {type(e).__name__}")
    if not results:
        results.append(f"no channel configured (set {prefix}_BOT_TOKEN + {prefix}_CHAT_IDS)")
    return results


# ---------- eligibility ----------

def pick_pdf(t: Tender) -> str | None:
    """GeM's own bid document if linked (summary), else the first tender PDF."""
    for label, url in t.docs:
        if "gem-bidding" in url.lower() or "summary" in label.lower():
            return url
    return t.docs[0][1] if t.docs else None


def fetch_pdf(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=90, context=_ssl_context()) as r:
        data = r.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("PDF larger than 25 MB")
    return data


def assess(t: Tender) -> dict:
    from apps.eligibility import load_profile, parse_bid, pdf_text, verdict

    url = pick_pdf(t)
    if not url:
        return {"verdict": "⚠️ Check", "reasons": ["no tender PDF linked"], "source": None}
    try:
        crit = parse_bid(pdf_text(fetch_pdf(url)))
    except Exception as e:  # noqa: BLE001 - a bad PDF must not stop the alert
        return {"verdict": "⚠️ Check", "reasons": [f"could not read PDF ({type(e).__name__})"],
                "source": url}
    v, reasons = verdict(crit, load_profile())
    return {"verdict": v, "reasons": reasons, "source": url, "criteria": asdict(crit)}


# ---------- run ----------

def fetch(url: str = SOURCE_URL) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60, context=_ssl_context()) as r:
        return r.read().decode("utf-8", "replace")


def process(page: str, env: dict, now: datetime, dry_run: bool = False,
            conn: sqlite3.Connection | None = None, check_pdfs: bool = True) -> dict:
    tenders = parse(page)
    units = re.compile(env.get("BIDWATCH_UNITS", DEFAULT_UNITS), re.IGNORECASE)
    min_score = int(env.get("BIDWATCH_MIN_SCORE", "0"))
    c = conn or db(memory=dry_run)
    stats = {"parsed": len(tenders), "watched": 0, "new": 0, "changed": 0, "reminders": 0,
             "sent": []}
    stamp = now.isoformat(timespec="seconds")
    fresh: list[tuple] = []
    seen_keys: set[str] = set()
    for t in tenders:
        if not units.search(t.unit) or t.key in seen_keys:  # NALCO sometimes lists one twice
            continue
        seen_keys.add(t.key)
        stats["watched"] += 1
        sc, similar = score(t)
        row = c.execute("SELECT * FROM bids WHERE key=?", (t.key,)).fetchone()
        if row is None and (alias := same_tender(c, t, now)) is not None:
            if alias["key"] in seen_keys:  # listed twice on this page, once without GeM no.
                stats["watched"] -= 1
                continue
            c.execute("UPDATE bids SET key=? WHERE key=?", (t.key, alias["key"]))
            row = c.execute("SELECT * FROM bids WHERE key=?", (t.key,)).fetchone()
        data = json.dumps(asdict(t), ensure_ascii=False)
        if row is None:
            c.execute("INSERT INTO bids(key, data, score, similar_to, first_seen, last_seen,"
                      " closes) VALUES (?,?,?,?,?,?,?)",
                      (t.key, data, sc, similar, stamp, stamp, t.closes))
            if sc >= min_score:
                elig = assess(t) if check_pdfs else None
                if elig:
                    c.execute("UPDATE bids SET eligibility=? WHERE key=?",
                              (json.dumps(elig, ensure_ascii=False), t.key))
                fresh.append((t, sc, similar, elig))
                c.execute("UPDATE bids SET notified_new=? WHERE key=?", (stamp, t.key))
                stats["new"] += 1
        else:
            seen = {x for x in (row["closes_seen"] or "").split("|") if x} | (
                {row["closes"]} if row["closes"] else set())
            if t.closes and t.closes not in seen:  # a closing date never announced before
                old = json.loads(row["eligibility"]) if row["eligibility"] else None
                stats["sent"] += send(message(t, sc, similar, now, "change", old), env, dry_run)
                # the change alert covers today; closing reminders restart for the new date
                days = (datetime.fromisoformat(t.closes[:10]).date()
                        - now.astimezone(IST).date()).days
                c.execute("UPDATE bids SET notified_change=?, reminded=? WHERE key=?",
                          (stamp, f"d{days}," if days in REMIND_DAYS else "", t.key))
                stats["changed"] += 1
            if t.closes:
                seen.add(t.closes)
            c.execute("UPDATE bids SET data=?, last_seen=?, closes=?, score=?, closes_seen=?"
                      " WHERE key=?", (data, stamp, t.closes or row["closes"], sc,
                                       "|".join(sorted(seen)), t.key))
        r2 = c.execute("SELECT * FROM bids WHERE key=?", (t.key,)).fetchone()
        el = json.loads(r2["eligibility"]) if r2["eligibility"] else {}
        closes = t.closes or r2["closes"]
        if closes and r2["status"] != "skipped" and not el.get("verdict", "").startswith("❌") \
                and sc >= min_score:
            days = (datetime.fromisoformat(closes[:10]).date() - now.astimezone(IST).date()).days
            tag = f"d{days}"
            if row is None and days in REMIND_DAYS:  # first sighting: the new alert says it
                c.execute("UPDATE bids SET reminded=? WHERE key=?", (tag + ",", t.key))
            elif days in REMIND_DAYS and tag not in (r2["reminded"] or ""):
                old = json.loads(r2["eligibility"]) if r2["eligibility"] else None
                stats["sent"] += send(message(t, sc, similar, now, "remind", old), env, dry_run)
                c.execute("UPDATE bids SET reminded=? WHERE key=?",
                          ((r2["reminded"] or "") + tag + ",", t.key))
                stats["reminders"] += 1
        c.commit()
    if len(fresh) > DIGEST_OVER:
        for part in digest(fresh, now):
            stats["sent"] += send(part, env, dry_run)
    else:
        for t, sc, similar, elig in fresh:
            stats["sent"] += send(message(t, sc, similar, now, elig=elig), env, dry_run)
    if conn is None:
        c.close()
    return stats


def hosted_elsewhere(env: dict) -> bool:
    """True on this Mac once the bots live on GitHub (.env BOTS_HOSTED=github): a real run here
    would use an out-of-date memory and repeat alerts, so it must refuse."""
    return env.get("BOTS_HOSTED") == "github" and env.get("GITHUB_ACTIONS") != "true"


HOSTED_MSG = ("The bots run on GitHub now (BOTS_HOSTED=github in .env); a real run here would "
              "repeat alerts. Use --dry-run, or GitHub → tender-bots → Actions → Run workflow.")


def channels_configured(env: dict, prefix: str = "TELEGRAM") -> bool:
    return bool(env.get(f"{prefix}_BOT_TOKEN") and env.get(f"{prefix}_CHAT_IDS", "").strip()) \
        or bool(env.get("CALLMEBOT_PHONE") and env.get("CALLMEBOT_APIKEY"))


def setup(env: dict, prefix: str = "TELEGRAM", hello: str | None = None) -> list[str]:
    """Find chats that pressed Start on the bot, save them to .env, send a test to each."""
    tok = env.get(f"{prefix}_BOT_TOKEN")
    if not tok:
        return [f"{prefix}_BOT_TOKEN missing in .env"]
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/getUpdates",
                                 headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20, context=_ssl_context()) as r:
        updates = json.load(r)["result"]
    chats = {}
    for u in updates:
        m = u.get("message") or u.get("my_chat_member") or {}
        ch = m.get("chat") or {}
        if ch.get("type") == "private":
            chats[str(ch["id"])] = ch.get("first_name", "")
    old = {c.strip() for c in env.get(f"{prefix}_CHAT_IDS", "").split(",") if c.strip()}
    ids = sorted(old | set(chats))
    if not ids:
        return ["nobody has pressed Start on the bot yet"]
    p = REPO / ".env"
    lines = [ln for ln in (p.read_text().splitlines() if p.exists() else [])
             if not ln.startswith(f"{prefix}_CHAT_IDS=")]
    lines.append(f"{prefix}_CHAT_IDS=" + ",".join(ids))
    p.write_text("\n".join(lines) + "\n")
    p.chmod(0o600)
    env = {**env, f"{prefix}_CHAT_IDS": ",".join(ids)}
    names = ", ".join(chats.get(i, i) for i in ids)
    return [f"recipients: {names}"] + send(hello or (
        "✅ <b>AMC tender alerts are connected</b>\n" + RULE + "\nYou'll get every new NALCO "
        "tender here: name of work, closing date, OMC/MC no., GeM bid no., unit and whether AMC "
        "is likely eligible."), env, prefix=prefix)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bidwatch")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    r.add_argument("--no-pdf", action="store_true", help="skip the eligibility check")
    sub.add_parser("list")
    sub.add_parser("test")
    sub.add_parser("setup", help="register chats that pressed Start; send a test")
    a = ap.parse_args(argv)
    env = load_env()
    now = datetime.now(IST)
    if a.cmd == "setup":
        print(*setup(env), sep="\n")
        return 0
    if a.cmd in ("run", "test") and not getattr(a, "dry_run", False) and hosted_elsewhere(env):
        print(HOSTED_MSG, file=sys.stderr)
        return 4
    if a.cmd == "run":
        if not a.dry_run and not channels_configured(env):
            # never mark tenders "sent" when nobody can receive them
            print("No recipient configured: run `python -m apps.bidwatch setup` after pressing "
                  "Start on the bot.", file=sys.stderr)
            return 3
        page = fetch()
        WORK.joinpath("bidwatch").mkdir(parents=True, exist_ok=True)
        WORK.joinpath("bidwatch", "last_page.html").write_text(page, encoding="utf-8")
        stats = process(page, env, now, dry_run=a.dry_run, check_pdfs=not a.no_pdf)
        print(json.dumps({k: v for k, v in stats.items() if k != "sent"}),
              *(sorted(set(stats["sent"])) or []), sep="\n")
        if stats["parsed"] == 0:
            print("WARNING: 0 tenders parsed — page layout may have changed", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "list":
        for row in db().execute("SELECT * FROM bids ORDER BY closes"):
            d = json.loads(row["data"])
            print(f"{row['closes'] or '?':16} {row['score']:3}%  {d['unit'][:22]:22} "
                  f"{(d['gem_bid'] or d['tender_no'])[:24]:24} {d['work'][:70]}")
        return 0
    if a.cmd == "test":
        print(*send("✅ Contractor OS bid watcher is connected.", env), sep="\n")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
