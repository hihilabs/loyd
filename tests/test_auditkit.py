"""Audit kit: scope gate (the whole point), parsers, report, real nmap on localhost."""
import datetime as dt
import json
import shutil
import subprocess

import pytest

import auditkit as ak

TODAY = dt.datetime.now(dt.timezone.utc).date()


def _scope(tmp_path, monkeypatch, sid="acme", **kw):
    monkeypatch.setenv("LOYD_AUDITS_DIR", str(tmp_path))
    base = {"client": "Acme", "authorized_by": "Jane, IT Dir", "authorization_ref": "SOW 1",
            "cidrs": ["127.0.0.1/32", "10.9.0.0/24"], "domains": ["acme.example"],
            "starts": str(TODAY - dt.timedelta(days=1)), "expires": str(TODAY + dt.timedelta(days=1))}
    base.update(kw)
    (tmp_path / f"{sid}.json").write_text(json.dumps(base))
    return sid


def _tools(exec_fn=None):
    schemas, impl, writes = [], {}, set()
    calls = []

    def fake(cmd, timeout):
        calls.append(cmd)
        return (0, "/usr/bin/x\n", "") if cmd.startswith("command -v") else (0, "", "")
    ak.install(schemas, impl, writes, exec_fn or fake, lambda s: s)
    return impl, writes, calls


@pytest.mark.parametrize("args, why", [
    ({"scope": "acme", "cidr": "10.9.1.0/24"}, "outside scope"),
    ({"scope": "acme", "cidr": "10.0.0.0/8"}, "outside scope"),
    ({"scope": "acme", "cidr": "8.8.8.8"}, "outside scope"),
    ({"scope": "nope", "cidr": "10.9.0.0/24"}, "no scope file"),
    ({"scope": "../etc/x", "cidr": "10.9.0.0/24"}, "scope id"),
    ({"scope": "acme", "cidr": "10.9.0.0/24; rm -rf /"}, "not an IP"),
])
def test_out_of_scope_refused_before_any_probe(tmp_path, monkeypatch, args, why):
    _scope(tmp_path, monkeypatch)
    impl, _, calls = _tools()
    out = impl["audit_discover"](args, False)
    assert out.startswith("REFUSED") and why in out
    assert not [c for c in calls if c.startswith("nmap")]


def test_expired_and_future_scopes_refused(tmp_path, monkeypatch):
    impl, _, calls = _tools()
    _scope(tmp_path, monkeypatch, sid="old", expires=str(TODAY - dt.timedelta(days=1)),
           starts=str(TODAY - dt.timedelta(days=5)))
    assert "expired" in impl["audit_discover"]({"scope": "old", "cidr": "10.9.0.0/24"}, False)
    _scope(tmp_path, monkeypatch, sid="soon", starts=str(TODAY + dt.timedelta(days=2)),
           expires=str(TODAY + dt.timedelta(days=4)))
    assert "not yet authorized" in impl["audit_services"]({"scope": "soon", "target": "10.9.0.5"}, False)
    assert not [c for c in calls if c.startswith("nmap")]


def test_scope_requires_authorization_fields(tmp_path, monkeypatch):
    _scope(tmp_path, monkeypatch, authorized_by="")
    impl, _, _ = _tools()
    assert "authorized_by" in impl["audit_scope"]({"scope": "acme"}, False)


def test_hostname_must_resolve_inside_scope(tmp_path, monkeypatch):
    sid = _scope(tmp_path, monkeypatch)
    sc = ak.load_scope(sid)
    inside = lambda h, p: [(0, 0, 0, "", ("10.9.0.7", 0))]
    outside = lambda h, p: [(0, 0, 0, "", ("10.9.0.7", 0)), (0, 0, 0, "", ("1.2.3.4", 0))]
    assert ak.check_network_target("nas.acme.example", sc, resolve=inside) == "nas.acme.example"
    with pytest.raises(ak.ScopeError, match="1.2.3.4"):
        ak.check_network_target("mixed.example", sc, resolve=outside)


def test_mail_dns_only_for_scope_domains(tmp_path, monkeypatch):
    _scope(tmp_path, monkeypatch)
    impl, _, calls = _tools()
    assert impl["audit_mail_dns"]({"scope": "acme", "domain": "google.com"}, False).startswith("REFUSED")
    assert not [c for c in calls if c.startswith("dig")]
    impl["audit_mail_dns"]({"scope": "acme", "domain": "mail.acme.example"}, False)
    assert any("dig" in c and "_dmarc.mail.acme.example" in c for c in calls)


def test_discover_size_cap(tmp_path, monkeypatch):
    _scope(tmp_path, monkeypatch, cidrs=["10.0.0.0/16"])
    impl, _, _ = _tools()
    assert "bigger than /22" in impl["audit_discover"]({"scope": "acme", "cidr": "10.0.0.0/20"}, False)


def test_scans_are_write_class():
    _, writes, _ = _tools()
    assert writes == {"audit_discover", "audit_services"}


def test_intensity_shapes_the_scan(tmp_path, monkeypatch):
    _scope(tmp_path, monkeypatch)
    impl, _, calls = _tools()
    impl["audit_services"]({"scope": "acme", "target": "10.9.0.5"}, False)
    nm = next(c for c in calls if c.startswith("nmap"))
    assert "--max-rate 50" in nm and "--top-ports 100" in nm and "-T2" in nm


SAMPLE_OG = """# Nmap 7.94 scan
Host: 10.9.0.5 ()\tStatus: Up
Host: 10.9.0.5 ()\tPorts: 22/open/tcp//ssh//OpenSSH 9.6/, 23/open/tcp//telnet///, 2375/open/tcp//docker///, 80/closed/tcp//http///\tIgnored State: closed (96)
Host: 10.9.0.9 ()\tStatus: Up
"""


def test_parsers_and_findings():
    assert ak.parse_discover(SAMPLE_OG) == ["10.9.0.5", "10.9.0.9"]
    svcs = ak.parse_services(SAMPLE_OG)
    assert [s[1] for s in svcs] == [22, 23, 2375]
    sev = {f["title"]: f["severity"] for f in ak.findings_for_services(svcs)}
    assert sev == {"Telnet open": "high", "Docker API without TLS": "critical"}


def test_tls_and_mail_findings():
    soon = (TODAY + dt.timedelta(days=5)).strftime("%b %d %H:%M:%S %Y GMT")
    f = ak.findings_for_tls("h", 443, f"notAfter={soon}", ["TLS 1.0"])
    assert [x["severity"] for x in f] == ["high", "medium"]
    m = ak.findings_for_mail("acme.example", ["v=spf1 +all"], ["v=DMARC1; p=none"])
    assert {x["title"] for x in m} == {"SPF allows any sender (+all)", "DMARC policy is p=none"}
    assert ak.findings_for_mail("a.b", ["v=spf1 include:x -all"], ["v=DMARC1; p=reject"]) == []


def test_report(tmp_path, monkeypatch):
    sid = _scope(tmp_path, monkeypatch)
    sc = ak.load_scope(sid)
    ak.record(sc, ak.findings_for_services(ak.parse_services(SAMPLE_OG)))
    ak.record(sc, ak.findings_for_services(ak.parse_services(SAMPLE_OG)))     # dedup
    impl, _, _ = _tools()
    out = impl["audit_report"]({"scope": sid}, False)
    assert "**2 findings:** 1 critical, 1 high" in out and "Jane, IT Dir" in out
    assert (tmp_path / "acme.report.md").exists()


@pytest.mark.skipif(not shutil.which("nmap"), reason="nmap not installed")
def test_real_nmap_on_localhost(tmp_path, monkeypatch):
    srv = subprocess.Popen(["python3", "-m", "http.server", "18765", "--bind", "127.0.0.1"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        _scope(tmp_path, monkeypatch, cidrs=["127.0.0.1/32"], intensity="normal")
        real = lambda cmd, t: (lambda p: (p.returncode, p.stdout, p.stderr))(
            subprocess.run(["bash", "-c", cmd.replace("--top-ports 1000", "-p 18765,23")],
                           capture_output=True, text=True, timeout=t))
        impl, _, _ = _tools(real)
        assert "127.0.0.1" in impl["audit_discover"]({"scope": "acme", "cidr": "127.0.0.1/32"}, False)
        out = impl["audit_services"]({"scope": "acme", "target": "127.0.0.1"}, False)
        assert "127.0.0.1:18765/tcp" in out
        assert impl["audit_services"]({"scope": "acme", "target": "127.0.0.2"}, False).startswith("REFUSED")
    finally:
        srv.terminate()
