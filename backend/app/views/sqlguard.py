"""SELECT-only SQL allow-list for db_table views (spec 7.3 V2). Oracle dialect, via sqlglot."""
import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.errors import AppError

MACROS = {"today": "TRUNC(SYSDATE)"}  # fixed server-side expressions, never user input
_MACRO = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_LITERALS = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)
_BIND = re.compile(r":([A-Za-z_]\w*)")
_FORBIDDEN_TEXT = re.compile(r"\b(DBMS_\w*|UTL_\w*|FOR\s+UPDATE|INTO|DBLINK|XMLTYPE)\b|@", re.I)
_FORBIDDEN_NODES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop, exp.Alter,
                    exp.Command, exp.Into, exp.Lock, exp.Transaction, exp.Commit, exp.Rollback, exp.Use,
                    exp.Set, exp.Pragma)


def _reject(msg: str):
    raise AppError("VALIDATION", f"SQL rejected: {msg}")


def expand_macros(sql: str, declared: set[str]) -> str:
    """{{today}} becomes its fixed expression; {{name}} of a declared variable becomes the bind :name."""
    def sub(m):
        name = m.group(1)
        if name in MACROS:
            return MACROS[name]
        if name in declared:
            return f":{name}"
        _reject(f"unknown placeholder {{{{{name}}}}}")
    return _MACRO.sub(sub, sql)


def validate(sql: str) -> set[str]:
    """Raise VALIDATION unless `sql` is exactly one read-only SELECT. Returns the bind names it uses."""
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        _reject("empty")
    stripped = _LITERALS.sub(" ", sql)  # keywords inside strings/comments are not code
    if ";" in stripped:
        _reject("only one statement is allowed")
    if (m := _FORBIDDEN_TEXT.search(stripped)):
        _reject(f"'{m.group(0)}' is not allowed")
    try:
        trees = sqlglot.parse(sql, read="oracle")
    except SqlglotError:
        _reject("could not be parsed")
    if len(trees) != 1 or trees[0] is None:
        _reject("only one statement is allowed")
    root = trees[0]
    if not isinstance(root, (exp.Select, exp.Union)):
        _reject("only SELECT is allowed")
    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            _reject(f"{type(node).__name__} is not allowed")
    return set(_BIND.findall(stripped))


def wrap(sql: str) -> str:
    """Cap rows inside the database."""
    return f"SELECT * FROM ({sql.strip().rstrip(';')}) WHERE ROWNUM <= :max_rows"
