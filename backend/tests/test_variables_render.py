import json

import pytest

from app.errors import AppError
from app.views.api_response import render
from app.views.base import Variable, bind_params, merge_results, result_set, variables_json_schema

V = lambda **k: Variable(**k)  # noqa: E731


def err(variables, supplied):
    with pytest.raises(AppError) as e:
        bind_params(variables, supplied)
    assert e.value.code == "VALIDATION"
    return e.value.details


def test_missing_unknown_and_defaults():
    vs = [V(name="a"), V(name="b", required=False), V(name="c", default="dflt")]
    assert err(vs, {})["missing"] == ["a"]
    assert err(vs, {"a": "1", "zzz": 1})["unknown"] == ["zzz"]
    assert bind_params(vs, {"a": "x"}) == {"a": "x", "c": "dflt"}


def test_type_and_constraint_checks():
    pat = V(name="msisdn", pattern=r"^[0-9]{10}$")
    assert bind_params([pat], {"msisdn": "9876543210"})
    for bad in ("98765", "9876543210; DROP", "9876543210\n", 5):
        err([pat], {"msisdn": bad})
    assert bind_params([V(name="n", type="number", min=0, max=10)], {"n": "7"}) == {"n": 7}
    err([V(name="n", type="number", min=0, max=10)], {"n": 11})
    err([V(name="n", type="number")], {"n": "NaN"})
    err([V(name="n", type="number")], {"n": True})
    err([V(name="e", type="enum", enum=["A", "B"])], {"e": "C"})
    err([V(name="d", type="date")], {"d": "2025-13-40"})
    err([V(name="s", max_length=3)], {"s": "toolong"})
    err([V(name="s")], {"s": "nul\x00byte"})
    err([V(name="b", type="boolean")], {"b": "maybe"})


def test_url_encoding_cannot_escape_slot():
    p = {"id": "../admin?x=1&y=2#frag /"}
    assert render.url_part("/users/{{id}}", p) == "/users/..%2Fadmin%3Fx%3D1%26y%3D2%23frag%20%2F"
    assert render.url_part("q={{id}}", p).count("&") == 0


def test_json_values_are_typed_leaves_not_concatenated():
    tpl = {"id": "{{n}}", "name": "{{s}}", "note": "hi {{s}}"}
    out = render.json_value(tpl, {"n": 5, "s": '", "admin": true, "x": "'})
    assert out["id"] == 5
    assert json.loads(json.dumps(out)).keys() == {"id", "name", "note"}  # injection stays inside the string


def test_xml_escaping():
    out = render.xml_text("<id>{{x}}</id>", {"x": "</id><evil a=\"1\">&"})
    assert "<evil" not in out and "&lt;/id&gt;" in out and "&amp;" in out


def test_header_line_breaks_rejected():
    with pytest.raises(ValueError):
        render.header_value("{{x}}", {"x": "a\r\nX-Evil: 1"})


def test_merge_adds_source_column_and_caps():
    cols = [{"name": "A", "type": "number"}]
    a = result_set(cols, [[1], [2]])
    b = result_set(cols, [[3]])
    m = merge_results([("S1", a), ("S2", b)])
    assert [c["name"] for c in m["columns"]] == ["SOURCE", "A"]
    assert m["rows"] == [["S1", 1], ["S1", 2], ["S2", 3]]
    assert merge_results([("S1", a), ("S2", b)], row_limit=2)["meta"]["truncated"] is True


def test_llm_schema_hides_sensitive_variables():
    vs = [V(name="id", pattern="^[0-9]+$", description="id"), V(name="pin", sensitive=True),
          V(name="note", llm_fillable=False), V(name="opt", required=False)]
    s = variables_json_schema(vs)
    assert set(s["properties"]) == {"id", "opt"} and s["required"] == ["id"]
    assert s["additionalProperties"] is False
