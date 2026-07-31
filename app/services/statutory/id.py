"""Indonesia statutory module — not yet implemented.

Will need BPJS Kesehatan (health, employer/employee split, wage-ceiling
capped), BPJS Ketenagakerjaan (JKK/JKM/JHT/JP), and PPh 21 income-tax
withholding (progressive brackets, PTKP non-taxable threshold), against
current BPJS/DJP regulations — none of which are configured yet.
"""


def enrich(entities, airtable_list=None) -> None:
    raise NotImplementedError(
        "Indonesia (PTHIT) statutory calculation is not yet configured — "
        "BPJS/PPh21 rates and rules have not been entered."
    )
