CREATE TABLE IF NOT EXISTS consultant_sighting (
    id              uuid PRIMARY KEY
                    DEFAULT gen_random_uuid(),
    case_id         uuid NOT NULL
                    REFERENCES payroll_cases(id),
    employee_id     varchar(50) NOT NULL,
    consultant_name varchar(200) NOT NULL,
    entity          varchar(20) NOT NULL,
    status          varchar(10) NOT NULL
                    CHECK (status IN ('sighted','missing')),
    sighted_by      text,
    sighted_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (case_id, employee_id)
);

CREATE INDEX IF NOT EXISTS idx_cs_case_id
    ON consultant_sighting(case_id);
