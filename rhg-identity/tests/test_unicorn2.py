"""Unicorn 2.0: the registers (findings, decisions, actions, rules), the detectors and the agent loop."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("SERVICE_TOKEN", "test")

import config  # noqa: E402

_tmp = tempfile.mkdtemp()
config.DB_PATH = Path(_tmp) / "identity.db"

import store  # noqa: E402
import unicorn_registers as reg  # noqa: E402
import unicorn_detect as detect  # noqa: E402
import unicorn_agent as agent  # noqa: E402

reg.init()

FACTS = {
    "day": "2026-11-14",
    "finance": {
        "sales": {"month_from": "2026-11-01", "last_year_day": "2025-11-15", "restaurants": [
            {"ref": "LIFT", "name": "LIFT", "company": "SALT", "currency": "ALL", "yesterday": 700000,
             "same_weekday_last_year": 1000000, "vs_last_year_pct": -30.0, "month_to_date": 9000000,
             "month_to_date_budget": 12000000, "mtd_vs_budget_pct": -25.0, "discounts_pct_of_gross": 11.0,
             "voids_pct_of_gross": 1.0},
            {"ref": "OPA_B", "name": "OPA Blloku", "company": "SALT", "currency": "ALL", "yesterday": 400000,
             "same_weekday_last_year": 380000, "vs_last_year_pct": 5.3, "discounts_pct_of_gross": None},
        ]},
        "pl": [{"company": "SALT", "currency": "ALL", "month": "2026-10", "departments": [
            {"code": "LIFT", "name": "LIFT", "actual": {"sales": 30000000, "food_pct": 34.0, "labor_pct": 25.0, "ebitda": 2000000},
             "budget": {"sales": 31000000, "food_pct": 30.0, "labor_pct": 24.5, "ebitda": 4000000}}]}],
        "payables_receivables": [{"company": "SALT", "currency": "ALL",
                                  "payables": {"open": 10000000, "beyond_critical": 2500000, "critical_days": 90},
                                  "receivables": {"open": 500000, "beyond_critical": 0, "critical_days": 90}}],
    },
    "purchasing": {"companies": [{"company": "SALT", "currency": "ALL", "largest_price_movers": [
        {"item": "Fileto vici", "previous_average_cost": 1500, "current_average_cost": 1800, "change_pct": 20.0, "impact": 240000},
        {"item": "Kripe", "change_pct": 40.0, "impact": 2000}],
        "invoices_without_po_yesterday": {"invoices": 20, "without_order": 3, "value_without_order": 150000, "rate_pct": 15.0}}]},
}


class Detectors(unittest.TestCase):
    def test_breaches_become_findings_with_impact(self):
        got = {f["rule"]: f for f in detect.detect(FACTS)}
        self.assertEqual(got["sales.drop_vs_last_year"]["impact"], 300000)
        self.assertEqual(got["sales.drop_vs_last_year"]["severity"], "high")
        self.assertEqual(got["sales.mtd_vs_budget"]["impact"], 3000000)
        self.assertEqual(got["sales.discounts_pct"]["impact"], 21000)           # (11 − 8)% × 700k
        self.assertNotIn("sales.voids_pct", got)
        self.assertEqual(got["pl.food_vs_budget"]["impact"], 1200000)          # 4 pts × 30 M
        self.assertNotIn("pl.labor_vs_budget", got)                             # 0.5 pts, under 2
        self.assertEqual(got["pl.ebitda_vs_budget"]["value"], -50.0)
        self.assertEqual(got["apar.payables_beyond_critical"]["impact"], 2500000)
        self.assertNotIn("apar.receivables_beyond_critical", got)
        self.assertEqual(got["purchasing.price_mover"]["location_name"], "Fileto vici")   # Kripe under min impact
        self.assertEqual(got["purchasing.invoices_without_po"]["value"], 15.0)
        self.assertEqual(got["sales.drop_vs_last_year"]["evidence"]["rule_status"], "Proposed")

    def test_missing_figures_raise_nothing(self):
        f = {"day": "2026-11-14", "finance": {"sales": {"restaurants": [{"ref": "X", "name": "X", "yesterday": None,
                                                                          "vs_last_year_pct": None}]}}}
        self.assertEqual(detect.detect(f), [])

    def test_an_approved_rule_moves_the_limit(self):
        reg.save_rule({"key": "sales.discounts_pct", "kind": "threshold", "title": "Discounts", "value": {"max": 12}},
                      "cfo", approve=True)
        try:
            self.assertNotIn("sales.discounts_pct", {f["rule"] for f in detect.detect(FACTS)})
        finally:
            reg.save_rule({"key": "sales.discounts_pct", "kind": "threshold", "title": "Discounts", "value": {"max": 8}},
                          "cfo", approve=True)

    def test_same_problem_seen_again_is_one_finding(self):
        seen1, new1 = detect.run(FACTS)
        seen2, new2 = detect.run(FACTS)
        self.assertTrue(new1)
        self.assertEqual(new2, [])
        self.assertEqual(sorted(seen1), sorted(seen2))
        f = reg.finding(seen1[0])
        self.assertEqual(f["seen_count"], 2)


class Lifecycle(unittest.TestCase):
    def test_propose_approve_measure_close(self):
        detect.run(FACTS)
        f = next(x for x in reg.findings("open") if x["rule"] == "sales.discounts_pct")
        did = reg.propose(f["id"], {"decision": "Kufizo zbritjet e menaxherit te LIFT", "summary": "s", "confidence": "high",
                                    "evidence": [{"fact": "11%", "source": "sales 2026-11-14"}], "impact_amount": 600000,
                                    "impact_formula": "3% × 20 M"})
        self.assertEqual(reg.finding(f["id"])["status"], "Proposed")
        with self.assertRaises(ValueError):
            reg.approve(did, "cfo", "", "2026-12-01")                        # no owner
        ids = reg.approve(did, "cfo", "Drejtori i LIFT", "2026-12-01", 8)
        a = reg.actions("open")
        a = next(x for x in a if x["id"] == ids[0])
        self.assertEqual((a["baseline"], a["target"], a["metric"]), (11.0, 8.0, "discounts_pct_of_gross"))
        later = {"day": "2026-11-20", "finance": {"sales": {"restaurants": [dict(FACTS["finance"]["sales"]["restaurants"][0],
                                                                                 discounts_pct_of_gross=7.5)]}}}
        self.assertGreaterEqual(detect.follow_up(later), 1)
        a = next(x for x in reg.actions() if x["id"] == ids[0])
        self.assertEqual((a["latest"], a["effect"]), (7.5, -3.5))
        reg.update_action(ids[0], "drejtori", "Done", "U zbatua")
        self.assertEqual(reg.decision(did)["status"], "Done")
        self.assertEqual(reg.finding(f["id"])["status"], "Closed")

    def test_refusal_needs_a_reason_and_is_remembered(self):
        detect.run(FACTS)
        f = next(x for x in reg.findings("open") if x["rule"] == "purchasing.invoices_without_po")
        did = reg.propose(f["id"], {"decision": "Blloko faturat pa PO", "confidence": "low"})
        with self.assertRaises(ValueError):
            reg.refuse(did, "coo", " ")
        reg.refuse(did, "coo", "Furnitorët e shërbimeve nuk kanë PO me politikë")
        self.assertEqual(reg.finding(f["id"])["status"], "Dismissed")
        hist = reg.similar_history(dict(f, id=0))
        self.assertTrue(any("politikë" in (h.get("reason") or "") for h in hist))

    def test_dismiss_needs_reason(self):
        detect.run(FACTS)
        f = reg.findings("open")[0]
        with self.assertRaises(ValueError):
            reg.set_finding(f["id"], "Dismissed", "cfo", "")


class Delegate(unittest.TestCase):
    def test_a_person_hands_a_finding_to_an_owner(self):
        detect.run(FACTS)
        f = next(x for x in reg.findings("open") if x["rule"] == "pl.ebitda_vs_budget")
        with self.assertRaises(ValueError):
            reg.delegate(f["id"], "cfo", "", "2026-12-01")                    # no owner
        did, ids = reg.delegate(f["id"], "cfo", "Ana Marku <ana@rhg.al>", "2026-12-01", "Gjej shkakun", 2500000, "brenda dhjetorit")
        d = reg.decision(did)
        self.assertEqual((d["status"], d["owner"], d["proposed_by"], d["meta"]["delegated"]),
                         ("Approved", "Ana Marku <ana@rhg.al>", "cfo", True))
        a = next(x for x in reg.actions() if x["id"] == ids[0])
        self.assertEqual((a["title"], a["baseline"], a["target"], a["metric"]), ("Gjej shkakun", -50.0, 2500000.0, "ebitda_vs_budget_pct"))

    def test_not_while_unicorns_proposal_waits(self):
        detect.run(FACTS)
        f = next(x for x in reg.findings("open") if x["rule"] == "purchasing.price_mover")
        reg.propose(f["id"], {"decision": "Rinegocio", "confidence": "medium"})
        with self.assertRaises(ValueError):
            reg.delegate(f["id"], "cfo", "Ana <ana@rhg.al>", "2026-12-01")


class Rules(unittest.TestCase):
    def test_versions_proposals_wait(self):
        r = reg.save_rule({"key": "policy.po_required", "kind": "policy", "title": "PO mbi 50k", "value": {"min": 50000}},
                          "unicorn", approve=False)
        cur = next(x for x in reg.rules() if x["key"] == "policy.po_required")
        self.assertEqual(cur["status"], "Proposed")
        reg.save_rule({"key": "policy.po_required", "kind": "policy", "title": "PO mbi 50k", "value": {"min": 50000}},
                      "cfo", approve=True)
        reg.save_rule({"key": "policy.po_required", "kind": "policy", "title": "PO mbi 30k", "value": {"min": 30000}},
                      "unicorn", approve=False)
        cur = next(x for x in reg.rules() if x["key"] == "policy.po_required")
        self.assertEqual((cur["status"], cur["value"]["min"], cur["pending"]["value"]["min"]), ("Approved", 50000, 30000))
        self.assertEqual(r["version"], 1)
        with self.assertRaises(ValueError):
            reg.save_rule({"key": "Bad Key", "title": "x"}, "cfo", approve=True)
        with self.assertRaises(ValueError):
            reg.save_rule({"key": "x.y", "title": "x", "value": "not json"}, "cfo", approve=True)


class AgentLoop(unittest.TestCase):
    def test_tools_then_proposal(self):
        calls = []

        def fake_api(payload):
            calls.append(payload)
            if len(calls) == 1:
                return {"content": [{"type": "tool_use", "id": "t1", "name": "rules_in_force", "input": {}}],
                        "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}}
            return {"content": [{"type": "tool_use", "id": "t2", "name": "submit_proposal",
                                 "input": {"summary": "s", "evidence": [], "decision": "d", "confidence": "medium"}}],
                    "stop_reason": "tool_use", "usage": {"input_tokens": 20, "output_tokens": 7}}
        text, prop, usage, n = agent.loop("sys", [{"role": "user", "content": "go"}], agent.investigation_specs(),
                                          {"context": "finding:1", "actor": "t"}, "m", 1000, finish="submit_proposal",
                                          api=fake_api)
        self.assertEqual((prop["decision"], n, usage["input_tokens"]), ("d", 1, 30))
        self.assertEqual(calls[1]["messages"][-1]["content"][0]["type"], "tool_result")
        self.assertTrue(reg.tool_calls("finding:1"))

    def test_a_tool_not_open_is_refused(self):
        def fake_api(payload):
            if payload["messages"][-1]["role"] == "user" and isinstance(payload["messages"][-1]["content"], list):
                return {"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
            return {"content": [{"type": "tool_use", "id": "t1", "name": "pl_month", "input": {"company": "SALT"}}],
                    "stop_reason": "tool_use"}
        specs = agent.chat_specs(["unicorn.use"])                           # no finance.view
        self.assertNotIn("pl_month", {t["name"] for t in specs})
        text, _p, _u, n = agent.loop("s", [{"role": "user", "content": "q"}], specs, {"context": "chat:1", "actor": "x"},
                                     "m", 100, api=fake_api)
        self.assertEqual(text, "ok")
        self.assertFalse(reg.tool_calls("chat:1")[0]["ok"])

    def test_the_read_limit_forces_the_end(self):
        seen = []

        def fake_api(payload):
            seen.append(payload.get("tool_choice"))
            if payload.get("tool_choice"):
                return {"content": [{"type": "tool_use", "id": "z", "name": "submit_proposal",
                                     "input": {"summary": "s", "evidence": [], "decision": "d", "confidence": "low"}}]}
            return {"content": [{"type": "tool_use", "id": f"t{len(seen)}", "name": "rules_in_force", "input": {}}]}
        _t, prop, _u, n = agent.loop("s", [{"role": "user", "content": "q"}], agent.investigation_specs(),
                                     {"context": "finding:2", "actor": "x"}, "m", 100, max_calls=2,
                                     finish="submit_proposal", api=fake_api)
        self.assertEqual(prop["decision"], "d")
        self.assertEqual(seen[-1], {"type": "tool", "name": "submit_proposal"})


if __name__ == "__main__":
    unittest.main()
