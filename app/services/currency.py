"""Single source of truth for currency display formatting, replacing the
three previously-duplicated ``_fmt_rm`` implementations (payroll_cases.py,
email.py, pdf.py). Display prefix only -- not a real i18n/locale formatter
(no thousands/decimal separator swap for IDR/NPR conventions); revisit once
real Indonesia/Nepal payroll data exists to design against.
"""

CURRENCY_PREFIX: dict = {
    "MYR": "RM",
    "IDR": "Rp",
    "NPR": "NPR",
}


def currency_prefix(currency: str) -> str:
    return CURRENCY_PREFIX.get((currency or "MYR").upper(), (currency or "MYR").upper())


def fmt_currency(amount, currency: str = "MYR") -> str:
    """Byte-identical to the original MYR-only ``_fmt_rm`` when currency is
    MYR/unset -- ``fmt_currency(n)`` and the old ``_fmt_rm(n)`` produce the
    same string for every input."""
    if amount is None:
        return "—"
    return f"{currency_prefix(currency)} {float(amount):,.2f}"
