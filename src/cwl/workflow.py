"""Referral saga: eligibility -> scheduling -> notify, with compensation and a
work-item queue for steps that have no API (portal-only payers)."""
import json
import uuid

from . import fhir

STEPS = ["eligibility", "scheduling", "notify"]
PORTAL_ONLY = {"portal-mutual"}
API_PAYERS = {"acme-health", "northwind"}
LEASE = 60


class Rejected(Exception):
    """Permanent business failure. Triggers compensation, never retried."""


class TransientError(Exception):
    """Temporary failure. Propagates so the bus retries with backoff."""


class Services:
    """Stand-ins for payer, scheduling and messaging systems."""

    def __init__(self, slots=2):
        self.slots = {"cardiology": slots, "dermatology": slots, "orthopedics": slots}
        self.reserved = set()
        self.fail_next = 0
        self.sent = []

    def eligibility(self, r):
        if r["payer"] in PORTAL_ONLY:
            return "manual"
        if r["payer"] not in API_PAYERS:
            raise Rejected("unknown_payer")
        if r["member_id"].endswith("0"):
            raise Rejected("inactive_coverage")
        return "ok"

    def scheduling(self, r):
        if self.fail_next:
            self.fail_next -= 1
            raise TransientError("scheduler timeout")
        if r["id"] in self.reserved:  # idempotent on referral id
            return "ok"
        if self.slots[r["specialty"]] <= 0:
            raise Rejected("no_capacity")
        self.slots[r["specialty"]] -= 1
        self.reserved.add(r["id"])
        return "ok"

    def undo_scheduling(self, r):
        if r["id"] in self.reserved:
            self.reserved.discard(r["id"])
            self.slots[r["specialty"]] += 1

    def notify(self, r):
        if not r["contact"]:
            raise Rejected("no_contact")
        self.sent.append(r["id"])
        return "ok"


class Workflow:
    def __init__(self, db, bus, audit, services):
        self.db, self.bus, self.audit, self.svc = db, bus, audit, services
        db.executescript(
            """
            create table if not exists referrals(id text primary key, state text, payer text,
              member_id text, specialty text, patient_id text, contact text, history text);
            create table if not exists queue(id text primary key, referral_id text, kind text,
              status text, lease_until real default 0, result text);
            create table if not exists idem(key text primary key, referral_id text);
            """
        )
        bus.subscribe("saga.step", "orchestrator", self.on_step)

    def start(self, intake, idem_key=None, actor="api"):
        if idem_key:
            row = self.db.execute("select referral_id from idem where key=?", (idem_key,)).fetchone()
            if row:
                return row[0], False
        rid = str(uuid.uuid4())
        self.db.execute(
            "insert into referrals values(?,?,?,?,?,?,?,?)",
            (rid, "RUNNING", intake["payer"], intake["member_id"], intake["specialty"],
             intake["patient_id"], intake.get("contact"), "[]"),
        )
        if idem_key:
            self.db.execute("insert into idem values(?,?)", (idem_key, rid))
        self.audit.record(actor, "referral.created", f"ServiceRequest/{rid}")
        self.bus.publish("saga.step", {"referral_id": rid, "step": STEPS[0]}, f"{rid}:{STEPS[0]}")
        return rid, True

    def get(self, rid):
        row = self.db.execute("select * from referrals where id=?", (rid,)).fetchone()
        return dict(row) if row else None

    def view(self, rid):
        r = self.get(rid)
        if not r:
            return None
        return {
            "state": r["state"],
            "history": json.loads(r["history"]),
            "resource": fhir.service_request(rid, r),
        }

    def _log(self, r, **entry):
        h = json.loads(r["history"]) + [entry]
        r["history"] = json.dumps(h)
        self.db.execute("update referrals set history=?,state=? where id=?", (r["history"], r["state"], r["id"]))

    def on_step(self, body):
        r = self.get(body["referral_id"])
        step = body["step"]
        if r["state"] != "RUNNING" or any(h.get("step") == step for h in json.loads(r["history"])):
            return  # stale or duplicate event
        try:
            outcome = getattr(self.svc, step)(r)
        except Rejected as e:
            return self._compensate(r, step, str(e))
        if outcome == "manual":
            qid = str(uuid.uuid4())
            self.db.execute("insert into queue values(?,?,?,?,0,null)", (qid, r["id"], step, "open"))
            r["state"] = "WAITING"
            self._log(r, step=step, result="queued", item=qid)
            self.audit.record("workflow", "queue.opened", f"Task/{qid}")
            return
        self._advance(r, step)

    def _advance(self, r, step):
        self._log(r, step=step, result="ok")
        nxt = STEPS.index(step) + 1
        if nxt == len(STEPS):
            r["state"] = "DONE"
            self.db.execute("update referrals set state='DONE' where id=?", (r["id"],))
            self.audit.record("workflow", "referral.done", f"ServiceRequest/{r['id']}")
        else:
            self.bus.publish("saga.step", {"referral_id": r["id"], "step": STEPS[nxt]}, f"{r['id']}:{STEPS[nxt]}")

    def _compensate(self, r, failed, reason):
        done = [h["step"] for h in json.loads(r["history"]) if h.get("result") == "ok"]
        for s in reversed(done):
            undo = getattr(self.svc, "undo_" + s, None)
            if undo:
                undo(r)
        r["state"] = "COMPENSATED"
        self._log(r, step=failed, result="rejected", reason=reason)
        self.audit.record("workflow", "referral.compensated", f"ServiceRequest/{r['id']}")

    # work-item queue: the surface an RPA bot or agent polls
    def claim(self, now):
        row = self.db.execute(
            "select * from queue where status='open' or (status='claimed' and lease_until<?)"
            " order by rowid limit 1", (now,)
        ).fetchone()
        if not row:
            return None
        self.db.execute("update queue set status='claimed',lease_until=? where id=?", (now + LEASE, row["id"]))
        r = self.get(row["referral_id"])
        return {"item": fhir.task(row["id"], r["id"], row["kind"]),
                "input": {"payer": r["payer"], "member_id": r["member_id"]}}

    def complete(self, item_id, result, now):
        if result not in ("active", "inactive"):
            raise ValueError("invalid result")
        row = self.db.execute("select * from queue where id=?", (item_id,)).fetchone()
        if not row or row["status"] != "claimed" or row["lease_until"] < now:
            raise ValueError("item not claimed or lease expired")
        self.db.execute("update queue set status='done',result=? where id=?", (result, item_id))
        r = self.get(row["referral_id"])
        r["state"] = "RUNNING"
        r["history"] = json.dumps([h for h in json.loads(r["history"]) if h.get("item") != item_id])
        self.audit.record("bot", "queue.completed", f"Task/{item_id}")
        if result == "inactive":
            return self._compensate(r, row["kind"], "inactive_coverage")
        self.db.execute("update referrals set state='RUNNING',history=? where id=?", (r["history"], r["id"]))
        self._advance(r, row["kind"])
