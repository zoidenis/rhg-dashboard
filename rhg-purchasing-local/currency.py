"""The currency label for the company being looked at.

Business Central stores each company's amounts in that company's own currency, so
nothing here converts anything: this only decides whether a figure is written as
ALL or EUR. The server sets it once per request, before the analysis runs.
"""

_current = ["ALL"]


def set_current(code):
    _current[0] = (code or "ALL").upper()


def code():
    return _current[0]


def money(value, decimals=0):
    return f"{value:,.{decimals}f} {_current[0]}"
