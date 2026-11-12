"""'Can we do it?' — read a GeM bid PDF's eligibility criteria and compare with AMC's profile.

Deterministic and conservative: anything not read with confidence becomes "check", never a
pass. The verdict is advice for a human; the system never bids (CLAUDE.md rule 4).

Profile: config/amc_profile.json (values with source + confirmed flag; edit by hand).
    uv run python -m apps.eligibility build-profile   # (re)create from the catalog, unconfirmed
    uv run python -m apps.eligibility check <pdf>     # print fields + verdict for one bid PDF
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROFILE = REPO / "config" / "amc_profile.json"
S = re.DOTALL | re.IGNORECASE
LAKH, CRORE = 100_000, 10_000_000


@dataclass
class Criteria:
    turnover_inr: int | None = None        # minimum average annual turnover (3 years)
    experience_years: int | None = None
    similar_required: bool | None = None
    mse_exemption: bool | None = None
    startup_exemption: bool | None = None
    estimated_inr: int | None = None
    emd_inr: int | None = None
    contract_years: int | None = None
    bid_end: str | None = None
    office: str | None = None
    similar_rule: list[tuple[int, int]] = field(default_factory=list)  # (orders, percent)
    read_ok: bool = False
    kind: str = ""          # gem_bid | nit | notice | forms | unknown
    gem_ref: str | None = None


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _yes(s: str | None) -> bool | None:
    return None if s is None else s.lower() == "yes"


def parse_bid(text: str) -> Criteria:
    """Criteria from a GeM bid document's text (Hindi labels between English label and value
    are tolerated by searching a short window after each English label)."""
    full = text[:150000]
    t = text[:20000]
    c = Criteria()
    if m := re.search(r"GEM/20\d\d/B/\d{6,8}", full):
        c.gem_ref = m[0]
    if re.search(r"Bid Number", t) and re.search(r"EMD|Bid Details|Item Category", t):
        c.kind = "gem_bid"
    elif len(full) < 1500:
        c.kind = "notice"
    elif re.search(r"NECESSARY FORMS|CHECK ?LIST", full[:400], re.IGNORECASE):
        c.kind = "forms"
    else:
        c.kind = "nit"
    if m := re.search(r"Minimum Average Annual Turnover.{0,220}?(\d[\d,.]*)\s*(Lakh|Crore|Cr)",
                      t, S):
        c.turnover_inr = int(_num(m[1]) * (LAKH if m[2].lower().startswith("l") else CRORE))
    if m := re.search(r"Years of Past Experience Required.{0,220}?(\d+)\s*Year", t, S):
        c.experience_years = int(m[1])
    if m := re.search(r"Past Experience of Similar Services.{0,160}?\b(Yes|No)\b", t, S):
        c.similar_required = _yes(m[1])
    # older GeM layout says "Exemption", newer says "Relaxation"
    if m := re.search(r"MSE\s+(?:Exemption|Relaxation)\s+for\s+Years\s+of\s+(?:Past\s+)?Experience"
                      r".{0,140}?\b(Yes|No)\b", t, S):
        c.mse_exemption = _yes(m[1])
    if m := re.search(r"Startup\s+(?:Exemption|Relaxation)\s+for\s+Years\s+of\s+(?:Past\s+)?Experience"
                      r".{0,140}?\b(Yes|No)\b", t, S):
        c.startup_exemption = _yes(m[1])
    # values may be on the next line; >= 4 digits so stray digits in garbled Hindi don't match
    if m := re.search(r"Estimated Bid Value(?! indicated).{0,100}?(?<![\d.])(\d[\d,]{3,}(?:\.\d+)?)",
                      t, S):
        c.estimated_inr = int(_num(m[1]))
    if m := re.search(r"EMD Amount.{0,80}?(?<![\d.])(\d[\d,]{2,}(?:\.\d+)?)", t, S):
        c.emd_inr = int(_num(m[1]))
    if m := re.search(r"Contract Period.{0,60}?(\d+)\s*Year", t, S):
        c.contract_years = int(m[1])
    if m := re.search(r"Bid End Date/Time.{0,80}?(\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2})", t, S):
        c.bid_end = m[1]
    if m := re.search(r"Office Name[^\n]*?(?:\s{2,}|\n)([A-Za-z][^\n]{2,60})", t):
        c.office = m[1].strip()
    for n, pct in re.findall(r"(One|Two|Three)\s+similar\s+completed\s+services?\s+costing\s+not"
                             r"\s+less\s+than\s+the\s+amount\s+equal\s+to\s+(\d{2})\s*%", t, re.IGNORECASE):
        c.similar_rule.append(({"one": 1, "two": 2, "three": 3}[n.lower()], int(pct)))
    if c.kind == "nit" and c.turnover_inr is None and (m := re.search(
            r"average annual (?:financial )?turnover.{0,260}?(?:Rs\.?|₹|INR)\s*([\d,]+(?:\.\d+)?)"
            r"\s*(lakh|lac|crore|cr)?", full, S | re.IGNORECASE)):
        unit = (m[2] or "").lower()
        mult = LAKH if unit.startswith(("lakh", "lac")) else CRORE if unit.startswith("cr") else 1
        c.turnover_inr = int(_num(m[1]) * mult)
    c.read_ok = c.kind == "gem_bid" or any(
        v is not None for v in (c.turnover_inr, c.experience_years, c.estimated_inr,
                                c.mse_exemption))
    return c


def load_profile(path: Path = PROFILE) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


def _l(x: int) -> str:
    return f"₹{x / LAKH:,.1f} L" if x < CRORE else f"₹{x / CRORE:,.2f} Cr"


def verdict(c: Criteria, p: dict | None) -> tuple[str, list[str]]:
    """('✅ Likely eligible' | '⚠️ Check' | '❌ Probably not eligible', reasons)."""
    if not c.read_ok:
        where = f" (GeM bid {c.gem_ref})" if c.gem_ref else ""
        why = {"notice": f"only a short notice — full terms on GeM{where}",
               "forms": "this PDF is the forms/checklist — qualifying rules are in another "
                        "tender document",
               "nit": "qualifying rules not in this PDF — see the tender documents on the portal"
               }.get(c.kind, "eligibility not readable from the bid PDF — open it")
        return "⚠️ Check", [why]
    if not p:
        return "⚠️ Check", ["AMC profile missing (config/amc_profile.json)"]
    fails, checks, oks = [], [], []
    material: list[str] = []  # open questions that decide eligibility → verdict "check"
    mse = p.get("mse", {}).get("value") and c.mse_exemption
    if mse:
        cav = p.get("mse", {}).get("caveat")
        checks.append("MSE exemption offered — AMC is MSE" + (f", but Udyam: {cav}" if cav else "")
                      + "; confirm it covers services")
    turnover = p.get("avg_turnover_3y_inr", {}).get("value")
    if c.turnover_inr:
        if turnover is None:
            checks.append(f"turnover ≥ {_l(c.turnover_inr)} required — AMC figure missing")
        elif turnover >= c.turnover_inr:
            oks.append(f"turnover {_l(turnover)} ≥ required {_l(c.turnover_inr)}")
        else:
            fails.append(f"turnover needs {_l(c.turnover_inr)}, AMC {_l(turnover)}")
    years = p.get("experience_years", {}).get("value")
    if c.experience_years:
        if years is not None and years >= c.experience_years:
            oks.append(f"experience {years} y ≥ {c.experience_years} y")
        else:
            fails.append(f"needs {c.experience_years} y experience, AMC {years} y")
    if c.similar_required and c.estimated_inr:
        rule = c.similar_rule or [(3, 40), (2, 50), (1, 80)]

        def meets(key: str) -> str | None:
            vals = sorted((o.get(key) or o["value_inr"] for o in p.get("completed_orders", [])),
                          reverse=True)
            return next((f"{n}×≥{pct}%" for n, pct in rule
                         if len(vals) >= n and vals[n - 1] >= c.estimated_inr * pct / 100), None)

        met, met_est = meets("value_inr"), meets("est_value_inr")
        if met:  # cash actually received already clears the bar
            oks.append(f"completed NALCO jobs meet {met} of {_l(c.estimated_inr)} (by cash paid)")
            checks.append("confirm those jobs count as 'similar' work")
        elif met_est:
            material.append(f"similar work: probably meets {met_est} of {_l(c.estimated_inr)} "
                            "(estimated bill values) — confirm with work orders")
        if not met and not met_est:
            need = max(c.estimated_inr * pct / 100 for _, pct in rule)
            msg = (f"similar work: needs e.g. 1 completed order ≥ {_l(int(need))} — known "
                   f"orders don't show it")
            if p.get("confirmed") and p.get("completed_orders_complete"):
                fails.append(msg)
            else:  # our list of completed orders is incomplete: never a ❌ on this alone
                material.append(msg + " (completed-orders list not yet complete)")
    if mse:  # turnover/experience shortfalls may be waived for MSEs: downgrade to "check"
        waivable = [f for f in fails if f.startswith(("turnover", "needs"))]
        fails = [f for f in fails if f not in waivable]
        material += [f"{f} (may be exempt as MSE)" for f in waivable]
    if c.kind == "gem_bid" and not (c.turnover_inr or c.experience_years or
                                    c.similar_required):
        oks.append("no turnover / experience rule in the GeM bid")
        checks.append("read the ATC / extra documents for any other conditions")
    if c.emd_inr:
        exempt = p.get("mse", {}).get("value")
        checks.append(f"EMD {_l(c.emd_inr)}" + (" — MSE (Udyam) can claim exemption"
                                                 if exempt else ""))
    if not p.get("confirmed", False):
        checks.append("AMC profile not yet confirmed")
    if fails:
        return "❌ Probably not eligible", fails + checks
    if material or not oks:
        return "⚠️ Check", material + checks + oks
    return "✅ Likely eligible", oks + checks


def pdf_text(data: bytes, pages: int = 4) -> str:
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)
    doc = pymupdf.open(stream=data, filetype="pdf")
    try:
        return "\f".join(doc[i].get_text() for i in range(min(pages, doc.page_count)))
    finally:
        doc.close()


def build_profile() -> dict:
    """Profile from the catalog. Every value has a source; confirmed=false until checked.
    Firm details (MSE, turnover, experience) are kept from the existing profile, never in code."""
    import sqlite3

    c = sqlite3.connect(REPO / "work" / "catalog.db")
    c.row_factory = sqlite3.Row
    values = {}
    for w in c.execute("SELECT ref, status, facts FROM works"):
        for cv in json.loads(w["facts"])["contract_value"]:
            values.setdefault(cv["value"], []).append((w["ref"], w["status"], cv["evidence"]))
    orders = [{"ref": refs[0][0], "value_inr": int(v) // 100, "source": refs[0][2],
               "label": "INFERRED: contract value mentioned in its documents; confirm completed"}
              for v, refs in values.items()
              if len(refs) == 1 and refs[0][1] == "past" and int(v) >= 10 * LAKH * 100]
    orders.sort(key=lambda o: -o["value_inr"])
    old = load_profile() or {}
    return {
        "confirmed": False,
        "note": "Edit values, then set confirmed=true. Used by apps/eligibility.py.",
        **{k: old[k] for k in ("firm", "mse", "avg_turnover_3y_inr", "experience_years",
                                "past_work") if k in old},
        "completed_orders": orders[:12],
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["build-profile"]:
        PROFILE.parent.mkdir(exist_ok=True)
        if PROFILE.exists() and "--force" not in argv:
            print(f"{PROFILE} exists (edit it, or --force to rebuild)")
            return 1
        PROFILE.write_text(json.dumps(build_profile(), indent=2, ensure_ascii=False) + "\n")
        print(PROFILE)
        return 0
    if argv[:1] == ["check"] and len(argv) > 1:
        c = parse_bid(pdf_text(Path(argv[1]).read_bytes()))
        print(json.dumps(asdict(c), indent=1))
        v, reasons = verdict(c, load_profile())
        print(v, *reasons, sep="\n - ")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
