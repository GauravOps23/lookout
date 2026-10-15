"""Safe substitution of validated variables into request templates (spec 7.3 V3).

Values never reach the request by raw string concatenation: each is encoded for the slot it lands in
(URL component, JSON leaf, XML text). Values are validated by bind_params before they get here.
"""
import re
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_WHOLE = re.compile(r"^\s*\{\{\s*(\w+)\s*\}\}\s*$")


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))


def _text(v: Any) -> str:
    return ("true" if v else "false") if isinstance(v, bool) else str(v)


def url_part(template: str, params: dict) -> str:
    """Path / query: each value is percent-encoded completely (so '/', '?', '&', '#' cannot escape)."""
    return PLACEHOLDER.sub(lambda m: quote(_text(params[m.group(1)]), safe=""), template)


def xml_text(template: str, params: dict) -> str:
    return PLACEHOLDER.sub(lambda m: escape(_text(params[m.group(1)]), {'"': "&quot;", "'": "&apos;"}), template)


def json_value(node: Any, params: dict) -> Any:
    """Walk a parsed JSON template: a string that is exactly '{{x}}' becomes the typed value;
    '{{x}}' inside longer text is replaced by its text. Keys are never templated."""
    if isinstance(node, str):
        m = _WHOLE.match(node)
        if m:
            return params[m.group(1)]
        return PLACEHOLDER.sub(lambda m: _text(params[m.group(1)]), node)
    if isinstance(node, list):
        return [json_value(x, params) for x in node]
    if isinstance(node, dict):
        return {k: json_value(v, params) for k, v in node.items()}
    return node


def header_value(template: str, params: dict) -> str:
    v = PLACEHOLDER.sub(lambda m: _text(params[m.group(1)]), template)
    if "\r" in v or "\n" in v:
        raise ValueError("header value contains a line break")
    return v
