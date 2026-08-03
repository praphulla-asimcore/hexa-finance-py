"""Nepal statutory module — pass-through, not a real calculation engine yet.

Nepal's CSI parser (app.services.parser._parse_employee_np) extracts SSF
Contribution/Deduction, CIT, SST and TDS directly from the uploaded file's
own already-computed columns, rather than deriving them from raw pay data
the way Malaysia's parser does. So unlike statutory/my.py (which recomputes
EPF/SOCSO/EIS/HRDF from rate tables), there is nothing to *enrich* here yet
-- this deliberately does not touch the employee dicts, since building a
real recompute engine first requires confirming with the client whether the
entity is SSF-registered (Social Security Fund: 11% employee + 20%
employer, the post-2019 mandatory scheme) or still on the older Provident
Fund (10%+10%) + Gratuity (8.33% employer) scheme, plus TDS slabs per the
current Nepal Income Tax Act. (2026-08-03: a real Nepal_CSI_Sample.xlsx
shows SSF Contribution consistently ~20% of Basic Salary across 3 clients,
suggestive of the SSF scheme -- but that's an inference from one sample
file, not a client confirmation, so it isn't assumed here.)
"""


def enrich(entities, airtable_list=None) -> None:
    return None
