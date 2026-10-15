"""Server-side command templates and output parsers for the ssh_health catalog (spec 7.3 V1).

Each check yields rows (check, metric, value, unit, status). Parsers are pure functions of the command output.
"""
import re
import shlex

OK, WARN, CRIT = "ok", "warn", "crit"

_MOUNT = re.compile(r"^/[A-Za-z0-9_./-]{0,127}$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ArgError(ValueError):
    pass


def _grade(value: float, warn: float | None, crit: float | None) -> str:
    if crit is not None and value >= crit:
        return CRIT
    if warn is not None and value >= warn:
        return WARN
    return OK


def command(check: str, args: dict) -> str:
    """Build the shell command from a fixed template; arguments are validated, then shell-quoted."""
    if check == "cpu_load":
        return "cat /proc/loadavg"
    if check == "mem":
        return "free -m"
    if check == "uptime":
        return "cat /proc/uptime"
    if check == "disk":
        mount = args.get("mount", "/")
        if not _MOUNT.match(mount) or ".." in mount:
            raise ArgError("invalid mount")
        return f"df -P {shlex.quote(mount)}"
    if check == "process":
        name = args.get("name", "")
        if not _NAME.match(name):
            raise ArgError("invalid process name")
        return f"pgrep -fc -- {shlex.quote(name)}"
    if check == "port":
        if not isinstance(args.get("port"), int) or not 1 <= args["port"] <= 65535:
            raise ArgError("invalid port")
        return "ss -ltn"
    raise ArgError(f"unknown check: {check}")


CHECKS = ("cpu_load", "mem", "disk", "uptime", "process", "port")


def parse(check: str, args: dict, output: str, warn: float | None = None, crit: float | None = None) -> list[tuple]:
    out = output.strip()
    if check == "cpu_load":
        l1, l5, l15 = (float(x) for x in out.split()[:3])
        return [(check, "load1", l1, "", _grade(l1, warn, crit)), (check, "load5", l5, "", OK),
                (check, "load15", l15, "", OK)]
    if check == "mem":
        line = next(ln for ln in out.splitlines() if ln.lower().startswith("mem:")).split()
        total, used = float(line[1]), float(line[2])
        pct = round(used / total * 100, 1) if total else 0.0
        return [(check, "used_pct", pct, "%", _grade(pct, warn, crit)), (check, "total", total, "MB", OK),
                (check, "used", used, "MB", OK)]
    if check == "disk":
        line = out.splitlines()[-1].split()
        pct = float(line[4].rstrip("%"))
        return [(check, f"used_pct {args.get('mount', '/')}", pct, "%", _grade(pct, warn, crit)),
                (check, "size", float(line[1]) / 1024, "MB", OK)]
    if check == "uptime":
        days = round(float(out.split()[0]) / 86400, 2)
        return [(check, "uptime", days, "days", OK)]
    if check == "process":
        n = int(out.split()[0]) if out.split() else 0
        return [(check, f"count {args['name']}", n, "", OK if n >= 1 else CRIT)]
    if check == "port":
        port = args["port"]
        listening = any(re.search(rf"[:.]{port}\s", ln + " ") for ln in out.splitlines()[1:])
        return [(check, f"listening {port}", 1 if listening else 0, "", OK if listening else CRIT)]
    raise ArgError(f"unknown check: {check}")
