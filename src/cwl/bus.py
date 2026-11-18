"""Transactional outbox with at-least-once delivery, per-consumer idempotency,
exponential backoff and a dead-letter state. SQLite stands in for SQS plus a database."""
import json
import time
import uuid

MAX_ATTEMPTS = 4

SCHEMA = """
create table if not exists events(
  id text primary key, topic text not null, body text not null,
  status text not null default 'pending', attempts int not null default 0,
  next_at real not null default 0, error text);
create table if not exists processed(consumer text, event_id text, primary key(consumer, event_id));
"""


class Bus:
    def __init__(self, db):
        self.db = db
        db.executescript(SCHEMA)
        self.handlers = {}

    def subscribe(self, topic, name, fn):
        self.handlers.setdefault(topic, []).append((name, fn))

    def publish(self, topic, body, event_id=None):
        """Insert only. The caller commits, so the state change and the event land together."""
        eid = event_id or str(uuid.uuid4())
        self.db.execute(
            "insert or ignore into events(id,topic,body) values(?,?,?)", (eid, topic, json.dumps(body))
        )
        return eid

    def pump(self, now=None, limit=100):
        now = time.time() if now is None else now
        rows = self.db.execute(
            "select id,topic,body,attempts from events where status='pending' and next_at<=?"
            " order by rowid limit ?",
            (now, limit),
        ).fetchall()
        for eid, topic, body, attempts in rows:
            self._deliver(eid, topic, json.loads(body), attempts, now)
        return len(rows)

    def _deliver(self, eid, topic, body, attempts, now):
        failed = None
        for name, fn in self.handlers.get(topic, []):
            if self.db.execute(
                "select 1 from processed where consumer=? and event_id=?", (name, eid)
            ).fetchone():
                continue
            try:
                with self.db:  # handler effects and the processed marker commit together
                    fn(body)
                    self.db.execute("insert into processed values(?,?)", (name, eid))
            except Exception as e:  # noqa: BLE001
                failed = e
        with self.db:
            if failed is None:
                self.db.execute("update events set status='done' where id=?", (eid,))
            else:
                attempts += 1
                status = "dead" if attempts >= MAX_ATTEMPTS else "pending"
                # store the exception type only: messages can carry PHI
                self.db.execute(
                    "update events set status=?,attempts=?,next_at=?,error=? where id=?",
                    (status, attempts, now + 2**attempts, type(failed).__name__, eid),
                )

    def dead(self):
        return [r[0] for r in self.db.execute("select id from events where status='dead'")]
