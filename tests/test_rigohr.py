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


def test_hnpl_employee_net_pay_rows_count_as_employees_branch_rows_as_summary():
    from app.services.rigohr import aggregate_rows
    # the shape of HNPL's live October 2026 pull (values synthetic)
    rows = [{"GroupedBy": "Branch", "GroupCode": "B1", "Name": "GuardWare", "AccountName": "Salary Expense",
             "Amount": 1000, "AccountCode": "", "Description": "", "EmployeeLedgerCode": None},
            {"GroupedBy": "Employee Net Pay", "GroupCode": "E-1", "Name": "Ram Example",
             "EmployeeLedgerCode": "123-1", "AccountName": "Salary Payable", "Amount": -800,
             "AccountCode": "", "Description": ""}]
    assert group_by_employee(rows)["E-1"]["accounts"] == {"Salary Payable": -800.0}
    assert aggregate_rows(rows) == [{"grouped_by": "Branch", "group": "GuardWare",
                                     "account": "Salary Expense", "amount": 1000.0}]
