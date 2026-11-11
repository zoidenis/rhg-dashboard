"""Order channels in Simphony: which channel each location's checks carry, and where none is set.

R&A's Order Channel Sales Mix shows two channels, "Default" and "Undefined". In Simphony 19.6 and later one
order channel, Default, is provisioned and given to every new check unless the revenue centre, the
workstation, the order type or the operator sets another; a check with no channel at all (number 0 or
missing) is reported as Undefined. This reads, through the BI API (read only):

  * getOrderChannelDimensions: the channels each location has;
  * getOrderTypeDimensions / getRevenueCenterDimensions: names;
  * getOrderChannelDailyTotals on sample days (two a month, and the last closed day): net sales by revenue
    centre, order type and channel number, so the share without a channel is seen by location, revenue
    centre, order type and month, and the day the Default channel first appears.

Nothing is written to Simphony.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import simphony

NONE = "none"


def sample_days(start, end, per_month=(5, 20)):
    out, d = [], start.replace(day=1)
    while d <= end:
        for k in per_month:
            x = d.replace(day=k)
            if start <= x <= end:
                out.append(x)
        d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _day_totals(ref, day):
    j = simphony.call("getOrderChannelDailyTotals", {"locRef": ref, "busDt": day.isoformat()}) or {}
    rows = []
    for rvc in j.get("revenueCenters") or []:
        for oc in rvc.get("orderChannels") or []:
            rows.append({"rvc": rvc.get("rvcNum"), "oc": oc.get("ocNum") or NONE, "ot": oc.get("otNum"),
                         "net": float(oc.get("netSlsTtl") or 0), "checks": int(oc.get("chkCnt") or 0)})
    return rows


def build(start, end, refs=None, workers=8):
    locs = [l for l in simphony.locations() if refs is None or l["locRef"] in refs]
    days = sample_days(start, end)
    jobs = {}
    for l in locs:
        ref = l["locRef"]
        jobs[("dims", ref)] = lambda r=ref: {
            "channels": (simphony.call("getOrderChannelDimensions", {"locRef": r}) or {}).get("orderChannels") or [],
            "ots": simphony.order_type_dims(r), "rvcs": simphony.rvc_dims(r)}
        try:
            last = simphony.latest_business_date(ref)
            extra = [date.fromisoformat(last[:10])] if last else []
        except Exception:                                        # noqa: BLE001
            extra = []
        for d in sorted(set(days + extra)):
            jobs[("day", ref, d)] = lambda r=ref, x=d: _day_totals(r, x)
    res = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        fut = {k: pool.submit(fn) for k, fn in jobs.items()}
        for k, f in fut.items():
            try:
                res[k] = (f.result(), None)
            except Exception as exc:                             # noqa: BLE001
                res[k] = (None, str(exc)[:200])

    out = []
    for l in locs:
        ref = l["locRef"]
        dims, err = res.get(("dims", ref), (None, "not read"))
        dims = dims or {"channels": [], "ots": {}, "rvcs": {}}
        chan = {int(c["num"]): (c.get("name") or c.get("mstrName") or f"#{c['num']}") for c in dims["channels"]
                if c.get("num") is not None}
        name_oc = lambda oc: "Undefined (no channel)" if oc == NONE else chan.get(int(oc), f"#{oc}")
        by_month, by_rvc, by_ot, days_read, errors = {}, {}, {}, [], []
        first_set = last_none = None
        for (kind, *rest), (rows, e) in res.items():
            if kind != "day" or rest[0] != ref:
                continue
            d = rest[1]
            if e:
                errors.append(f"{d}: {e}")
                continue
            days_read.append(d.isoformat())
            for r in rows:
                if not r["net"] and not r["checks"]:
                    continue
                oc = name_oc(r["oc"])
                m = by_month.setdefault(d.isoformat()[:7], {})
                m[oc] = m.get(oc, 0) + r["net"]
                k = f"{r['rvc']} {dims['rvcs'].get(r['rvc'], '')}".strip()
                by_rvc.setdefault(k, {})[oc] = by_rvc.setdefault(k, {}).get(oc, 0) + r["net"]
                k = f"{r['ot']} {dims['ots'].get(r['ot'], '')}".strip()
                by_ot.setdefault(k, {})[oc] = by_ot.setdefault(k, {}).get(oc, 0) + r["net"]
                if r["oc"] == NONE:
                    last_none = max(last_none or d, d)
                else:
                    first_set = min(first_set or d, d)
        tot = {}
        for m in by_month.values():
            for k, v in m.items():
                tot[k] = tot.get(k, 0) + v
        out.append({"locRef": ref, "name": l["name"], "currency": l["currency"], "dims_error": err,
                    "channels": [{"num": k, "name": v} for k, v in sorted(chan.items())],
                    "totals": {k: round(v, 2) for k, v in tot.items()},
                    "undefined_pct": round(tot.get("Undefined (no channel)", 0) / sum(tot.values()) * 100, 1)
                    if sum(tot.values()) else None,
                    "first_day_with_channel": first_set.isoformat() if first_set else None,
                    "last_day_without_channel": last_none.isoformat() if last_none else None,
                    "by_month": {k: {a: round(b, 2) for a, b in v.items()} for k, v in sorted(by_month.items())},
                    "by_rvc": {k: {a: round(b, 2) for a, b in v.items()} for k, v in by_rvc.items()},
                    "by_order_type": {k: {a: round(b, 2) for a, b in v.items()} for k, v in by_ot.items()},
                    "days_read": sorted(days_read), "errors": errors[:5]})
    out.sort(key=lambda x: -(x["undefined_pct"] or 0))
    return {"from": start.isoformat(), "to": end.isoformat(), "sample_days": [d.isoformat() for d in days],
            "locations": out,
            "note": "Net sales on sample days (the 5th and 20th of each month, and each location's last business "
                    "day), by the order channel on the checks. Undefined = no channel on the check."}
