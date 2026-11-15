"""Unicorn's read-only tools in the Purchasing tower (Layer 2 → Layer 3 of the Unicorn 2.0 architecture).

Identity asks POST /internal/unicorn/tool {"name", "args"} with the service token. Each tool answers from what the
tower already builds for its screens (Buying, Supplier performance, Item history, Invoices without PO); a read
that is still running answers {"pending": true, "stage"} and the same call asked again gives the result.
Nothing is written to Business Central.
"""
import itemhist

MODES = ("week", "month", "quarter", "ytd")


def _co(args, ctx):
    return str(args.get("company") or ctx["default_company"]).strip().upper()


def _mode(args):
    m = str(args.get("period") or "month")
    if m not in MODES:
        raise ValueError("period: week, month, quarter or ytd.")
    return m


def _r(v):
    return round(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


def _slim(d, keys):
    return {k: _r(d.get(k)) for k in keys if d.get(k) not in (None, "", [])}


def t_purchases_overview(ctx, args):
    co, mode = _co(args, ctx), _mode(args)
    data = ctx["dashboard"](co, str(args.get("date") or ""), "", mode, 15)
    if data.get("pending"):
        return {"pending": True, "stage": data.get("step") or "reading Business Central"}
    if data.get("available") is False or data.get("error"):
        return {"available": False, "error": data.get("error") or "Not read."}
    k, meta = data.get("kpis") or {}, data.get("meta") or {}
    return {"company": co, "currency": ctx["currency_of"](co), "period": meta.get("period_label"),
            "dates": meta.get("current"), "comparison_dates": meta.get("prior"),
            "kpis": _slim(k, ("total_cur", "total_prev", "total_pct", "increases", "decreases", "increase_impact", "saving",
                              "net_price_impact", "items", "documents", "uninvoiced_in_period", "uninvoiced_receipts")),
            "price_movers": [_slim(m, ("item_no", "desc", "vendor", "prev_avg", "cur_avg", "pct", "impact", "qty", "locations"))
                             for m in (data.get("movers") or [])[:15]],
            "price_spreads": [_slim(m, ("item_no", "desc", "min", "max", "spread_pct", "impact")) for m in (data.get("spreads") or [])[:8]],
            "tower_brief": _slim(data.get("brief") or {}, ("status", "status_note", "developments", "risks", "actions")),
            "source": "Business Central posted purchase invoice lines and receipts (Purchasing → Buying)"}


def t_supplier_summary(ctx, args):
    co, mode = _co(args, ctx), _mode(args)
    r = ctx["supplier_summary"](co, str(args.get("date") or ""), "", mode)
    if r.get("pending"):
        return {"pending": True, "stage": r.get("step") or "reading Business Central"}
    if r.get("error"):
        return {"available": False, "error": r["error"]}
    rows = r.get("rows") or []
    return {"company": co, "currency": ctx["currency_of"](co), "kpis": r.get("kpis"),
            "suppliers": [_slim(x, ("no", "name", "net", "share", "prev", "change_pct", "invoices", "credit", "items_cost", "notes"))
                          for x in rows[:25]],
            "lost_suppliers": [_slim(x, ("no", "name", "prev")) for x in (r.get("lost") or [])[:10]],
            "definitions": r.get("definitions"),
            "source": "Business Central posted purchase invoices and credit memos (Purchasing → Supplier performance)"}


def t_item_prices(ctx, args):
    co, mode = _co(args, ctx), _mode(args)
    a, b, pa, pb, _lfl, label = ctx["period"](str(args.get("date") or ""), mode)
    prog = itemhist.ensure(ctx["client"], co, min(a, pa), max(b, pb))
    if prog:
        return {"pending": True, "stage": f"purchase invoice lines, month {prog.get('done', 0)} of {prog.get('total', 0)}"}
    f = {"q": str(args.get("search") or "")[:120], "item": str(args.get("item") or "")[:40], "vendor": "", "category": "", "stream": ""}
    r = itemhist.report(ctx["client"], co, a, b, pa, pb, "", None, f, ctx["currency_of"](co), itemhist.last_errors(co))
    out = {"company": co, "currency": r["currency"], "period": r["period"], "comparison": r["prior"], "period_label": label,
           "items_found": r["count"], "kpis": r["kpis"],
           "items": [_slim(x, ("item", "desc", "category", "uom", "cost", "qty", "avg", "min", "max", "last", "last_day",
                               "spread", "suppliers", "main_supplier", "prev_avg", "change_pct", "outliers")) for x in r["rows"][:25]],
           "errors": r.get("errors") or [], "definitions": r.get("definitions"),
           "source": "Business Central posted purchase invoice lines (Prices & contracts → Item history)"}
    if r.get("detail"):
        d = r["detail"]
        out["detail"] = {"item": d["item"], "by": d["by"], "series": d["series"][-26:],
                         "suppliers": [_slim(s, ("no", "name", "cost", "qty", "avg", "min", "max", "last", "prev_avg")) for s in d["suppliers"][:10]],
                         "locations": [_slim(s, ("code", "cost", "qty", "avg")) for s in d["locations"][:10]],
                         "latest_purchases": d["purchases"][:20]}
    return out


def t_invoices_without_po(ctx, args):
    from datetime import date
    co = _co(args, ctx)
    try:
        a = date.fromisoformat(str(args.get("date_from"))[:10])
        b = date.fromisoformat(str(args.get("date_to") or args.get("date_from"))[:10])
    except ValueError as exc:
        raise ValueError("date_from / date_to: dates YYYY-MM-DD.") from exc
    if (b - a).days > 92:
        raise ValueError("At most 92 days in one call.")
    n = ctx["nopo"](co, a, b)
    if not n.get("available"):
        return {"available": False, "error": n.get("error") or "Not read."}
    return {"company": co, "currency": ctx["currency_of"](co), "period": {"from": a.isoformat(), "to": b.isoformat()},
            "kpis": n.get("kpis"),
            "by_vendor": (n.get("by_vendor") or n.get("vendors") or [])[:15],
            "source": "Business Central posted purchase invoices against purchase orders (Invoices without PO)"}


TOOLS = {"purchases_overview": t_purchases_overview, "supplier_summary": t_supplier_summary,
         "item_prices": t_item_prices, "invoices_without_po": t_invoices_without_po}


def call(ctx, name, args):
    fn = TOOLS.get(name)
    if not fn:
        return {"available": False, "error": f"Purchasing has no tool {name}."}
    try:
        return fn(ctx, args if isinstance(args, dict) else {})
    except ValueError as exc:
        return {"available": False, "error": str(exc)}
    except Exception as exc:                                        # noqa: BLE001 (Business Central refused, timed out)
        return {"available": False, "error": f"Could not read: {str(exc)[:300]}"}
