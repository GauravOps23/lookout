"""One SSH round-trip that gathers everything the Servers page shows, parsed with the ssh_health parsers.

The command is a fixed template with no user input. It takes about a second (the iowait sample).
"""
from app.views.ssh_health import parsers

COMMAND = (
    "echo '##load'; cat /proc/loadavg; echo '##cores'; nproc; echo '##mem'; free -m; echo '##uptime'; cat /proc/uptime; "
    "echo '##disk'; (df -P -x tmpfs -x devtmpfs -x squashfs -x overlay 2>/dev/null || df -P); "
    "echo '##cpu'; head -1 /proc/stat; sleep 1; head -1 /proc/stat"
)

# (warn, crit) thresholds used to colour the page
LOAD_PER_CORE, MEM, SWAP, IOWAIT, DISK = (0.7, 1.0), (85, 95), (25, 60), (10, 30), (80, 90)
_RANK = {parsers.OK: 0, parsers.WARN: 1, parsers.CRIT: 2}


def _sections(out: str) -> dict[str, str]:
    cur, buf = None, {}
    for line in out.splitlines():
        if line.startswith("##"):
            cur = line[2:].strip()
            buf[cur] = []
        elif cur:
            buf[cur].append(line)
    return {k: "\n".join(v) for k, v in buf.items()}


def _try(fn):
    try:
        return fn()
    except (ValueError, StopIteration, IndexError, KeyError):
        return None  # one unreadable metric must not hide the others


def _metrics(check: str, text: str, warn=None, crit=None) -> dict | None:
    rows = _try(lambda: parsers.parse(check, {}, text, warn, crit))
    return None if rows is None else {m: (v, s) for _, m, v, _, s in rows}


def _disks(text: str) -> list[dict]:
    out = []
    for line in text.splitlines()[1:]:
        p = line.split()
        if len(p) < 6 or not p[4].endswith("%"):
            continue
        pct = _try(lambda: float(p[4].rstrip("%")))
        if pct is not None:
            out.append({"mount": " ".join(p[5:]), "used_pct": pct, "size_mb": round(int(p[1]) / 1024),
                        "status": parsers._grade(pct, *DISK)})
    return sorted(out, key=lambda d: -d["used_pct"])


def parse(output: str) -> dict:
    sec = _sections(output)
    cores = max(1, _try(lambda: int(sec["cores"].split()[0])) or 1)
    load = _metrics("cpu_load", sec.get("load", ""))
    mem = _metrics("mem", sec.get("mem", ""), *MEM)
    swap = _metrics("swap", sec.get("mem", ""), *SWAP)
    io = _metrics("iowait", sec.get("cpu", ""), *IOWAIT)
    up = _metrics("uptime", sec.get("uptime", ""))
    cpu = None
    if load:
        per_core = round(load["load1"][0] / cores, 2)
        cpu = {"load1": load["load1"][0], "load5": load["load5"][0], "load15": load["load15"][0], "cores": cores,
               "per_core": per_core, "status": parsers._grade(per_core, *LOAD_PER_CORE)}
    out = {
        "cpu": cpu,
        "mem": mem and {"used_pct": mem["used_pct"][0], "total_mb": mem["total"][0], "used_mb": mem["used"][0],
                        "status": mem["used_pct"][1]},
        "swap": swap and {"used_pct": swap["used_pct"][0], "total_mb": swap["total"][0], "used_mb": swap["used"][0],
                          "status": swap["used_pct"][1]},
        "iowait": io and {"pct": io["iowait_pct"][0], "status": io["iowait_pct"][1]},
        "uptime_days": up and up["uptime"][0],
        "disks": _disks(sec.get("disk", "")),
    }
    states = [m["status"] for m in (out["cpu"], out["mem"], out["swap"], out["iowait"]) if m]
    states += [d["status"] for d in out["disks"]]
    out["health"] = max(states, key=_RANK.get, default=parsers.OK)
    return out
