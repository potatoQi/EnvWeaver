"""Session-scoped background operations."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import RLock

from .store import Missing, now


class Jobs:
    def __init__(self):
        self.lock = RLock()
        self.records = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="envweaver-management")

    def close(self):
        self.executor.shutdown(wait=True)

    def submit(self, kind, actor, operation):
        job_id = uuid.uuid4().hex
        with self.lock:
            self.records[job_id] = dict(
                id=job_id,
                kind=kind,
                actor=actor,
                status="pending",
                result=None,
                error=None,
                created_at=now(),
                updated_at=now(),
            )
            completed = [
                key
                for key, row in self.records.items()
                if row["status"] not in {"pending", "running"}
            ]
            for key in completed[:-50]:
                del self.records[key]
        self.executor.submit(self.run, job_id, operation)
        return self.get(job_id)

    def update(self, job_id, **values):
        with self.lock:
            self.records[job_id].update(deepcopy(values), updated_at=now())

    def run(self, job_id, operation):
        try:
            self.update(job_id, status="running")
            result = operation()
            self.update(
                job_id, status="partial" if result.get("errors") else "completed", result=result
            )
        except Exception as error:
            self.update(job_id, status="failed", error=str(error))

    def get(self, job_id):
        with self.lock:
            if job_id not in self.records:
                raise Missing("操作记录不存在")
            return deepcopy(self.records[job_id])

    def list(self):
        with self.lock:
            return deepcopy(
                sorted(self.records.values(), key=lambda r: r["created_at"], reverse=True)[:50]
            )
