"""Malaysia statutory module (EPF/SOCSO/EIS/HRDF) — thin wrapper around the
existing, unchanged app.services.statutory_enrich implementation, exposed
under the country-dispatch interface every statutory module implements:
``enrich(entities, airtable_list=None) -> None`` (mutates entities in place).
"""

from app.services.statutory_enrich import enrich_entities_statutory as enrich

__all__ = ["enrich"]
