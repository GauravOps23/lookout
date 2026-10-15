import logging
import re

_SENSITIVE = re.compile(
    r"(password|secret|token|key|authorization)(['\"]?\s*[:=]\s*['\"]?)([^\s,'\"}]+)", re.I
)


def redact(text: str) -> str:
    return _SENSITIVE.sub(r"\1\2***", text)


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def install() -> None:
    root = logging.getLogger()
    root.addFilter(RedactFilter())
    for h in root.handlers:
        h.addFilter(RedactFilter())
