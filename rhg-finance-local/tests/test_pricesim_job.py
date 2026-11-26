"""Price simulator: the read is one job on the server; it goes on whatever the page does (26 Nov 2026)."""
import time

import server


def test_the_job_runs_on_and_keeps_its_answer(monkeypatch):
    calls = {"n": 0}

    def req(per, latest, client, key, refs, refresh):
        calls["n"] += 1
        return {"pending": True, "done": 5, "total": 10} if calls["n"] < 3 else {"rows": {}, "locations": []}
    monkeypatch.setattr(server.menueng, "request", req)
    monkeypatch.setattr(server.pricesim, "build", lambda ds, *a, **k: {"items": [], "elasticity": {"running": False}})
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    server._ps_jobs.clear()
    q = {"period": "2026-10-01", "mode": "month", "level": "all"}
    first = server._pricesim_request(None, ("a", "b", None), None, q, None)
    assert first["pending"]
    for _ in range(200):
        r = server._pricesim_request(None, ("a", "b", None), None, q, None)
        if not r.get("pending"):
            break
        time.sleep(0.01)
    assert r["items"] == [] and "plan_options" in r and calls["n"] >= 3
    n = calls["n"]
    assert server._pricesim_request(None, ("a", "b", None), None, q, None) is r and calls["n"] == n     # kept
