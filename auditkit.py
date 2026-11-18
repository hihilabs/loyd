"""
Loyd audit kit — AUTHORIZED network audits only, gated by a scope file.

Nothing here runs without a scope: `audits/<scope-id>.json`, written before the
engagement, naming who authorized it and exactly which address ranges and
domains may be examined, between which dates:

  {"id": "acme-2026-11", "client": "Acme Co",
   "authorized_by": "Jane Doe, IT Director",
   "authorization_ref": "SOW #123 / signed email 2026-11-17",
   "cidrs": ["192.168.1.0/24"], "domains": ["acme.com"],
   "starts": "2026-11-18", "expires": "2026-11-20", "intensity": "gentle"}

Every tool takes the scope id and re-reads the file on each call, so deleting or
editing it takes effect at once. A target outside the scope, or a call outside its
dates, is refused before anything touches the network. Scanning tools are
WRITE-class (operator-only over MCP, y/N in the CLI) and every call is audited.

Discovery and assessment only. Deliberately NOT here: exploitation, credential
guessing, traffic interception, Wi-Fi attacks, denial of service.

  LOYD_AUDITS_DIR   where scope files + findings live (default ./audits)
  Enable with LOYD_KITS=...,audit
"""
import datetime as _dt
import ipaddress
import json
import os
import re
import shlex
import socket

_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_HOST = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$")
MAX_DISCOVER_HOSTS = 1024            # /22 — anything bigger is split by the operator
INTENSITY = {
    "gentle": {"rate": 50, "top": 100, "timing": "-T2"},
    "normal": {"rate": 300, "top": 1000, "timing": "-T3"},
}
# port -> (severity, title, fix)
RISKY_PORTS = {
    23: ("high", "Telnet open", "Disable Telnet; use SSH."),
    21: ("medium", "FTP open", "Replace with SFTP, or restrict to the hosts that need it."),
    2375: ("critical", "Docker API without TLS", "Close 2375; anyone reaching it controls the host."),
    5900: ("high", "VNC open", "Put VNC behind a VPN and require strong auth."),
    5901: ("high", "VNC open", "Put VNC behind a VPN and require strong auth."),
    3389: ("medium", "RDP open", "Restrict RDP to a VPN or jump host; require NLA."),
    445: ("medium", "SMB open", "Limit SMB to the hosts that need file sharing; disable SMBv1."),
    139: ("medium", "NetBIOS/SMB open", "Disable NetBIOS where not needed."),
    3306: ("high", "MySQL reachable from the network", "Bind to localhost or firewall to the app hosts."),
    5432: ("high", "PostgreSQL reachable from the network", "Bind to localhost or firewall to the app hosts."),
    6379: ("high", "Redis reachable from the network", "Bind to localhost and require a password."),
    27017: ("high", "MongoDB reachable from the network", "Bind to localhost and enable auth."),
    9200: ("high", "Elasticsearch reachable from the network", "Firewall it and enable security."),
    9100: ("low", "Raw printing port open", "Restrict printer ports to the print server."),
    8080: ("info", "HTTP on 8080 (often an admin panel)", "Check it isn't an unauthenticated admin page."),
    8443: ("info", "HTTPS on 8443 (often an admin panel)", "Check it isn't an exposed admin page."),
    161: ("medium", "SNMP open", "Use SNMPv3 or disable; change default communities."),
}
SEV_ORDER = ["critical", "high", "medium", "low", "info"]


class ScopeError(ValueError):
    pass


def audits_dir():
    return os.getenv("LOYD_AUDITS_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "audits")


def load_scope(scope_id, today=None):
    if not _ID.match(scope_id or ""):
        raise ScopeError("scope id must be a-z 0-9 _ - (the file name under audits/)")
    path = os.path.join(audits_dir(), f"{scope_id}.json")
    if not os.path.isfile(path):
        raise ScopeError(f"no scope file audits/{scope_id}.json — write one before auditing")
    with open(path) as f:
        sc = json.load(f)
    for k in ("client", "authorized_by", "authorization_ref", "starts", "expires"):
        if not str(sc.get(k, "")).strip():
            raise ScopeError(f"scope {scope_id}: '{k}' is required")
    try:
        sc["_cidrs"] = [ipaddress.ip_network(c, strict=False) for c in sc.get("cidrs", [])]
    except ValueError as e:
        raise ScopeError(f"scope {scope_id}: bad cidr ({e})")
    sc["_domains"] = [d.lower().rstrip(".") for d in sc.get("domains", [])]
    if not sc["_cidrs"] and not sc["_domains"]:
        raise ScopeError(f"scope {scope_id}: list at least one cidr or domain")
    starts, expires = (_dt.date.fromisoformat(sc["starts"]), _dt.date.fromisoformat(sc["expires"]))
    today = today or _dt.datetime.now(_dt.timezone.utc).date()
    if today < starts:
        raise ScopeError(f"scope {scope_id} starts {starts}; not yet authorized")
    if today > expires:
        raise ScopeError(f"scope {scope_id} expired {expires}; get a new authorization")
    if sc.get("intensity", "gentle") not in INTENSITY:
        raise ScopeError(f"scope {scope_id}: intensity must be gentle or normal")
    sc["id"] = scope_id
    return sc


def _domain_in_scope(host, sc):
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in sc["_domains"])


def _ip_in_scope(ip, sc):
    a = ipaddress.ip_address(ip)
    return any(a in n for n in sc["_cidrs"])


def check_network_target(target, sc, resolve=socket.getaddrinfo):
    """An IP, a CIDR, or a hostname whose every address is inside the scope's
    cidrs. -> normalized target string. Used by tools that send probes."""
    t = (target or "").strip()
    try:
        net = ipaddress.ip_network(t, strict=False)
    except ValueError:
        net = None
    if net is not None:
        if not any(net.subnet_of(n) for n in sc["_cidrs"] if n.version == net.version):
            raise ScopeError(f"{t} is outside scope {sc['id']} ({', '.join(map(str, sc['_cidrs'])) or 'no cidrs'})")
        return str(net) if net.num_addresses > 1 else str(net.network_address)
    if not _HOST.match(t):
        raise ScopeError(f"{t!r} is not an IP, CIDR or hostname")
    try:
        ips = sorted({ai[4][0] for ai in resolve(t, None)})
    except OSError:
        raise ScopeError(f"{t} does not resolve")
    outside = [ip for ip in ips if not _ip_in_scope(ip, sc)]
    if not ips or outside:
        raise ScopeError(f"{t} resolves to {', '.join(outside or ips)}, outside scope {sc['id']}")
    return t


def check_domain_target(domain, sc):
    d = (domain or "").strip().lower().rstrip(".")
    if not _HOST.match(d) or not _domain_in_scope(d, sc):
        raise ScopeError(f"{domain!r} is not one of scope {sc['id']}'s domains ({', '.join(sc['_domains']) or 'none'})")
    return d


# ── findings ─────────────────────────────────────────────────────────────────
def _findings_path(sc):
    return os.path.join(audits_dir(), f"{sc['id']}.findings.jsonl")


def record(sc, findings):
    if not findings:
        return
    with open(_findings_path(sc), "a") as f:
        for fd in findings:
            f.write(json.dumps(fd) + "\n")


def _fmt(findings):
    if not findings:
        return "No findings."
    return "\n".join(f"[{f['severity'].upper()}] {f['target']}: {f['title']} — {f['fix']}" for f in findings)


# ── parsers (pure; tested on captured output) ────────────────────────────────
def parse_discover(grepable):
    return [m.group(1) for m in re.finditer(r"^Host: (\S+) .*Status: Up", grepable, re.M)]


def parse_services(grepable):
    """-> [(ip, port, proto, service, version)] open ports from nmap -oG."""
    out = []
    for line in grepable.splitlines():
        m = re.match(r"^Host: (\S+) .*Ports: (.*?)(\tIgnored|$)", line)
        if not m:
            continue
        for p in m.group(2).split(", "):
            f = p.split("/")
            if len(f) >= 7 and f[1] == "open":
                out.append((m.group(1), int(f[0]), f[2], f[4], f[6]))
    return out


def findings_for_services(services):
    fds = []
    for ip, port, proto, svc, ver in services:
        if port in RISKY_PORTS:
            sev, title, fix = RISKY_PORTS[port]
            fds.append({"severity": sev, "target": f"{ip}:{port}/{proto}",
                        "title": title, "detail": f"{svc} {ver}".strip(), "fix": fix})
    return fds


def findings_for_tls(host, port, cert_text, legacy, today=None):
    fds, target = [], f"{host}:{port}"
    m = re.search(r"notAfter=(.+)", cert_text or "")
    if not m:
        return [{"severity": "medium", "target": target, "title": "No TLS certificate read",
                 "detail": "handshake failed or no TLS on this port", "fix": "Check the service serves TLS."}]
    end = _dt.datetime.strptime(m.group(1).strip().replace("  ", " "), "%b %d %H:%M:%S %Y %Z").date()
    days = (end - (today or _dt.datetime.now(_dt.timezone.utc).date())).days
    if days < 0:
        fds.append({"severity": "critical", "target": target, "title": "TLS certificate expired",
                    "detail": f"expired {end}", "fix": "Renew the certificate now."})
    elif days < 14:
        fds.append({"severity": "high", "target": target, "title": "TLS certificate expires within 14 days",
                    "detail": f"expires {end}", "fix": "Renew, and automate renewal."})
    elif days < 30:
        fds.append({"severity": "medium", "target": target, "title": "TLS certificate expires within 30 days",
                    "detail": f"expires {end}", "fix": "Schedule renewal."})
    for proto in legacy:
        fds.append({"severity": "medium", "target": target, "title": f"Accepts {proto}",
                    "detail": f"{proto} is deprecated", "fix": "Allow TLS 1.2 and 1.3 only."})
    return fds


def findings_for_mail(domain, spf_txt, dmarc_txt):
    fds = []
    spf = [t for t in spf_txt if t.lower().startswith("v=spf1")]
    if not spf:
        fds.append({"severity": "medium", "target": domain, "title": "No SPF record",
                    "detail": "anyone can claim to send mail as this domain",
                    "fix": "Publish an SPF record listing the real senders, ending in -all or ~all."})
    elif any(re.search(r"(^|\s)\+?all\b", s) and not re.search(r"[-~?]all\b", s) for s in spf):
        fds.append({"severity": "high", "target": domain, "title": "SPF allows any sender (+all)",
                    "detail": spf[0], "fix": "End SPF with -all or ~all."})
    dm = [t for t in dmarc_txt if t.lower().startswith("v=dmarc1")]
    if not dm:
        fds.append({"severity": "medium", "target": domain, "title": "No DMARC record",
                    "detail": "spoofed mail isn't rejected or reported",
                    "fix": "Publish _dmarc with p=none and rua= reporting, then move to quarantine/reject."})
    elif re.search(r"\bp=none\b", dm[0], re.I):
        fds.append({"severity": "low", "target": domain, "title": "DMARC policy is p=none",
                    "detail": dm[0], "fix": "After reviewing reports, move to p=quarantine or p=reject."})
    return fds


def _txt(dig_output):
    return [" ".join(re.findall(r'"([^"]*)"', line)) for line in dig_output.splitlines() if '"' in line]


# ── tools ────────────────────────────────────────────────────────────────────
TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": "audit_scope",
        "description": "[audit kit] Show an audit scope: client, who authorized it, allowed ranges and domains, dates. "
                       "Start every audit here.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string", "description": "scope id (file name under audits/)"}}, "required": ["scope"]}}},
    {"type": "function", "function": {"name": "audit_discover",
        "description": "[audit kit] REQUIRES operator confirmation. Find live hosts in an in-scope range (ping sweep, "
                       "no port scan). Refused outside the scope.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string"}, "cidr": {"type": "string", "description": "range inside the scope, e.g. 192.168.1.0/24"}},
            "required": ["scope", "cidr"]}}},
    {"type": "function", "function": {"name": "audit_services",
        "description": "[audit kit] REQUIRES operator confirmation. Inventory open TCP ports and service versions on an "
                       "in-scope host or range (rate-limited by the scope's intensity) and flag risky exposures.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string"}, "target": {"type": "string", "description": "in-scope IP, range or hostname"}},
            "required": ["scope", "target"]}}},
    {"type": "function", "function": {"name": "audit_tls",
        "description": "[audit kit] Check an in-scope TLS service: certificate subject, issuer, expiry, and whether it "
                       "still accepts TLS 1.0/1.1.",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string"}, "host": {"type": "string"},
            "port": {"type": "integer", "description": "default 443"}}, "required": ["scope", "host"]}}},
    {"type": "function", "function": {"name": "audit_mail_dns",
        "description": "[audit kit] Check an in-scope domain's mail security records: MX, SPF, DMARC (public DNS only).",
        "parameters": {"type": "object", "properties": {
            "scope": {"type": "string"}, "domain": {"type": "string"}}, "required": ["scope", "domain"]}}},
    {"type": "function", "function": {"name": "audit_report",
        "description": "[audit kit] Write the findings so far as a markdown report (severity, target, fix) headed by "
                       "the scope and authorization.",
        "parameters": {"type": "object", "properties": {"scope": {"type": "string"}}, "required": ["scope"]}}},
]
WRITE_CLASS = {"audit_discover", "audit_services"}       # active probes: operator-only, confirmed


def install(tool_schemas, impl, write_tools, exec_fn, cap):
    """Register the audit tools. exec_fn(cmd, timeout) -> (rc, stdout, stderr)."""
    def guarded(fn):
        def run(a, ro):
            try:
                return fn(a or {})
            except ScopeError as e:
                return f"REFUSED: {e}"
            except (ValueError, KeyError) as e:
                return f"ERROR: {e}"
        return run

    def need(tool):
        rc, out, _ = exec_fn(f"command -v {tool}", 10)
        if rc != 0 or not out.strip():
            raise ValueError(f"{tool} is not installed on the device Loyd is acting on")

    def scope_tool(a):
        sc = load_scope(a.get("scope"))
        days = (_dt.date.fromisoformat(sc["expires"]) - _dt.datetime.now(_dt.timezone.utc).date()).days
        return (f"Scope {sc['id']} — {sc['client']}\nAuthorized by: {sc['authorized_by']} "
                f"({sc['authorization_ref']})\nValid: {sc['starts']} to {sc['expires']} ({days} days left)\n"
                f"Ranges: {', '.join(map(str, sc['_cidrs'])) or 'none'}\nDomains: {', '.join(sc['_domains']) or 'none'}\n"
                f"Intensity: {sc.get('intensity', 'gentle')}")

    def discover(a):
        sc = load_scope(a.get("scope"))
        cidr = check_network_target(a.get("cidr"), sc)
        if ipaddress.ip_network(cidr, strict=False).num_addresses > MAX_DISCOVER_HOSTS:
            raise ScopeError(f"{cidr} is bigger than /22; sweep it in smaller ranges")
        need("nmap")
        it = INTENSITY[sc.get("intensity", "gentle")]
        rc, out, err = exec_fn(f"nmap -sn -n {it['timing']} --max-rate {it['rate']} -oG - {shlex.quote(cidr)}", 600)
        hosts = parse_discover(out)
        return cap(f"{len(hosts)} live host(s) in {cidr}:\n" + "\n".join(hosts) + (f"\n[stderr] {err.strip()}" if rc else ""))

    def services(a):
        sc = load_scope(a.get("scope"))
        target = check_network_target(a.get("target"), sc)
        need("nmap")
        it = INTENSITY[sc.get("intensity", "gentle")]
        rc, out, err = exec_fn(
            f"nmap -sV --version-light -Pn -n {it['timing']} --max-rate {it['rate']} "
            f"--top-ports {it['top']} -oG - {shlex.quote(target)}", 600)
        svcs = parse_services(out)
        fds = findings_for_services(svcs)
        record(sc, fds)
        lines = [f"{ip}:{port}/{proto}  {svc}  {ver}".rstrip() for ip, port, proto, svc, ver in svcs]
        return cap(f"{len(svcs)} open port(s):\n" + ("\n".join(lines) or "(none)") + "\n\nFindings:\n" + _fmt(fds)
                   + (f"\n[stderr] {err.strip()}" if rc else ""))

    def tls(a):
        sc = load_scope(a.get("scope"))
        host = (a.get("host") or "").strip()
        if _HOST.match(host) and _domain_in_scope(host, sc):
            pass                                      # a public site on an in-scope domain
        else:
            host = check_network_target(host, sc)
        port = int(a.get("port") or 443)
        if not 1 <= port <= 65535:
            raise ValueError("port must be 1-65535")
        need("openssl")
        h, p = shlex.quote(host), port
        rc, out, _ = exec_fn(f"echo | openssl s_client -connect {h}:{p} -servername {h} 2>/dev/null"
                             f" | openssl x509 -noout -subject -issuer -enddate", 30)
        legacy = []
        for flag, name in (("-tls1", "TLS 1.0"), ("-tls1_1", "TLS 1.1")):
            lrc, lout, _ = exec_fn(f"echo | openssl s_client {flag} -connect {h}:{p} -servername {h} 2>/dev/null"
                                   f" | grep -q '^ *Cipher *: *[A-Z0-9]' && echo yes", 20)
            if lout.strip() == "yes":
                legacy.append(name)
        fds = findings_for_tls(host, port, out, legacy)
        record(sc, fds)
        return cap((out.strip() or "(no certificate)") + "\n\nFindings:\n" + _fmt(fds))

    def mail(a):
        sc = load_scope(a.get("scope"))
        d = check_domain_target(a.get("domain"), sc)
        need("dig")
        q = shlex.quote
        _, mx, _ = exec_fn(f"dig +short +time=3 +tries=1 MX {q(d)}", 15)
        _, spf, _ = exec_fn(f"dig +time=3 +tries=1 TXT {q(d)} +noall +answer", 15)
        _, dmarc, _ = exec_fn(f"dig +time=3 +tries=1 TXT {q('_dmarc.' + d)} +noall +answer", 15)
        fds = findings_for_mail(d, _txt(spf), _txt(dmarc))
        record(sc, fds)
        return cap(f"MX:\n{mx.strip() or '(none)'}\nSPF: {next((t for t in _txt(spf) if t.lower().startswith('v=spf1')), '(none)')}\n"
                   f"DMARC: {next(iter(_txt(dmarc)), '(none)')}\n\nFindings:\n" + _fmt(fds))

    def report(a):
        sc = load_scope(a.get("scope"))
        fds = []
        if os.path.exists(_findings_path(sc)):
            seen = set()
            for line in open(_findings_path(sc)):
                f = json.loads(line)
                key = (f["severity"], f["target"], f["title"])
                if key not in seen:
                    seen.add(key)
                    fds.append(f)
        fds.sort(key=lambda f: (SEV_ORDER.index(f["severity"]), f["target"]))
        counts = ", ".join(f"{sum(f['severity'] == s for f in fds)} {s}" for s in SEV_ORDER)
        md = [f"# Network audit — {sc['client']}", "",
              f"Scope `{sc['id']}` · authorized by {sc['authorized_by']} ({sc['authorization_ref']}) · "
              f"{sc['starts']} to {sc['expires']}",
              f"Ranges: {', '.join(map(str, sc['_cidrs'])) or 'none'} · Domains: {', '.join(sc['_domains']) or 'none'}",
              "", f"**{len(fds)} findings:** {counts}", "",
              "| Severity | Target | Finding | Fix |", "| --- | --- | --- | --- |"]
        md += [f"| {f['severity']} | {f['target']} | {f['title']} ({f['detail']}) | {f['fix']} |" for f in fds]
        md += ["", "Discovery and configuration review only; no exploitation or credential testing was performed."]
        path = os.path.join(audits_dir(), f"{sc['id']}.report.md")
        with open(path, "w") as f:
            f.write("\n".join(md) + "\n")
        return cap("\n".join(md) + f"\n\n(saved to {path})")

    impls = {"audit_scope": scope_tool, "audit_discover": discover, "audit_services": services,
             "audit_tls": tls, "audit_mail_dns": mail, "audit_report": report}
    for s in TOOL_SCHEMAS:
        name = s["function"]["name"]
        tool_schemas.append(s)
        impl[name] = guarded(impls[name])
    write_tools |= WRITE_CLASS
