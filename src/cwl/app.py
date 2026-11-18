"""Wires the pieces together. One lock serializes access to the shared SQLite connection."""
import sqlite3
import threading
import time

from . import fhir, security
from .bus import Bus
from .workflow import Services, Workflow


class App:
    def __init__(self, secret, path=":memory:", services=None):
        self.secret = secret
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.audit = security.Audit(self.db)
        self.bus = Bus(self.db)
        self.svc = services or Services()
        self.wf = Workflow(self.db, self.bus, self.audit, self.svc)

    def create_referral(self, intake, idem_key=None):
        fhir.validate_intake(intake)
        with self.lock, self.db:
            return self.wf.start(intake, idem_key)

    def pump(self, now=None):
        with self.lock:
            return self.bus.pump(now)

    def view(self, rid):
        with self.lock:
            return self.wf.view(rid)

    def claim(self, now=None):
        with self.lock, self.db:
            return self.wf.claim(time.time() if now is None else now)

    def complete(self, item_id, result, now=None):
        with self.lock, self.db:
            self.wf.complete(item_id, result, time.time() if now is None else now)

    def drain(self, start=0.0, step=100.0, rounds=20):
        """Pump with a synthetic clock so backoff does not need real sleeping."""
        now = start
        for _ in range(rounds):
            if not self.pump(now):
                break
            now += step
        return now
