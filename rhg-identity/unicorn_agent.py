"""Unicorn as an agent (Layer 3 of the Unicorn 2.0 architecture): Claude with read-only tools over the towers and
pulse's own registers.

Tools come in three groups:
  * tower tools (Finance, Purchasing): figures read on demand through /internal/unicorn/tool with the service token;
    they only read, as the towers' screens do;
  * register tools (identity): open findings, the rules in force, what people decided before;
  * recording tools: submit_proposal (an investigation ends with one), raise_finding (a problem met in a
    conversation goes to the Findings register), propose_rule_change (a new version of a rule that waits for a
    person to approve it). These write only to pulse's registers, never to a source system.

A person's question uses the tools their role opens (Finance tools with finance.view, Purchasing tools with
purchasing.view, food safety with an Operations & Food Safety screen). Every call is kept in the tool trail.
"""
import json
import time
import urllib.error
import urllib.request

import config
import unicorn_registers as reg

FINANCE_URL = config.get("FINANCE_URL", "http://finance:8766").rstrip("/")
PURCHASING_URL = config.get("PURCHASING_URL", "http://purchasing:8765").rstrip("/")
TOOL_WAIT = int(config.get("UNICORN_TOOL_WAIT", "240"))
RESULT_CHARS = 24000

_DATE = {"type": "string", "description": "YYYY-MM-DD"}
_CO = {"type": "string", "description": "Business Central company, e.g. SALT (list_restaurants gives them)"}
_PERIOD = {"type": "string", "enum": ["week", "month", "quarter", "ytd"],
           "description": "the period that holds the date (week, month, quarter or year to date)"}

TOOLS = [
    {"name": "list_restaurants", "tower": "finance", "need": "finance.view",
     "description": "The restaurants of pulse with their ref, brand, company, BC location, department and currency. "
                    "Use first to know the refs and companies.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "kpi_definitions", "tower": "finance", "need": "finance.view",
     "description": "The KPI library of pulse: the formula of every financial KPI.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "sales", "tower": "finance", "need": "finance.view",
     "description": "Simphony sales per restaurant for a range of days (net ex VAT, checks, guests, average check, "
                    "discounts and voids as % of gross) against the same weekdays last year or the period before. "
                    "At most 92 days. Optional restaurants (refs or names).",
     "input_schema": {"type": "object", "required": ["date_from", "date_to"], "properties": {
         "date_from": _DATE, "date_to": _DATE, "restaurants": {"type": "array", "items": {"type": "string"}},
         "compare": {"type": "string", "enum": ["last_year", "prior", "none"]}}}},
    {"name": "sales_by_day", "tower": "finance", "need": "finance.view",
     "description": "One restaurant's Simphony sales day by day (net ex VAT, checks, guests, discounts, voids) with "
                    "the same weekday last year. At most 62 days.",
     "input_schema": {"type": "object", "required": ["restaurant", "date_from", "date_to"], "properties": {
         "restaurant": {"type": "string"}, "date_from": _DATE, "date_to": _DATE}}},
    {"name": "pl_month", "tower": "finance", "need": "finance.view",
     "description": "The P&L of a company for a closed month by department (sales, food, labour, opex, EBITDA and "
                    "their % of sales) against budget and last year, with the largest account variances.",
     "input_schema": {"type": "object", "required": ["company"], "properties": {
         "company": _CO, "month": {"type": "string", "description": "YYYY-MM; default the last closed month"}}}},
    {"name": "payables_receivables", "tower": "finance", "need": "finance.view",
     "description": "Open, overdue and beyond-critical payables and receivables of a company, the largest parties "
                    "and the Kartela exceptions.",
     "input_schema": {"type": "object", "required": ["company"], "properties": {"company": _CO}}},
    {"name": "food_safety", "tower": "finance", "need": "finance.s.opsexec.",
     "description": "Crunchtime Ops Execution by restaurant for a range of days: checklist completion, temperatures "
                    "in range, sensors out of range now, frying oil, products refused on arrival by supplier, spoil "
                    "entries, yield. At most 120 days.",
     "input_schema": {"type": "object", "required": ["date_from", "date_to"], "properties": {
         "date_from": _DATE, "date_to": _DATE}}},
    {"name": "purchases_overview", "tower": "purchasing", "need": "purchasing.view",
     "description": "Purchases of a company in the period holding the date: spend against the comparison period, "
                    "price rises and savings, the largest price movers and spreads, the tower's own brief.",
     "input_schema": {"type": "object", "required": ["company", "date"], "properties": {
         "company": _CO, "date": _DATE, "period": _PERIOD}}},
    {"name": "supplier_summary", "tower": "purchasing", "need": "purchasing.view",
     "description": "Net purchases by supplier in the period holding the date, share, change, credit notes, "
                    "concentration and lost suppliers.",
     "input_schema": {"type": "object", "required": ["company", "date"], "properties": {
         "company": _CO, "date": _DATE, "period": _PERIOD}}},
    {"name": "item_prices", "tower": "purchasing", "need": "purchasing.view",
     "description": "Item purchase prices in the period holding the date: average, min, max, last price, suppliers, "
                    "change against the comparison period. search filters by text; item (an item no.) adds its "
                    "price series, suppliers and latest purchases.",
     "input_schema": {"type": "object", "required": ["company", "date"], "properties": {
         "company": _CO, "date": _DATE, "period": _PERIOD, "search": {"type": "string"}, "item": {"type": "string"}}}},
    {"name": "invoices_without_po", "tower": "purchasing", "need": "purchasing.view",
     "description": "Purchase invoices posted without a purchase order in a range of days (at most 92).",
     "input_schema": {"type": "object", "required": ["company", "date_from", "date_to"], "properties": {
         "company": _CO, "date_from": _DATE, "date_to": _DATE}}},
    {"name": "open_findings", "tower": "local", "need": "unicorn.use",
     "description": "The findings in pulse's Findings register that are still open (optionally one restaurant or "
                    "company, or one capability).",
     "input_schema": {"type": "object", "properties": {"search": {"type": "string"},
                                                         "capability": {"type": "string", "enum": list(reg.CAPABILITIES)}}}},
    {"name": "rules_in_force", "tower": "local", "need": "unicorn.use",
     "description": "RHG's rules library: thresholds, policies, approved decisions and business knowledge in force "
                    "(and their approval status). Approved = RHG's rule; Proposed = not yet approved.",
     "input_schema": {"type": "object", "properties": {"kind": {"type": "string", "enum": list(reg.RULE_KINDS)}}}},
    {"name": "decision_history", "tower": "local", "need": "unicorn.use",
     "description": "What people decided before for findings of the same rule and place: dismissals and refusals "
                    "with their reasons, approved actions and their measured effect.",
     "input_schema": {"type": "object", "required": ["finding_id"], "properties": {"finding_id": {"type": "integer"}}}},
]

SUBMIT = {"name": "submit_proposal", "tower": "record", "need": "unicorn.use",
          "description": "Ends the investigation with a proposed decision for people to approve. Use only facts you "
                         "read; separate facts from the hypothesis; give the impact with its formula.",
          "input_schema": {"type": "object", "required": ["summary", "evidence", "decision", "confidence"], "properties": {
              "summary": {"type": "string", "description": "what happened and why it matters, 2-4 sentences"},
              "evidence": {"type": "array", "items": {"type": "object", "required": ["fact", "source"], "properties": {
                  "fact": {"type": "string"}, "source": {"type": "string", "description": "the tool and period read"}}}},
              "hypothesis": {"type": "string", "description": "the likely cause, marked as a hypothesis"},
              "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
              "decision": {"type": "string", "description": "the decision proposed, one sentence, an action verb"},
              "impact_amount": {"type": "number", "description": "expected monthly effect in the currency, if measurable"},
              "impact_currency": {"type": "string"},
              "impact_formula": {"type": "string", "description": "how the impact is computed, with the figures"},
              "alternatives": {"type": "array", "items": {"type": "string"}},
              "risks": {"type": "string"},
              "approver_role": {"type": "string", "description": "CEO, CFO, COO, Purchasing manager, Food safety manager…"},
              "due_days": {"type": "integer", "description": "days to carry it out"},
              "kpi": {"type": "string", "description": "the KPI that should move"},
              "target": {"type": "string", "description": "its target value"},
              "no_action_reason": {"type": "string", "description": "when no decision is needed (e.g. explained by a "
                                                                   "rule or approved decision), why"}}}}
RAISE = {"name": "raise_finding", "tower": "record", "need": "unicorn.use",
         "description": "Records a problem met in the conversation in pulse's Findings register, so it is followed up. "
                        "Only for a problem supported by figures you read.",
         "input_schema": {"type": "object", "required": ["title", "capability", "detail", "severity"], "properties": {
             "title": {"type": "string"}, "capability": {"type": "string", "enum": list(reg.CAPABILITIES)},
             "company": {"type": "string"}, "location": {"type": "string"}, "period": {"type": "string"},
             "detail": {"type": "string", "description": "the figures and their source"},
             "severity": {"type": "string", "enum": list(reg.SEVERITIES)},
             "impact": {"type": "number"}, "currency": {"type": "string"}}}}
RULE_CHANGE = {"name": "propose_rule_change", "tower": "record", "need": "unicorn.use",
               "description": "Proposes a new version of a rule (e.g. a threshold that keeps raising findings people "
                              "dismiss). It waits for a person with unicorn.rules.manage; you never change a rule.",
               "input_schema": {"type": "object", "required": ["key", "value", "reason"], "properties": {
                   "key": {"type": "string"}, "value": {"type": "object"}, "reason": {"type": "string"}}}}


def allowed(perms):
    """The tool specs a person's permissions open (perms None = the morning run: every tool)."""
    out = []
    for t in TOOLS:
        need = t["need"]
        if perms is None or (any(p.startswith(need) for p in perms) if need.endswith(".") else need in perms):
            out.append(t)
    return out


def _api_spec(t):
    return {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}


# ---------------------------------------------------------------- running a tool
def _tower(base, name, args):
    body = json.dumps({"name": name, "args": args}).encode("utf-8")
    t0 = time.time()
    while True:
        req = urllib.request.Request(base + "/internal/unicorn/tool", data=body, method="POST", headers={
            "Content-Type": "application/json", "X-Service-Token": config.SERVICE_TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                res = json.loads(r.read() or b"{}")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return {"available": False, "error": f"The tower could not be reached: {exc}"}
        if not res.get("pending"):
            return res
        if time.time() - t0 > TOOL_WAIT:
            return {"available": False, "error": f"Still reading after {TOOL_WAIT // 60} minutes ({res.get('stage')}); "
                                                 "ask again later."}
        time.sleep(8)


def _local(name, args, ctx):
    if name == "open_findings":
        rows = reg.findings("open", args.get("capability"), (args.get("search") or "").strip() or None, 60)
        return {"findings": [{k: f[k] for k in ("id", "title", "capability", "company", "location_name", "period",
                                                 "value", "limit", "unit", "impact", "currency", "severity", "status",
                                                 "first_seen", "last_seen", "seen_count")} for f in rows]}
    if name == "rules_in_force":
        rows = [r for r in reg.rules() if not args.get("kind") or r["kind"] == args["kind"]]
        return {"rules": [{k: r[k] for k in ("key", "kind", "title", "text", "value", "applies", "status", "version",
                                              "approved_by", "approved_at")} for r in rows]}
    if name == "decision_history":
        f = reg.finding(args.get("finding_id") or 0)
        return {"history": reg.similar_history(f) if f else [], "finding": f and f["title"]}
    if name == "raise_finding":
        cap = args.get("capability") if args.get("capability") in reg.CAPABILITIES else "executive"
        f = {"rule": "chat." + cap, "capability": cap, "company": (args.get("company") or "")[:40],
             "location": (args.get("location") or "")[:60], "location_name": (args.get("location") or "")[:120],
             "period": (args.get("period") or time.strftime("%Y-%m-%d"))[:20], "title": str(args.get("title") or "")[:200],
             "detail": str(args.get("detail") or "")[:2000], "impact": args.get("impact"),
             "currency": (args.get("currency") or "")[:8],
             "severity": args.get("severity") if args.get("severity") in reg.SEVERITIES else "medium",
             "evidence": {"raised_in": ctx.get("context"), "by": ctx.get("actor")}}
        fid, new = reg.upsert_finding(f, time.strftime("%Y-%m-%d"))
        return {"finding_id": fid, "new": new}
    if name == "propose_rule_change":
        cur = next((r for r in reg.rules() if r["key"] == args.get("key")), None)
        if not cur:
            return {"available": False, "error": "No such rule; rules_in_force lists them."}
        r = reg.save_rule({"key": cur["key"], "kind": cur["kind"], "title": cur["title"], "text": cur["text"],
                           "value": args.get("value") or {}, "applies": cur["applies"],
                           "reason": "Unicorn: " + str(args.get("reason") or "")[:900]}, "unicorn", approve=False)
        return {"proposed": r, "note": "Waits for a person with unicorn.rules.manage."}
    return {"available": False, "error": f"No tool {name}."}


def run_tool(name, args, ctx):
    spec = next((t for t in TOOLS + [RAISE, RULE_CHANGE] if t["name"] == name), None)
    t0 = time.time()
    if spec is None or name not in ctx["names"]:
        res = {"available": False, "error": f"The tool {name} is not open to this conversation."}
    elif spec["tower"] == "finance":
        res = _tower(FINANCE_URL, name, args)
    elif spec["tower"] == "purchasing":
        res = _tower(PURCHASING_URL, name, args)
    else:
        try:
            res = _local(name, args, ctx)
        except ValueError as exc:
            res = {"available": False, "error": str(exc)}
    ok = res.get("available") is not False
    summary = res.get("error") if not ok else ", ".join(f"{k}: {len(v)}" for k, v in res.items() if isinstance(v, list))[:300]
    reg.log_tool(ctx.get("context"), ctx.get("actor"), name, args, ok, (time.time() - t0) * 1000, summary)
    text = json.dumps(res, ensure_ascii=False, default=str, separators=(",", ":"))
    if len(text) > RESULT_CHARS:
        text = text[:RESULT_CHARS] + '…(cut: ask a narrower question)'
    return text, ok


# ---------------------------------------------------------------- the loop
def _api(payload):
    req = urllib.request.Request(config.get("ANTHROPIC_URL", "https://api.anthropic.com/v1/messages"),
                                 data=json.dumps(payload).encode("utf-8"), method="POST", headers={
                                     "content-type": "application/json", "x-api-key": config.get("ANTHROPIC_API_KEY", ""),
                                     "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=280) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = (json.loads(exc.read() or b"{}").get("error") or {}).get("message") or ""
        except ValueError:
            pass
        raise RuntimeError(f"The Anthropic API answered {exc.code}: {detail[:200]}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"The Anthropic API could not be reached: {exc}") from None


def loop(system, messages, specs, ctx, model, max_tokens, effort=None, max_calls=12, finish=None, api=None):
    """Claude with tools until it answers (or calls `finish`). Returns (text, finish_input or None, usage, calls)."""
    api = api or _api
    tools = [_api_spec(t) for t in specs]
    ctx["names"] = {t["name"] for t in specs}
    usage = {"input_tokens": 0, "output_tokens": 0}
    calls = 0
    msgs = list(messages)
    for _turn in range(max_calls + 3):
        payload = {"model": model, "max_tokens": max_tokens, "system": system, "messages": msgs, "tools": tools}
        if calls >= max_calls:
            payload["tool_choice"] = {"type": "tool", "name": finish} if finish else {"type": "none"}
        elif effort:
            payload["output_config"] = {"effort": effort}
        j = api(payload)
        u = j.get("usage") or {}
        for k in usage:
            usage[k] += u.get(k) or 0
        content = j.get("content") or []
        uses = [b for b in content if b.get("type") == "tool_use"]
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        if finish:
            done = next((b for b in uses if b.get("name") == finish), None)
            if done:
                return text, done.get("input") or {}, usage, calls
        if not uses:
            if j.get("stop_reason") == "max_tokens":
                raise RuntimeError(f"The answer was cut off at {max_tokens} tokens.")
            return text, None, usage, calls
        msgs.append({"role": "assistant", "content": content})
        results = []
        for b in uses:
            calls += 1
            if calls > max_calls:
                out, ok = json.dumps({"available": False, "error": "The limit of reads for this question is reached; "
                                                                   "answer with what you have and say what is missing."}), False
            else:
                out, ok = run_tool(b["name"], b.get("input") or {}, ctx)
            results.append({"type": "tool_result", "tool_use_id": b["id"], "content": out, "is_error": not ok})
        msgs.append({"role": "user", "content": results})
    return "I reached the limit of steps for this question.", None, usage, calls


def investigation_specs():
    return allowed(None) + [SUBMIT, RULE_CHANGE]


def chat_specs(perms):
    return allowed(perms) + [RAISE]
