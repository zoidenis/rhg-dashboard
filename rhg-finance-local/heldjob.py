"""A read that takes long, held: the last result at once, a new read behind it.

The reconciliations (Simphony ↔ BC, Takings POS ↔ BC, Simphony ↔ Billy…) take from seconds to minutes. A screen
asks; it gets the last result at once, with reading: True while a new one runs (when the last is older than keep,
or a person asked to read again), and asks again until reading is False. The last result is also kept on disk, so
a restart or a deploy does not empty the screen. Only when nothing was ever read does a screen wait ({"pending"}).
"""
import gzip
import json
import re
import threading
import time
import traceback

import store


class Held:
    def __init__(self, name, keep=900):
        self.name, self.keep = name, keep
        self.results, self.jobs, self.lock = {}, {}, threading.Lock()

    def _file(self, key):
        return store.DATA_DIR / f"{self.name}_{re.sub(r'[^A-Za-z0-9_-]+', '_', str(key))}.json.gz"

    def held(self, key):
        with self.lock:
            hit = self.results.get(key)
        if hit:
            return hit[1]
        try:
            with gzip.open(self._file(key), "rt", encoding="utf-8") as f:
                r = json.load(f)
        except (OSError, ValueError):
            return None
        with self.lock:
            self.results.setdefault(key, (0.0, r))           # age 0: shown at once, read again behind it
        return r

    def remember(self, key, r):
        with self.lock:
            self.results[key] = (time.time(), r)
        try:
            tmp = self._file(key).with_suffix(".tmp")
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=5) as f:
                json.dump(r, f, default=str)
            tmp.replace(self._file(key))
        except (OSError, TypeError, ValueError):
            traceback.print_exc()

    def forget(self, key=None):
        with self.lock:
            if key is None:
                self.results.clear()
            else:
                self.results.pop(key, None)

    def request(self, key, build, refresh=False, after=None, note=""):
        """build(progress) → result; after(result) → what goes in result["register"] (or None)."""
        last = self.held(key)
        with self.lock:
            hit, job = self.results.get(key), self.jobs.get(key)
            running = bool(job and job["state"] == "running")
            fresh = bool(hit and time.time() - hit[0] < self.keep)
            if job and job["state"] == "failed" and not refresh and time.time() - job["at"] < 60:
                self.jobs.pop(key, None)
                return dict(last, reading=False, read_error=job["error"]) if isinstance(last, dict) else \
                    {"available": False, "error": job["error"]}
            if not running and (refresh or not fresh):
                job = self.jobs[key] = {"state": "running", "stage": "starting", "at": time.time(), "error": ""}
                running = True
                threading.Thread(target=self._run, args=(key, job, build, after), daemon=True,
                                 name=f"{self.name}-{key}").start()
            stage = job["stage"] if running and job else ""
        if isinstance(last, dict):
            return dict(last, reading=running, stage=stage)
        if running:
            return {"pending": True, "stage": stage or "starting", "note": note}
        return {"available": False, "error": "Not read yet."}

    def _run(self, key, job, build, after):
        try:
            r = build(lambda st: job.update(stage=st))
            if after:
                try:
                    r["register"] = after(r)
                except Exception:                                  # noqa: BLE001
                    traceback.print_exc()
            self.remember(key, r)
            job["state"] = "done"
        except Exception as exc:                                   # noqa: BLE001
            traceback.print_exc()
            job.update(state="failed", error=str(exc)[:300], at=time.time())
