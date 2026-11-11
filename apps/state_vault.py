"""Encrypted bot memory for a public repo: state/*.db + state/run.log  <->  state.enc

On a public GitHub repo every file and log is readable by anyone, so what the bots remember
(which tenders / reminders were already sent, eligibility notes, run log) is stored as one
encrypted blob. Key: env STATE_KEY (a GitHub secret; locally in contractor-os/.env).

  python -m apps.state_vault newkey               # print a fresh key (put it in .env + secret)
  python -m apps.state_vault unpack [DIR]         # state.enc -> DIR (default: state/)
  python -m apps.state_vault pack [DIR] [--force] # DIR -> state.enc, only when the content
                                                  # changed (ignoring last-seen stamps) or once
                                                  # a day, so the repo doesn't grow every run
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import sqlite3
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BLOB, FP = REPO / "state.enc", REPO / "state.fp"
IST = timezone(timedelta(hours=5, minutes=30))
LOG_LINES = 400


def _key() -> bytes:
    k = os.environ.get("STATE_KEY")
    if not k and (REPO / ".env").exists():
        for line in (REPO / ".env").read_text().splitlines():
            if line.startswith("STATE_KEY="):
                k = line.split("=", 1)[1].strip()
    if not k:
        sys.exit("STATE_KEY not set")
    return k.encode()


def _fernet():
    from cryptography.fernet import Fernet

    return Fernet(_key())


def fingerprint(d: Path) -> str:
    """Keyed hash of the meaningful content (bids.last_seen changes every run: ignored)."""
    h = hmac.new(_key(), digestmod=hashlib.sha256)
    for db in sorted(d.glob("*.db")):
        c = sqlite3.connect(db)
        try:
            for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY 1"):
                cols = [r[1] for r in c.execute(f"PRAGMA table_info('{t}')")
                        if not (db.name == "bids.db" and r[1] == "last_seen")]
                sel = ", ".join(f'"{x}"' for x in cols)
                h.update(f"{db.name}:{t}\n".encode())
                for row in c.execute(f'SELECT {sel} FROM "{t}" ORDER BY 1'):
                    h.update(repr(row).encode())
        finally:
            c.close()
    return h.hexdigest()


def pack(d: Path, force: bool = False) -> bool:
    log = d / "run.log"
    if log.exists():  # keep the log short
        log.write_text("\n".join(log.read_text(errors="replace").splitlines()[-LOG_LINES:]) + "\n")
    fp, day = fingerprint(d), datetime.now(IST).date().isoformat()
    old = json.loads(FP.read_text()) if FP.exists() else {}
    if not force and BLOB.exists() and old.get("fp") == fp and old.get("day") == day:
        return False
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in sorted([*d.glob("*.db"), log]):
            if f.exists():
                tar.add(f, arcname=f.name)
    BLOB.write_bytes(_fernet().encrypt(buf.getvalue()))
    FP.write_text(json.dumps({"fp": fp, "day": day}) + "\n")
    return True


def unpack(d: Path) -> int:
    d.mkdir(parents=True, exist_ok=True)
    if not BLOB.exists():
        return 0
    raw = _fernet().decrypt(BLOB.read_bytes())
    n = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for m in tar.getmembers():
            name = Path(m.name).name
            if m.isfile() and name == m.name and (name.endswith(".db") or name == "run.log"):
                (d / name).write_bytes(tar.extractfile(m).read())
                n += 1
    return n


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd, rest = (argv[0] if argv else ""), [a for a in argv[1:] if not a.startswith("--")]
    d = Path(rest[0]) if rest else REPO / "state"
    if cmd == "newkey":
        from cryptography.fernet import Fernet

        print(Fernet.generate_key().decode())
        return 0
    if cmd == "unpack":
        print(f"state: {unpack(d)} file(s) restored")
        return 0
    if cmd == "pack":
        print("state: saved" if pack(d, "--force" in argv) else "state: unchanged")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
