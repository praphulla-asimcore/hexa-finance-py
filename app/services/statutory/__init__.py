"""Country-dispatched statutory calculation.

Each submodule exposes ``enrich(entities, airtable_list=None) -> None``,
mutating each employee dict in place with statutory contribution figures and
a recomputed CTC — mirroring app.services.statutory_enrich's existing
Malaysian contract exactly, so callers don't need to know which country's
module is running.
"""

from app.services.statutory import my as _my
from app.services.statutory import id as _id
from app.services.statutory import np as _np

STATUTORY_MODULES = {
    "MY": _my,
    "ID": _id,
    "NP": _np,
}


def get_statutory_module(country: str):
    """Fail loud on an unrecognised/unconfigured country rather than
    silently applying Malaysian statutory math to a foreign payroll."""
    module = STATUTORY_MODULES.get((country or "MY").upper())
    if module is None:
        raise NotImplementedError(
            f"No statutory module configured for country {country!r}. "
            f"Supported: {sorted(STATUTORY_MODULES)}."
        )
    return module
