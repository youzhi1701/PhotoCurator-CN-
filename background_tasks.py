#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PhotoCurator durable background task queue.

The UI submits intent; workers perform filesystem/database work later. Tasks
are persisted so an unexpected restart cannot silently lose accepted work.
"""

from __future__ import annotations
import heapq, json, logging, sqlite3, threading, time
from pathlib import Path
from typing import Callable, Dict

logger = logging.getLogger(__name__)

class BackgroundTaskManager:
    def __init__(self, db_path, workers=2):
        self.db_path = Path(db_path)
        self.workers = max(1, int(workers))
        self.handlers: Dict[str, Callable[[dict], object]] = {}
        self._heap, self._seq, self._stop = [], 0, False
        self._cv = threading.Condition()
        self._init_db()
        self._recover_interrupted()
        for i in range(self.workers):
            threading.Thread(target=self._worker, daemon=True,
                             name=f"photocurator-task-{i+1}").start()

    def _connect(self):
        return sqlite3.connect(str(self.db_path), timeout=30)

    def _init_db(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS background_task (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 50,
                state TEXT NOT NULL,
                idempotency_key TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                error TEXT,
                result_json TEXT
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_background_task_idem
                          ON background_task(idempotency_key)
                          WHERE idempotency_key IS NOT NULL""")
            db.execute("""CREATE INDEX IF NOT EXISTS idx_background_task_state
                          ON background_task(state, priority, id)""")
            db.commit()

    def _recover_interrupted(self):
        now = time.time()
        with self._connect() as db:
            db.execute("UPDATE background_task SET state='queued',updated_at=?,error=NULL WHERE state='running'", (now,))
            rows = db.execute("SELECT id,priority FROM background_task WHERE state='queued' ORDER BY priority,id").fetchall()
            db.commit()
        with self._cv:
            for task_id, priority in rows:
                self._seq += 1
                heapq.heappush(self._heap, (int(priority), self._seq, int(task_id)))

    def register(self, kind, handler):
        self.handlers[str(kind)] = handler

    def enqueue(self, kind, payload, priority=50, idempotency_key=None):
        now = time.time()
        with self._connect() as db:
            if idempotency_key:
                row = db.execute("SELECT id,state FROM background_task WHERE idempotency_key=?", (str(idempotency_key),)).fetchone()
                if row and row[1] in ("queued", "running"):
                    return int(row[0]), False
                if row:
                    db.execute("DELETE FROM background_task WHERE id=?", (int(row[0]),))
            cur = db.execute(
                """INSERT INTO background_task
                   (kind,payload_json,priority,state,idempotency_key,created_at,updated_at)
                   VALUES(?,?,?,'queued',?,?,?)""",
                (str(kind), json.dumps(payload or {}, ensure_ascii=False), int(priority),
                 str(idempotency_key) if idempotency_key else None, now, now),
            )
            task_id = int(cur.lastrowid)
            db.commit()
        with self._cv:
            self._seq += 1
            heapq.heappush(self._heap, (int(priority), self._seq, task_id))
            self._cv.notify()
        return task_id, True

    def _next_task(self):
        with self._cv:
            while not self._stop and not self._heap:
                self._cv.wait(timeout=1.0)
            if self._stop:
                return None
            return heapq.heappop(self._heap)[2]

    def _worker(self):
        while not self._stop:
            task_id = self._next_task()
            if task_id is None:
                continue
            try:
                with self._connect() as db:
                    row = db.execute("SELECT kind,payload_json,state FROM background_task WHERE id=?", (task_id,)).fetchone()
                    if not row or row[2] != "queued":
                        continue
                    db.execute("UPDATE background_task SET state='running',updated_at=? WHERE id=?", (time.time(), task_id))
                    db.commit()
                kind, payload_json, _ = row
                handler = self.handlers.get(kind)
                if handler is None:
                    raise RuntimeError(f"未注册后台任务处理器：{kind}")
                result = handler(json.loads(payload_json or "{}"))
                with self._connect() as db:
                    db.execute("UPDATE background_task SET state='done',updated_at=?,error=NULL,result_json=? WHERE id=?",
                               (time.time(), json.dumps(result, ensure_ascii=False, default=str), task_id))
                    db.commit()
            except Exception as exc:
                logger.exception("background task %s failed", task_id)
                try:
                    with self._connect() as db:
                        db.execute("UPDATE background_task SET state='failed',updated_at=?,error=? WHERE id=?",
                                   (time.time(), str(exc), task_id))
                        db.commit()
                except Exception:
                    logger.exception("failed to persist task failure")

    def summary(self):
        with self._connect() as db:
            rows = db.execute("SELECT state,COUNT(*) FROM background_task GROUP BY state").fetchall()
            latest = db.execute("""SELECT id,kind,state,priority,created_at,updated_at,error
                                   FROM background_task ORDER BY id DESC LIMIT 30""").fetchall()
        counts = {str(k): int(v) for k, v in rows}
        return {
            "counts": counts,
            "active": counts.get("queued", 0) + counts.get("running", 0),
            "items": [{"id": int(r[0]), "kind": r[1], "state": r[2], "priority": int(r[3]),
                       "created_at": float(r[4]), "updated_at": float(r[5]), "error": r[6] or ""}
                      for r in latest],
        }

    def get(self, task_id):
        with self._connect() as db:
            row = db.execute("""SELECT id,kind,state,priority,created_at,updated_at,error,result_json
                                FROM background_task WHERE id=?""", (int(task_id),)).fetchone()
        if not row:
            return None
        result = None
        if row[7]:
            try: result = json.loads(row[7])
            except Exception: result = row[7]
        return {"id": int(row[0]), "kind": row[1], "state": row[2], "priority": int(row[3]),
                "created_at": float(row[4]), "updated_at": float(row[5]), "error": row[6] or "", "result": result}

    def shutdown(self):
        self._stop = True
        with self._cv:
            self._cv.notify_all()
