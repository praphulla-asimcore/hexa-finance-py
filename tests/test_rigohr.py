"""RigoHR salary-voucher grouping — synthetic rows only, no API calls."""
from app.services.rigohr import describe_rows, group_by_employee


def test_groups_documented_pascal_case_rows():
    rows = [{"GroupedBy": "Company", "GroupCode": "C1", "AccountName": "Salary Payable", "Amount": -900},
            {"GroupedBy": "Employee", "GroupCode": "E-101", "Name": "Hari Example",
             "EmployeeLedgerCode": "123-109", "AccountName": "Salary Payable", "Amount": -500},
            {"GroupedBy": "Employee", "GroupCode": "E-101", "Name": "Hari Example",
             "EmployeeLedgerCode": "123-109", "AccountName": "SSF Payable", "Amount": -60}]
    assert group_by_employee(rows) == {"E-101": {
        "name": "Hari Example", "ledger_code": "123-109",
        "accounts": {"Salary Payable": -500.0, "SSF Payable": -60.0}}}


def test_groups_rows_whatever_the_key_and_value_casing():
    rows = [{"groupedBy": "employee", "groupCode": "E-7", "name": "Sita Example",
             "employeeLedgerCode": "1", "accountName": "Salary Payable", "amount": "-250"}]
    assert group_by_employee(rows)["E-7"]["accounts"] == {"Salary Payable": -250.0}


def test_describe_rows_has_no_names_or_amounts():
    rows = [{"GroupedBy": "Branch", "Name": "Hari Example", "Amount": 5},
            {"groupedBy": "Branch"}, {"GroupedBy": "Employee"}]
    assert describe_rows(rows) == {"fields": ["Amount", "GroupedBy", "Name", "groupedBy"],
                                   "grouped_by": {"Branch": 2, "Employee": 1}}
