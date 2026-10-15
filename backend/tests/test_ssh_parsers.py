import pytest

from app.resources.connectors.base import ConnectorError
from app.resources.connectors.http import check_ssrf
from app.views.ssh_health import parsers as p

DF = "Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda1 100000 85000 15000 85% /\n"
FREE = "              total used free\nMem:           8000 6000 2000\nSwap: 0 0 0\n"
SS = "State Recv-Q Send-Q Local Address:Port Peer\nLISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 5 [::]:8080 [::]:*\n"


def test_cpu_thresholds():
    rows = p.parse("cpu_load", {}, "4.50 2.00 1.00 1/200 123", warn=2, crit=4)
    assert rows[0][:3] == ("cpu_load", "load1", 4.5) and rows[0][4] == "crit"
    assert p.parse("cpu_load", {}, "1.0 1 1 1/1 1", warn=2, crit=4)[0][4] == "ok"


def test_disk_and_mem():
    d = p.parse("disk", {"mount": "/"}, DF, warn=80, crit=90)[0]
    assert d[2] == 85.0 and d[4] == "warn"
    m = p.parse("mem", {}, FREE, warn=70, crit=90)[0]
    assert m[2] == 75.0 and m[4] == "warn"


def test_process_and_port():
    assert p.parse("process", {"name": "nginx"}, "3\n")[0][4] == "ok"
    assert p.parse("process", {"name": "nginx"}, "0\n")[0][4] == "crit"
    assert p.parse("port", {"port": 8080}, SS)[0][4] == "ok"
    assert p.parse("port", {"port": 9999}, SS)[0][4] == "crit"
    assert p.parse("port", {"port": 80}, SS)[0][4] == "crit"  # :8080 must not satisfy port 80


def test_uptime():
    assert p.parse("uptime", {}, "172800.5 1.0")[0][2] == 2.0


def test_commands_quote_and_validate_args():
    assert p.command("disk", {"mount": "/var/log"}) == "df -P /var/log"
    assert p.command("process", {"name": "my-app.1"}) == "pgrep -fc -- my-app.1"
    for check, args in (("disk", {"mount": "/; rm -rf /"}), ("disk", {"mount": "/../etc"}),
                        ("disk", {"mount": "relative"}), ("process", {"name": "a b"}),
                        ("process", {"name": "$(id)"}), ("port", {"port": "22"}), ("port", {"port": 70000}),
                        ("nope", {})):
        with pytest.raises(p.ArgError):
            p.command(check, args)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "169.254.169.254", "0.0.0.0", "::1", "224.0.0.1"])
def test_ssrf_blocks_loopback_metadata_multicast(host):
    with pytest.raises(ConnectorError):
        check_ssrf(host)


def test_ssrf_allows_private_and_allowlist():
    check_ssrf("10.1.2.3")  # RFC1918 internal APIs are allowed
    check_ssrf("127.0.0.1", ["127.0.0.0/8"])
    check_ssrf("127.0.0.1", ["127.0.0.1"])
