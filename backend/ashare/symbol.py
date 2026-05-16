"""A-share stock code helpers."""

from __future__ import annotations

BOARD_LIMITS = {
    "main": 0.10,
    "chinext": 0.20,
    "star": 0.20,
    "beijing": 0.30,
}


def _strip_prefix(symbol: str) -> str:
    s = symbol.strip().lower().replace(".", "")
    for prefix in ("sh", "sz", "bj"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return "".join(c for c in s if c.isdigit()) or s


def get_exchange(code: str) -> str:
    c = _strip_prefix(code)
    if c.startswith(("6", "9")):
        return "sh"
    if c.startswith(("0", "2", "3")):
        return "sz"
    if c.startswith(("4", "8")):
        return "bj"
    return "sh"


def get_market(code: str) -> str:
    c = _strip_prefix(code)
    if c.startswith("688"):
        return "star"
    if c.startswith(("300", "301")):
        return "chinext"
    if c.startswith(("4", "8")):
        return "beijing"
    return "main"


def normalize(symbol: str) -> str:
    """Normalize input like 600519, sh600519, SH.600519 to sh600519."""
    c = _strip_prefix(symbol)
    if len(c) == 6 and c.isdigit():
        return f"{get_exchange(c)}{c}"
    return symbol.strip().lower()


def to_akshare(symbol: str) -> str:
    """AKShare usually expects a plain 6-digit stock code."""
    return _strip_prefix(symbol)


def to_display(symbol: str, name: str = "") -> str:
    norm = normalize(symbol)
    return f"{norm} {name}".strip()


def get_limit_pct(symbol: str, is_st: bool = False) -> float:
    if is_st:
        return 0.05
    return BOARD_LIMITS.get(get_market(symbol), 0.10)


def get_akshare_secid(symbol: str) -> str:
    code = to_akshare(symbol)
    exchange = get_exchange(code)
    prefix = "1" if exchange == "sh" else "0"
    return f"{prefix}.{code}"
