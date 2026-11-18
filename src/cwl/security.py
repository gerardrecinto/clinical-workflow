"""Request signing with replay window, PHI redaction for logs, tamper-evident audit chain."""
import hashlib
import hmac
import logging
import re
import time

WINDOW = 300


def sign(secret, ts, method, path, body):
    msg = f"{ts}.{method}.{path}.".encode() + body
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify(secret, ts, sig, method, path, body, now=None):
    """Constant-time compare; stale or future timestamps are rejected to block replay."""
    now = time.time() if now is None else now
    try:
        if abs(now - int(ts)) > WINDOW:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(secret, ts, method, path, body), sig or "")


_PATTERNS = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"), "[EMAIL]"),
    (re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"), "[PHONE]"),
    (re.compile(r"\bMRN[:#\s]*\d+\b", re.I), "[MRN]"),
]


def redact(text):
    for rx, tag in _PATTERNS:
        text = rx.sub(tag, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


class Audit:
    """Each row hashes the previous one. Editing or deleting a row breaks verify()."""

    def __init__(self, db):
        self.db = db
        db.execute(
            "create table if not exists audit(seq integer primary key autoincrement,"
            " ts real, actor text, action text, ref text, prev text, hash text)"
        )

    @staticmethod
    def _h(prev, ts, actor, action, ref):
        return hashlib.sha256(f"{prev}|{ts}|{actor}|{action}|{ref}".encode()).hexdigest()

    def record(self, actor, action, ref, ts=None):
        ts = time.time() if ts is None else ts
        row = self.db.execute("select hash from audit order by seq desc limit 1").fetchone()
        prev = row[0] if row else "0" * 64
        self.db.execute(
            "insert into audit(ts,actor,action,ref,prev,hash) values(?,?,?,?,?,?)",
            (ts, actor, action, ref, prev, self._h(prev, ts, actor, action, ref)),
        )

    def verify(self):
        """Return None if the chain is intact, else the seq of the first bad row."""
        prev = "0" * 64
        for seq, ts, actor, action, ref, p, h in self.db.execute(
            "select seq,ts,actor,action,ref,prev,hash from audit order by seq"
        ):
            if p != prev or h != self._h(prev, ts, actor, action, ref):
                return seq
            prev = h
        return None
