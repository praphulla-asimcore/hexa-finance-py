"""Minimal Supabase-client compatibility layer over psycopg (DigitalOcean PG).

Reproduces the slice of the supabase-py query builder the app uses
(``db.from_(table).select()/insert()/update()/delete().eq().order().limit()
.single()/.maybe_single().execute()``) so the existing call sites work
unchanged after migrating off Supabase/PostgREST to a plain Postgres database.

Return shape matches PostgREST: timestamps/dates/uuids come back as strings and
numerics as floats, ``execute().data`` is a list (or a dict/None for
single/maybe_single), and ``execute().count`` is set when ``count="exact"``.
"""
from datetime import datetime, date
from decimal import Decimal
from uuid import UUID

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

# Many-to-one relationships used by PostgREST embeds: (base, embedded) ->
# (fk column on base, referenced column on embedded).
_EMBED_FK = {
    ("payroll_approval_tokens", "payroll_cases"): ("case_id", "id"),
}


def _norm(value):
    """Coerce DB types to the JSON-ish shapes PostgREST/supabase returned."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def _norm_row(row: dict) -> dict:
    return {k: _norm(v) for k, v in row.items()}


def _adapt(value):
    """Wrap dict/list so psycopg writes them to jsonb columns."""
    if isinstance(value, (dict, list)):
        return Jsonb(value)
    return value


class _Result:
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _Query:
    def __init__(self, conninfo: str, table: str):
        self._conninfo = conninfo
        self._table = table
        self._op = "select"
        self._columns = "*"
        self._embeds: list[str] = []
        self._payload = None
        self._filters: list[tuple] = []
        self._order: list[tuple] = []
        self._limit = None
        self._single = None          # "one" | "maybe" | None
        self._count = None

    # ── builder ──────────────────────────────────────────────────────────────
    def select(self, columns: str = "*", count: str | None = None):
        # ``.insert(...).select()`` / ``.update(...).select()`` in supabase just
        # requests the affected rows back — which RETURNING * already provides.
        # Only treat select() as a query when no write op is pending.
        if self._op != "select":
            return self
        self._count = count
        base, embeds = [], []
        for tok in _split_top_level(columns):
            tok = tok.strip()
            if not tok:
                continue
            if tok.endswith(")") and "(" in tok:
                embeds.append(tok[: tok.index("(")].strip())
            else:
                base.append(tok)
        self._columns = "*" if (not base or "*" in base) else base
        self._embeds = embeds
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, column, value):
        self._filters.append((column, value))
        return self

    def order(self, column, desc: bool = False):
        self._order.append((column, desc))
        return self

    def limit(self, n: int):
        self._limit = n
        return self

    def single(self):
        self._single = "one"
        return self

    def maybe_single(self):
        self._single = "maybe"
        return self

    # ── execution ────────────────────────────────────────────────────────────
    def execute(self) -> _Result:
        with psycopg.connect(self._conninfo, autocommit=True, row_factory=dict_row) as conn:
            conn.prepare_threshold = None  # PgBouncer transaction-pool safe
            if self._op == "select":
                return self._do_select(conn)
            if self._op == "insert":
                return self._do_insert(conn)
            if self._op == "update":
                return self._do_update(conn)
            if self._op == "delete":
                return self._do_delete(conn)
            raise ValueError(f"unknown op {self._op}")

    def _where(self):
        if not self._filters:
            return sql.SQL(""), []
        parts = [sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder()) for c, _ in self._filters]
        return sql.SQL(" WHERE ") + sql.SQL(" AND ").join(parts), [v for _, v in self._filters]

    def _cols_sql(self):
        if self._columns == "*":
            return sql.SQL("*")
        return sql.SQL(", ").join(sql.Identifier(c) for c in self._columns)

    def _do_select(self, conn) -> _Result:
        where, params = self._where()
        q = sql.SQL("SELECT {} FROM {}").format(self._cols_sql(), sql.Identifier(self._table)) + where
        if self._order:
            q += sql.SQL(" ORDER BY ") + sql.SQL(", ").join(
                sql.SQL("{} {}").format(sql.Identifier(c), sql.SQL("DESC" if d else "ASC"))
                for c, d in self._order
            )
        if self._limit is not None:
            q += sql.SQL(" LIMIT {}").format(sql.Literal(self._limit))
        rows = [_norm_row(r) for r in conn.execute(q, params).fetchall()]

        count = None
        if self._count == "exact":
            cq = sql.SQL("SELECT count(*) AS c FROM {}").format(sql.Identifier(self._table)) + where
            count = conn.execute(cq, params).fetchone()["c"]

        for embed in self._embeds:
            self._attach_embed(conn, rows, embed)

        if self._single:
            return _Result(rows[0] if rows else None, count)
        return _Result(rows, count)

    def _attach_embed(self, conn, rows, embed):
        fk = _EMBED_FK.get((self._table, embed))
        if not fk:
            for r in rows:
                r[embed] = None
            return
        fk_col, ref_col = fk
        ids = [r.get(fk_col) for r in rows if r.get(fk_col) is not None]
        related = {}
        if ids:
            q = sql.SQL("SELECT * FROM {} WHERE {} = ANY(%s)").format(
                sql.Identifier(embed), sql.Identifier(ref_col))
            for r in conn.execute(q, [ids]).fetchall():
                related[str(r[ref_col])] = _norm_row(r)
        for r in rows:
            r[embed] = related.get(str(r.get(fk_col)))

    def _rows_payload(self):
        return self._payload if isinstance(self._payload, list) else [self._payload]

    def _do_insert(self, conn) -> _Result:
        out = []
        for row in self._rows_payload():
            cols = list(row.keys())
            q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
                sql.Identifier(self._table),
                sql.SQL(", ").join(map(sql.Identifier, cols)),
                sql.SQL(", ").join(sql.Placeholder() * len(cols)),
            )
            res = conn.execute(q, [_adapt(row[c]) for c in cols]).fetchall()
            out.extend(_norm_row(r) for r in res)
        return _Result(out)

    def _do_update(self, conn) -> _Result:
        cols = list(self._payload.keys())
        sets = sql.SQL(", ").join(
            sql.SQL("{} = {}").format(sql.Identifier(c), sql.Placeholder()) for c in cols)
        where, wparams = self._where()
        q = sql.SQL("UPDATE {} SET ").format(sql.Identifier(self._table)) + sets + where + sql.SQL(" RETURNING *")
        params = [_adapt(self._payload[c]) for c in cols] + wparams
        rows = [_norm_row(r) for r in conn.execute(q, params).fetchall()]
        return _Result(rows)

    def _do_delete(self, conn) -> _Result:
        where, params = self._where()
        q = sql.SQL("DELETE FROM {}").format(sql.Identifier(self._table)) + where + sql.SQL(" RETURNING *")
        rows = [_norm_row(r) for r in conn.execute(q, params).fetchall()]
        return _Result(rows)


def _split_top_level(s: str) -> list[str]:
    """Split a PostgREST select string on top-level commas (ignoring parens)."""
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


class PgClient:
    """Drop-in stand-in for the supabase client, backed by psycopg."""

    def __init__(self, conninfo: str):
        self._conninfo = conninfo

    def from_(self, table: str) -> _Query:
        return _Query(self._conninfo, table)

    # supabase-py also exposes .table() as an alias
    def table(self, table: str) -> _Query:
        return _Query(self._conninfo, table)
