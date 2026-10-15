import datetime
import decimal

import oracledb

from app.resources.connectors.base import ConnectorError, Handle


def _connect(h: Handle):
    c = h.config
    # Thin mode, credentials passed as separate arguments (never concatenated into a string).
    conn = oracledb.connect(
        user=c["username"], password=h.secret.get("password", ""),
        dsn=f"{c['host']}:{c['port']}/{c['service_name']}",
        tcp_connect_timeout=float(c.get("connect_timeout_s", 10)),
    )
    conn.call_timeout = int(c.get("call_timeout_ms", 30000))
    return conn


def _type(v) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, (int, float, decimal.Decimal)):
        return "number"
    if isinstance(v, (datetime.datetime, datetime.date)):
        return "datetime"
    return "string"


def _plain(v):
    if isinstance(v, decimal.Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    if hasattr(v, "read"):  # LOB
        return v.read()
    return v


class OracleConnector:
    def ping(self, h: Handle) -> None:
        with _connect(h) as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM dual")
            cur.fetchone()

    def query(self, h: Handle, sql: str, binds: dict, max_rows: int):
        """Run a read-only query; returns (columns[{name,type}], rows[list])."""
        with _connect(h) as conn:
            cur = conn.cursor()
            cur.arraysize = int(h.config.get("fetch_size", 500))
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql, binds)
            names = [d[0] for d in cur.description]
            rows = [[_plain(v) for v in r] for r in cur.fetchmany(max_rows)]
            conn.rollback()
        cols = []
        for i, n in enumerate(names):
            sample = next((r[i] for r in rows if r[i] is not None), None)
            cols.append({"name": n, "type": _type(sample)})
        return cols, rows
