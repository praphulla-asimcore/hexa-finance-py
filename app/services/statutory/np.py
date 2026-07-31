"""Nepal statutory module — not yet implemented.

Before writing this, confirm with the client whether the entity is
SSF-registered (Social Security Fund: 11% employee + 20% employer, the
post-2019 mandatory scheme) or still on the older Provident Fund (10%+10%)
+ Gratuity (8.33% employer) scheme — this materially changes the
contribution math and must not be assumed. Also needs TDS (tax deducted at
source) per the current Nepal Income Tax Act slabs.
"""


def enrich(entities, airtable_list=None) -> None:
    raise NotImplementedError(
        "Nepal (HNPL) statutory calculation is not yet configured — the "
        "SSF-vs-PF/Gratuity scheme has not been confirmed and TDS slabs "
        "have not been entered."
    )
