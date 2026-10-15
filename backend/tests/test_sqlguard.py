import pytest

from app.errors import AppError
from app.views import sqlguard
from app.views.db_table.plugin import DAILY_ERRORS_SQL

OK = [
    "SELECT 1 FROM dual",
    "select a, b from t where x = :x and y > :y;",
    "WITH c AS (SELECT a FROM t) SELECT * FROM c",
    "SELECT a FROM t UNION ALL SELECT a FROM u",
    "SELECT 'DELETE FROM t; DROP TABLE x' AS s FROM dual",  # keywords inside a string are data
    "SELECT a FROM t -- INSERT INTO x\n WHERE 1=1",  # and inside comments
    DAILY_ERRORS_SQL,
]
BAD = [
    "",
    "DELETE FROM t",
    "UPDATE t SET a = 1",
    "INSERT INTO t VALUES (1)",
    "DROP TABLE t",
    "SELECT 1 FROM dual; SELECT 2 FROM dual",
    "SELECT 1 FROM dual; DROP TABLE t",
    "SELECT * INTO x FROM t",
    "SELECT * FROM t FOR UPDATE",
    "SELECT DBMS_RANDOM.VALUE FROM dual",
    "SELECT utl_http.request('http://x') FROM dual",
    "SELECT * FROM t@remote_link",
    "BEGIN NULL; END;",
    "GRANT SELECT ON t TO u",
    "SELECT (",  # unparsable
    "CREATE TABLE x AS SELECT 1 FROM dual",
    "MERGE INTO t USING u ON (1=1) WHEN MATCHED THEN UPDATE SET a = 1",
]


@pytest.mark.parametrize("sql", OK)
def test_accepts_read_only(sql):
    sqlguard.validate(sql)


@pytest.mark.parametrize("sql", BAD)
def test_rejects_everything_else(sql):
    with pytest.raises(AppError) as e:
        sqlguard.validate(sql)
    assert e.value.code == "VALIDATION"


def test_binds_reported_and_literals_ignored():
    assert sqlguard.validate("SELECT ':fake' AS a FROM t WHERE x = :real") == {"real"}


def test_macros():
    assert "TRUNC(SYSDATE)" in sqlguard.expand_macros("SELECT * FROM t WHERE d >= {{today}}", set())
    assert sqlguard.expand_macros("WHERE a = {{cust}}", {"cust"}) == "WHERE a = :cust"
    with pytest.raises(AppError):  # undeclared placeholder is never passed through
        sqlguard.expand_macros("WHERE a = {{cust}}", set())


def test_wrap_caps_rows():
    assert sqlguard.wrap("SELECT 1 FROM dual;") == "SELECT * FROM (SELECT 1 FROM dual) WHERE ROWNUM <= :max_rows"


def test_daily_errors_uses_range_predicate():
    assert "to_char" not in DAILY_ERRORS_SQL.lower() and "TRUNC(SYSDATE) + 1" in DAILY_ERRORS_SQL
