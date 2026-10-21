"""Debian 13 node lifecycle. Run only through the root-owned launcher."""
import argparse
import contextlib
import datetime as dt
import getpass
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time

from . import maintenance, model, network, releases, system

ETC = Path("/etc/argosbx-hy2")
DATA = Path("/var/lib/argosbx-hy2")
CURRENT = ETC / "current"
STATE = DATA / "state.json"
JOURNAL = DATA / "transaction.json"
SERVICES = ["argosbx-hy2.service", "argosbx-web.service"]
APP = Path(__file__).resolve().parent.parent
RESOLVER_POLICY = Path("/etc/systemd/resolved.conf.d/argosbx-hy2.conf")
UNIT_DIR = Path("/etc/systemd/system")
APT_POLICY = Path("/etc/apt/apt.conf.d/52argosbx-hy2-auto-upgrades")


class Error(RuntimeError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_json(path, default=None):
    return json.loads(Path(path).read_text()) if Path(path).exists() else default


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_journal():
    JOURNAL.unlink()
    sync_directory(JOURNAL.parent)


def atomic(path, content, mode=0o600, group=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".new-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        if group:
            import grp
            os.fchown(fd, 0, grp.getgrnam(group).gr_gid)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save(path, data):
    atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def run(args, *, check=True, timeout=120, input=None, env=None):
    result = subprocess.run([str(x) for x in args], input=input, capture_output=True,
                            text=True, timeout=timeout, env=env)
    if check and result.returncode:
        # Tools may echo API credentials or complete generated configurations.
        # Preserve the executable and exit status, never arbitrary tool output.
        raise Error(f"{Path(str(args[0])).name} 执行失败（退出码 {result.returncode}）")
    return result


def event(stage, **values):
    save(DATA / "last-operation.json", {"time": now(), "stage": stage, **values})


def ensure_public_page():
    public = DATA / "public"
    page = public / "index.html"
    if public.is_symlink() or page.is_symlink():
        raise Error("公开网站路径不能是符号链接")
    if not page.exists():
        atomic(page, model.PUBLIC_PAGE, 0o644)


def root_platform():
    if platform.system() != "Linux" or os.geteuid() != 0:
        raise Error("请在 Debian 13 上使用 root 或 sudo 执行")
    info = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            info[key] = value.strip('"')
    if info.get("ID") != "debian" or info.get("VERSION_ID") != "13":
        raise Error("当前仅支持 Debian 13（trixie）")
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if not arch:
        raise Error("仅支持 amd64 / arm64")
    if not Path("/run/systemd/system").exists():
        raise Error("需要以 systemd 启动的完整 VPS 系统")
    return arch


@contextlib.contextmanager
def lock(name="argosbx-hy2"):
    import fcntl
    fd = os.open(f"/run/lock/{name}.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "r+") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o022 or info.st_nlink != 1):
            raise Error("管理锁不属于当前用户或类型/权限异常，停止修改")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Error("另一项安装/维护任务正在运行，稍后重试") from None
        yield


def linked(path, target):
    temp = Path(str(path) + ".next")
    temp.unlink(missing_ok=True)
    temp.symlink_to(target)
    os.replace(temp, path)
    sync_directory(path.parent)


def current():
    if not CURRENT.is_symlink():
        raise Error("尚未完成安装，请运行 argosbx install")
    path = CURRENT.resolve()
    if path.parent != (DATA / "generations").resolve() or not (path / "node.json").exists():
        raise Error("当前版本路径异常，停止修改")
    return path


def recover(restart=True):
    tx = read_json(JOURNAL)
    if not tx:
        return
    if not restart and maintenance.owner_alive(tx):
        return  # systemd prerequisites of the live activation must not undo it.
    old = tx["old"]
    if old:
        oldpath = Path(old)
        if oldpath.parent != DATA / "generations" or not oldpath.is_dir():
            raise Error("事务备份路径异常，需人工恢复")
        linked(CURRENT, oldpath)
    else:
        CURRENT.unlink(missing_ok=True)
        sync_directory(CURRENT.parent)
    save(STATE, tx["old_state"])
    if restart:
        run(["systemctl", "reset-failed", *SERVICES], check=False)
        run(["systemctl", "restart" if old else "stop", *SERVICES])
        if old:
            wait_for_probe(Path(old))
        # Keep the recovery record if systemctl/readiness fails, including across
        # reboot. A later invocation retries the same idempotent restoration.
        remove_journal()
        event("recovered", message="已恢复中断事务之前的版本")


def preflight():
    if shutil.disk_usage(DATA).free < 1024 ** 3:
        raise Error("至少需要 1 GiB 空闲空间（含下载、候选版本和回滚空间）")
    synced = run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"]).stdout.strip()
    if synced != "yes":
        raise Error("系统时间尚未同步；请先检查 timedatectl 的 NTP 状态")
    if not CURRENT.is_symlink():
        for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
            with socket.socket(socket.AF_INET, kind) as sock:
                try:
                    sock.bind(("0.0.0.0", 443))
                except OSError:
                    raise Error("TCP 或 UDP 443 已被占用，未停止现有服务") from None
        # Include v6-only listeners that might collide with later configuration.
        listeners = run(["ss", "-H", "-lntup", "sport = :443"]).stdout.strip()
        if listeners:
            raise Error("发现已有 443 监听，请使用独立 VPS 或先自行处理冲突")


def identities():
    import grp
    import pwd
    registry = Path("/opt/argosbx-hy2/identities.json")
    owned = read_json(registry, {})
    try:
        grp.getgrnam("argosbx-tls")
        if not owned.get("tls"):
            raise Error("发现未知 argosbx-tls 组，拒绝复用")
    except KeyError:
        run(["groupadd", "--system", "argosbx-tls"])
        owned["tls"] = grp.getgrnam("argosbx-tls").gr_gid
        save(registry, owned)
    for name in ("argosbx-hy2", "argosbx-web"):
        try:
            user = pwd.getpwnam(name)
            if (owned.get(name) != user.pw_uid or
                    user.pw_shell not in ("/usr/sbin/nologin", "/sbin/nologin") or user.pw_uid == 0):
                raise Error(f"系统已有不符合要求的 {name} 用户")
        except KeyError:
            run(["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", "/nonexistent",
                 "--shell", "/usr/sbin/nologin", name])
            owned[name] = pwd.getpwnam(name).pw_uid
            save(registry, owned)
        run(["usermod", "-a", "-G", "argosbx-tls", name])


def verify_server_address(address, server_ip=None):
    if not ipaddress.IPv4Address(address).is_global:
        raise Error("A 记录必须是公网 IPv4")
    interfaces = json.loads(run(["ip", "-j", "-4", "address", "show"]).stdout)
    local_ips = {a["local"] for interface in interfaces for a in interface.get("addr_info", [])}
    if server_ip:
        if not ipaddress.IPv4Address(server_ip).is_global:
            raise Error("--server-ip 必须是公网 IPv4")
        local_ips.add(server_ip)
    if str(address) not in local_ips:
        raise Error("域名未指向本机 IPv4；NAT VPS 可显式指定 --server-ip")


def dns_preflight(node, token=None, server_ip=None):
    domain = node["domain"]
    if token is None:
        answers = run(["dig", "+short", "@1.1.1.1", domain, "A"]).stdout.split()
        v6 = run(["dig", "+short", "@1.1.1.1", domain, "AAAA"]).stdout.strip()
        if len(answers) != 1 or v6:
            raise Error("HTTP 验证需要唯一的公网 A 记录，首版暂不接受 AAAA / CNAME")
        try:
            address = ipaddress.IPv4Address(answers[0])
        except ValueError:
            raise Error("域名必须使用直接 A 记录，不接受 CNAME") from None
        verify_server_address(address, server_ip)
        print(f"公共 DNS 检查通过：{domain} → {address}")
        return
    # Check the exact dedicated record through Cloudflare, without changing A/AAAA.
    import urllib.parse
    import urllib.request
    def cf(endpoint):
        req = urllib.request.Request("https://api.cloudflare.com/client/v4/" + endpoint,
                                     headers={"Authorization": "Bearer " + token,
                                              "User-Agent": "ArgoSBX-HY2"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
        except Exception:
            raise Error("Cloudflare 查询失败，请检查 Token 权限及网络") from None
        if not data.get("success"):
            raise Error("Cloudflare Token 未获得所需 Zone / DNS 权限")
        return data["result"]
    zones = []
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        zones = cf("zones?name=" + ".".join(parts[i:]))
        if zones:
            break
    if len(zones) != 1:
        raise Error("无法唯一确定该域名的 Cloudflare Zone")
    records = cf(f'zones/{zones[0]["id"]}/dns_records?name={domain}')
    a = [r for r in records if r["type"] == "A"]
    if len(a) != 1 or any(r.get("proxied") for r in records):
        raise Error("请先为专用子域名建立一个 DNS only（灰云）A 记录")
    if any(r["type"] in ("AAAA", "CNAME") for r in records):
        raise Error("第一版为 IPv4 模式；专用记录暂不能含 AAAA / CNAME")
    ip = a[0]["content"]
    if not ipaddress.ip_address(ip).is_global:
        raise Error("A 记录必须指向 VPS 的公网 IPv4")
    answers = set(run(["dig", "+short", "@1.1.1.1", domain, "A"]).stdout.split())
    v6 = run(["dig", "+short", "@1.1.1.1", domain, "AAAA"]).stdout.strip()
    if answers != {ip} or v6:
        raise Error("公共 DNS 尚未与 Cloudflare 记录一致，等待传播后重试")
    verify_server_address(ip, server_ip)
    # VPS may sit behind NAT; do not guess its public IP through an unrelated service.
    print(f"DNS 检查通过：{domain} → {ip}；稍后将验证此公网地址的 HTTPS")


def certificate_check(cert, key, domain):
    cert, key = Path(cert), Path(key)
    text = cert.read_text()
    blocks = re.findall(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", text, re.S)
    if len(blocks) < 2:
        raise Error("证书缺少完整的中间证书链")
    with tempfile.TemporaryDirectory(prefix="tls-check-", dir=DATA) as tmp:
        leaf, chain = Path(tmp) / "leaf.pem", Path(tmp) / "chain.pem"
        leaf.write_text(blocks[0] + "\n")
        chain.write_text("\n".join(blocks[1:]) + "\n")
        run(["openssl", "verify", "-purpose", "sslserver", "-verify_hostname", domain,
             "-CAfile", "/etc/ssl/certs/ca-certificates.crt", "-untrusted", chain, leaf])
        run(["openssl", "x509", "-in", leaf, "-checkend", "86400", "-noout"])
        san = run(["openssl", "x509", "-in", leaf, "-noout", "-ext", "subjectAltName"]).stdout
        if f"DNS:{domain}" not in [v.strip() for v in san.replace("\n", ",").split(",")]:
            raise Error("证书 SAN 未包含精确域名")
        certpub = run(["openssl", "x509", "-in", leaf, "-pubkey", "-noout"]).stdout
        keypub = run(["openssl", "pkey", "-in", key, "-pubout"]).stdout
        if certpub != keypub:
            raise Error("证书与私钥不匹配")
    return text


def certificate_command(node, lego, acme, email, challenge):
    # lego 5 unifies issuance and lifetime/ARI-based renewal under `run`.
    command = [lego, "--log.level", "error", "run", "--path", acme,
               "--server", "https://acme-v02.api.letsencrypt.org/directory",
               "--account-id", "argosbx-hy2", "--domains", node["domain"], "--accept-tos",
               "--key-type", "EC256", "--cert.name", node["domain"], "--no-random-sleep"]
    if email:
        command += ["--email", email]
    if challenge == "http":
        command += ["--http", "--http.address", "0.0.0.0:80"]
    elif challenge == "dns":
        command += ["--dns", "cloudflare", "--dns.resolvers", "1.1.1.1:53", "--dns.resolvers", "8.8.8.8:53"]
    else:
        raise Error("未知证书验证方式")
    return command


def remove_ufw_comment(comment):
    if not shutil.which("ufw"):
        return
    text = run(["ufw", "status", "numbered"], check=False, env={**os.environ, "LC_ALL": "C"}).stdout
    indices = []
    for line in text.splitlines():
        match = re.match(r"\[\s*(\d+)\].*# " + re.escape(comment) + r"\s*$", line)
        if match:
            indices.append(int(match[1]))
    for index in sorted(indices, reverse=True):
        run(["ufw", "--force", "delete", str(index)])


def cleanup_http_challenge():
    state = read_json(ETC / "firewall.json", {})
    if state.get("http_challenge"):
        remove_ufw_comment("argosbx-hy2-acme-http")
        state.pop("http_challenge", None)
        save(ETC / "firewall.json", state)


@contextlib.contextmanager
def http_challenge(external=False):
    if run(["ss", "-H", "-lnt", "sport = :80"]).stdout.strip():
        raise Error("HTTP-01 需要空闲 TCP 80；未停止已有网站，可改用 --challenge dns")
    try:
        with socket.socket() as sock:
            sock.bind(("0.0.0.0", 80))
        if not external and shutil.which("ufw"):
            status = run(["ufw", "status"], env={**os.environ, "LC_ALL": "C"}).stdout
            if "Status: active" in status and not re.search(r"80(?:/tcp)?\s+ALLOW", status):
                owned = read_json(ETC / "firewall.json", {"ufw": []})
                owned["http_challenge"] = True
                save(ETC / "firewall.json", owned)
                run(["ufw", "allow", "80/tcp", "comment", "argosbx-hy2-acme-http"])
        yield
    finally:
        cleanup_http_challenge()


def obtain_certificate(node, lego, email, challenge="http", external=False):
    acme = DATA / "acme"
    acme.mkdir(mode=0o700, exist_ok=True)
    tokenfile = ETC / "cloudflare.token"
    env = dict(os.environ)
    # Remove alternate inherited credentials to ensure the scoped file is used.
    for key in list(env):
        if key.startswith(("CF_", "CLOUDFLARE_", "LEGO_")):
            del env[key]
    if challenge == "dns":
        env["CF_DNS_API_TOKEN_FILE"] = str(tokenfile)
        env["CF_ZONE_API_TOKEN_FILE"] = str(tokenfile)
    command = certificate_command(node, lego, acme, email, challenge)
    cert = acme / "certificates" / (node["domain"] + ".crt")
    key = cert.with_suffix(".key")
    event("certificate-request")
    with http_challenge(external) if challenge == "http" else contextlib.nullcontext():
        run(command, env=env, timeout=1200)
    certificate_check(cert, key, node["domain"])
    return cert, key


def generation():
    parent = DATA / "generations"
    parent.mkdir(mode=0o711, exist_ok=True)
    parent.chmod(0o711)
    path = DATA / "generations" / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(4))
    path.mkdir(parents=True, mode=0o711)
    path.chmod(0o711)
    (path / "bin").mkdir(mode=0o755)
    (path / "bin").chmod(0o755)
    return path


def copy_generation(old):
    stage = generation()
    # Preserve explicit restricted modes and ownership; never copy symlinks.
    for src in old.rglob("*"):
        if src.is_symlink():
            raise Error("版本目录包含意外符号链接")
        dest = stage / src.relative_to(old)
        if src.is_dir():
            dest.mkdir(exist_ok=True)
        else:
            shutil.copy2(src, dest)
        stat = src.stat()
        os.chown(dest, stat.st_uid, stat.st_gid)
        dest.chmod(stat.st_mode & 0o777)
    return stage


def write_generation(stage, node, cert, key, ua=None):
    save(stage / "node.json", node)
    for directory, group in [("tls", "argosbx-tls"), ("exports", "argosbx-web")]:
        (stage / directory).mkdir(exist_ok=True)
        shutil.chown(stage / directory, group=group)
        (stage / directory).chmod(0o750)
    atomic(stage / "tls/fullchain.pem", Path(cert).read_text(), 0o640, "argosbx-tls")
    atomic(stage / "tls/key.pem", Path(key).read_text(), 0o640, "argosbx-tls")
    atomic(stage / "server.json", json.dumps(model.server_config(node), indent=2), 0o640, "argosbx-hy2")
    atomic(stage / "Caddyfile", model.caddy_config(node, ua_rules=ua), 0o640, "argosbx-web")
    for name, content in model.exports(node).items():
        atomic(stage / "exports" / name, content, 0o640, "argosbx-web")
    save(stage / "ua.json", ua or [])


def validate_generation(stage):
    paths = list(stage.rglob('*'))
    if any(path.is_symlink() for path in paths):
        raise Error("候选版本存在符号链接，停止切换")
    node = model.validate_node(read_json(stage / "node.json"))
    certificate_check(stage / "tls/fullchain.pem", stage / "tls/key.pem", node["domain"])
    sb = stage / "bin/sing-box"
    versions = read_json(stage / "versions.json")
    version = versions["sing-box"]
    actual = run([sb, "version"]).stdout
    if not re.search(r"sing-box version " + re.escape(version.lstrip("v")) + r"(?:\s|$)", actual):
        raise Error("内核实际版本与下载记录不一致")
    for binary, arguments, prefix in [("caddy", ["version"], "v"), ("lego", ["--version"], "")]:
        output = run([stage / "bin" / binary, *arguments]).stdout
        expected = prefix + versions[binary].lstrip("v")
        if not re.search(r"(?<![\d.])" + re.escape(expected) + r"(?:\s|$)", output):
            raise Error(f"{binary} 实际版本与固定清单不一致")
    # Validate the files that will actually run, including copied update configs.
    # Only substitute current's file paths so checks read staged certificates.
    staged_server = read_json(stage / "server.json")
    if [item.get("type") for item in staged_server.get("inbounds", [])] != ["hysteria2"]:
        raise Error("仅允许单个 Hysteria2 代理入站")
    tls = staged_server["inbounds"][0]["tls"]
    for field, filename in [("certificate_path", "fullchain.pem"), ("key_path", "key.pem")]:
        if tls.get(field) != f"{model.ROOT}/tls/{filename}":
            raise Error("服务端证书路径不属于本项目当前部署")
        tls[field] = str(stage / "tls" / filename)
    staged_caddy = (stage / "Caddyfile").read_text().replace(model.ROOT, str(stage))
    checks = [(stage / ".check-server.json", json.dumps(staged_server),
               [sb, "check", "-c"]),
              (stage / ".check-Caddyfile", staged_caddy,
               [stage / "bin/caddy", "validate", "--adapter", "caddyfile", "--config"])]
    for path, content, command in checks:
        atomic(path, content)
        try:
            run([*command, path])
        finally:
            path.unlink(missing_ok=True)
    run([sb, "check", "-c", stage / "exports/singbox.json"])
    # copy2/chmod and executable renames are not durable merely because the
    # config files were atomic. Persist the complete candidate before switching.
    for path in paths:
        if path.is_file():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in paths if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        sync_directory(path)
    sync_directory(stage)
    sync_directory(stage.parent)


def set_units():
    for name, content in system.units().items():
        path = UNIT_DIR / name
        if path.is_symlink() or (path.exists() and not path.read_text().startswith("# Managed by ArgoSBX-HY2\n")):
            raise Error(f"发现未知同名服务 {name}，拒绝覆盖")
        atomic(path, content, 0o644)
    run(["systemctl", "daemon-reload"])


def resolver_privacy(remove=False):
    path = RESOLVER_POLICY
    marker = "# Managed by ArgoSBX-HY2\n"
    if path.is_symlink() or (path.exists() and not path.read_text().startswith(marker)):
        if remove:
            return
        raise Error("发现未知同名解析器配置，拒绝覆盖")
    active = run(["systemctl", "is-active", "--quiet", "systemd-resolved.service"], check=False).returncode == 0
    if remove:
        changed = path.exists()
        path.unlink(missing_ok=True)
    elif active:
        content = marker + "[Resolve]\nLLMNR=no\nMulticastDNS=no\n"
        if path.parent.is_symlink():
            raise Error("解析器配置目录是符号链接，停止修改")
        created = not path.parent.exists()
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        permissions_changed = created
        if path.parent.stat().st_mode & 0o055 != 0o055:
            if any(p.name != path.name for p in path.parent.iterdir()):
                raise Error("既有解析器目录权限受限，未覆盖其他配置")
            path.parent.chmod(0o755)
            permissions_changed = True
        changed = permissions_changed or not path.exists() or path.read_text() != content
        if changed:
            atomic(path, content, 0o644)
    else:
        changed = False
    if active and changed:
        run(["systemctl", "restart", "systemd-resolved.service"])


def security_updates():
    marker = "// Managed by ArgoSBX-HY2\n"
    if APT_POLICY.is_symlink() or (APT_POLICY.exists() and not APT_POLICY.read_text().startswith(marker)):
        raise Error("发现未知同名自动更新配置，拒绝覆盖")
    if APT_POLICY.exists():
        return
    lines = []
    for key in ("Update-Package-Lists", "Unattended-Upgrade"):
        if not run(["apt-config", "shell", "ArgosbxPeriodic", "APT::Periodic::" + key]).stdout.strip():
            lines.append(f'APT::Periodic::{key} "1";\n')
    if lines:
        atomic(APT_POLICY, marker + "".join(lines), 0o644)
        run(["systemctl", "enable", "--now", "apt-daily.timer", "apt-daily-upgrade.timer"])


def guard_rules(uid, resolvers):
    lines = ["table inet argosbx_hy2 {", "chain output {", "type filter hook output priority 0; policy accept;"]
    # Reply packets for incoming HY2 connections may legitimately target loopback
    # during self-test. New outbound proxy flows still pass the destination guard.
    lines.append(f"meta skuid {uid} ct direction reply accept")
    # The system resolver may legitimately reside on loopback/private infrastructure.
    for address in resolvers:
        ip = ipaddress.ip_address(address)
        family = "ip" if ip.version == 4 else "ip6"
        lines.append(f"meta skuid {uid} {family} daddr {ip} meta l4proto {{ tcp, udp }} th dport 53 accept")
    v4 = ", ".join(x for x in model.BLOCKED if ":" not in x)
    v6 = ", ".join(x for x in model.BLOCKED if ":" in x)
    lines += [f"meta skuid {uid} ip daddr {{ {v4} }} reject",
              f"meta skuid {uid} ip6 daddr {{ {v6} }} reject", "}", "}"]
    return "\n".join(lines) + "\n"


def firewall():
    import pwd
    uid = pwd.getpwnam("argosbx-hy2").pw_uid
    existing = run(["nft", "list", "table", "inet", "argosbx_hy2"], check=False).returncode == 0
    owned = ETC / "firewall.json"
    state = read_json(owned, {"ufw": []})
    if existing and not state.get("guard_owned"):
        raise Error("发现同名未知防火墙表，拒绝覆盖")
    resolvers = []
    for line in Path("/etc/resolv.conf").read_text().splitlines():
        if line.startswith("nameserver "):
            address = line.split()[1]
            try:
                ipaddress.ip_address(address)
                resolvers.append(address)
            except ValueError:
                pass
    rules = guard_rules(uid, resolvers)
    script = ("delete table inet argosbx_hy2\n" if existing else "") + rules
    # A single nft transaction: there is no empty-table window.
    run(["nft", "-c", "-f", "-"], input=script)
    state["guard_owned"] = True
    save(owned, state)
    run(["nft", "-f", "-"], input=script)
    payload = json.loads(run(["nft", "-j", "list", "table", "inet", "argosbx_hy2"]).stdout)
    state["guard_digest"] = maintenance.firewall_digest(payload)
    save(owned, state)


def verify_firewall():
    state = read_json(ETC / "firewall.json", {})
    result = run(["nft", "-j", "list", "table", "inet", "argosbx_hy2"], check=False)
    if result.returncode or not state.get("guard_owned") or not state.get("guard_digest"):
        raise Error("私网防护规则缺失或尚未登记完整性，请运行 argosbx repair")
    if maintenance.firewall_digest(json.loads(result.stdout)) != state["guard_digest"]:
        raise Error("私网防护规则不完整，请运行 argosbx repair")


def ingress(external=False):
    owned = read_json(ETC / "firewall.json", {"ufw": []})
    active = shutil.which("ufw") and "Status: active" in run(["ufw", "status"], env={**os.environ, "LC_ALL": "C"}).stdout
    if active and not external:
        status = run(["ufw", "status", "numbered"], env={**os.environ, "LC_ALL": "C"}).stdout
        # Do not adopt an existing equivalent rule. Only remove our exact comments later.
        for proto in ("tcp", "udp"):
            comment = f"argosbx-hy2-{proto}"
            if comment not in status and not re.search(rf"443/{proto}\s+ALLOW", status):
                # Record intent first so a power loss cannot orphan an owned rule.
                if proto not in owned["ufw"]:
                    owned["ufw"].append(proto)
                save(ETC / "firewall.json", owned)
                run(["ufw", "allow", f"443/{proto}", "comment", comment])
    elif not external:
        ruleset = json.loads(run(["nft", "-j", "list", "ruleset"]).stdout)
        for item in ruleset.get("nftables", []):
            chain = item.get("chain", {})
            if chain.get("hook") == "input" and chain.get("table") != "argosbx_hy2":
                raise Error("检测到已有自定义入站防火墙；自行放行 TCP/UDP 443 后加 --firewall-external 重试")
    save(ETC / "firewall.json", owned)


def rollback_ingress():
    owned = read_json(ETC / "firewall.json", {"ufw": []})
    if shutil.which("ufw"):
        # Delete by numbered exact comment and descending index, avoiding shared rules.
        text = run(["ufw", "status", "numbered"], check=False, env={**os.environ, "LC_ALL": "C"}).stdout
        indices = []
        for line in text.splitlines():
            match = re.match(r"\[\s*(\d+)\].*# (argosbx-hy2-(tcp|udp))\s*$", line)
            if match and match[3] in owned["ufw"]:
                indices.append(int(match[1]))
        for index in sorted(indices, reverse=True):
            run(["ufw", "--force", "delete", str(index)])
    owned["ufw"] = []
    save(ETC / "firewall.json", owned)


def https_request(node, path, *, local=True, headers=None):
    import http.client
    connection = http.client.HTTPSConnection(node["domain"], 443, timeout=15,
                                              context=ssl.create_default_context())
    if local:
        # Preserve domain-based SNI and certificate verification while connecting locally.
        connection._create_connection = lambda address, timeout, source_address=None: socket.create_connection(("127.0.0.1", 443), timeout)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict((k.lower(), v) for k, v in response.getheaders()), response.read(2 * 1024 * 1024)
    finally:
        connection.close()


def probe_web(stage, public=False):
    node = read_json(stage / "node.json")
    run(["systemctl", "is-active", "--quiet", "argosbx-web.service"])
    path = "/s/" + node["token"]
    status, headers, body = https_request(node, path + "?format=clash", local=not public)
    if status != 200 or body != (stage / "exports/clash.yaml").read_bytes():
        raise Error("HTTPS 订阅内容自检失败")
    if "no-store" not in headers.get("cache-control", "") or "alt-svc" in headers:
        raise Error("订阅缓存策略或 HTTP/3 禁用检查失败")
    if public:
        return
    for fmt, filename in [("shadowrocket", "shadowrocket.txt"), ("singbox", "singbox.json")]:
        status, _, body = https_request(node, path + "?format=" + fmt)
        if status != 200 or body != (stage / "exports" / filename).read_bytes():
            raise Error("客户端导出自检失败")
    for ua, filename in [("clash-verge/v2.4.0", "clash.yaml"),
                         ("Shadowrocket/2.2.71", "shadowrocket.txt")]:
        status, headers, body = https_request(node, path, headers={"User-Agent": ua})
        if status != 200 or body != (stage / "exports" / filename).read_bytes():
            raise Error("单链接客户端自动适配检查失败")
        if "no-store" not in headers.get("cache-control", "") or "user-agent" not in headers.get("vary", "").lower():
            raise Error("单链接客户端缓存隔离检查失败")
    status, _, body = https_request(node, path)
    if status != 200 or b"Clash Verge Rev" not in body:
        raise Error("未知客户端格式页检查失败")
    status, _, _ = https_request(node, "/s/invalid-token?format=clash")
    if status != 404:
        raise Error("错误订阅 Token 未被拒绝")


def probe_hy2():
    run(["systemctl", "is-active", "--quiet", "argosbx-hy2.service"])
    # UDP 443 must be held by our user. A local HTTPS test cannot prove external UDP.
    uid = __import__("pwd").getpwnam("argosbx-hy2").pw_uid
    udp = run(["ss", "-H", "-lnupe", "sport = :443"]).stdout
    if "sing-box" not in udp or f"uid:{uid}" not in udp:
        raise Error("Hysteria2 UDP 443 未正确监听")


def probe(stage, public=False):
    probe_web(stage, public)
    probe_hy2()
    verify_firewall()


def proxy_probe(stage, target_ip=None):
    """Exercise HY2 auth/TLS/data and private-address rejection through a real client."""
    node = read_json(stage / "node.json")
    config = model.client_config(node)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config["inbounds"][0]["listen_port"] = port
    config["outbounds"][0]["server"] = "127.0.0.1"
    with tempfile.TemporaryDirectory(prefix="proxy-check-", dir=DATA) as temp:
        path = Path(temp) / "client.json"
        atomic(path, json.dumps(config))
        with subprocess.Popen([str(stage / "bin/sing-box"), "run", "-c", str(path)],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as client:
            try:
                for _ in range(30):
                    if client.poll() is not None:
                        raise Error("本地 HY2 测试客户端启动失败")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                proxy = f"socks5h://127.0.0.1:{port}"
                # A public address assigned to this VPS gives a complete local
                # HY2/TLS/data test without depending on an external website.
                targets = ([f'https://{node["domain"]}/'] if target_ip else
                           ["https://example.com/", "https://www.cloudflare.com/cdn-cgi/trace"])
                connect = ["--connect-to", f'{node["domain"]}:443:{target_ip}:443'] if target_ip else []
                for target in targets:
                    response = run(["curl", "--fail", "--silent", "--show-error", "--max-time", "12",
                                    "--proxy", proxy, "--noproxy", "", *connect, "--output", "/dev/null", target],
                                   check=False, timeout=15)
                    if response.returncode == 0:
                        break
                else:
                    raise Error("HY2 实际 HTTPS 转发失败；检查节点、DNS 与外网连通性")
                for url in ("http://127.0.0.1:443/", "http://169.254.169.254/", "http://10.0.0.1/"):
                    result = run(["curl", "--silent", "--max-time", "3", "--proxy", proxy,
                                  "--noproxy", "", "--output", "/dev/null", url], check=False, timeout=5)
                    # HY2 clients may optimistically acknowledge SOCKS before the
                    # remote denial; curl then reports empty/reset data (52/56).
                    # A timeout is not evidence of an enforced destination guard.
                    if result.returncode not in (52, 56, 97):
                        raise Error("代理私网目标拒绝检查失败")
            finally:
                client.terminate()
                try:
                    client.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    client.kill()
                    client.wait()


def wait_for_probe(stage, attempts=10):
    failure = None
    for _ in range(attempts):
        try:
            probe(stage)
            return
        except (Error, OSError) as exc:
            failure = exc
            time.sleep(1)
    raise failure or Error("服务就绪检查失败")


def activate(stage, state, public=False):
    old = str(current()) if CURRENT.is_symlink() else None
    validate_generation(stage)
    oldstate = read_json(STATE, {})
    save(JOURNAL, {"old": old, "new": str(stage), "old_state": oldstate, "time": now(),
                   "owner": maintenance.process_identity()})
    event("switching")
    linked(CURRENT, stage)
    try:
        run(["systemctl", "reset-failed", *SERVICES], check=False)
        run(["systemctl", "restart", *SERVICES])
        wait_for_probe(stage)
        # On a routed VPS this tests the entire local HY2 path without external
        # sites/DNS. NAT-only hosts retain the explicit external fallback probe.
        target_ip = local_public_ip()
        if target_ip:
            proxy_probe(stage, target_ip=target_ip)
        else:
            proxy_probe(stage)
        if public:
            try:
                probe(stage, public=True)
            except Exception:
                print("提示：本地检查通过，公网 HTTPS 自测未通过；请从外部检查 DNS、安全组与 NAT 回环。", file=sys.stderr)
        state.update({"current": str(stage), "previous": old, "updated_at": now()})
        save(STATE, state)
        remove_journal()
        event("ready", message="本地检查通过；公网 UDP 与客户端导入仍需外部验证")
    except BaseException:
        recover()
        raise
    try:
        cleanup_generations()
    except OSError:
        # Retention housekeeping must never undo an already committed transaction.
        pass


def cleanup_generations():
    state = read_json(STATE, {})
    keep = {state.get("current"), state.get("previous")}
    app_tx = read_json(Path("/opt/argosbx-hy2/app-transaction.json"), {})
    if app_tx.get("old_deployment"):
        keep.add(app_tx["old_deployment"])
    generations = DATA / "generations"
    candidates = sorted((p for p in generations.iterdir() if p.is_dir() and not p.is_symlink()),
                        key=lambda p: p.name, reverse=True)
    keep.update(str(p) for p in candidates[:3])
    for path in candidates:
        if str(path) not in keep and re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{8}", path.name):
            shutil.rmtree(path)


def tune():
    try:
        result = network.enable(ETC, run, save, atomic)
    except (Error, ValueError, OSError, subprocess.SubprocessError) as exc:
        print("提示：UDP 缓冲优化未应用，继续使用内核默认值：" + safe_error(exc), file=sys.stderr)
        return None
    print("UDP 缓冲上限已核实；宿主机 TCP 与队列策略保持由管理员管理。")
    return result


def undo_tune():
    network.undo(ETC, run)


def install(args):
    security_updates()
    if CURRENT.is_symlink():
        print("检测到已安装，保留订阅地址和节点密码。")
        repair()
        return subscription() if not os.environ.get("ARGOSBX_APP_DEPLOY") else None
    preflight()
    pending = read_json(DATA / "pending.json")
    if pending:
        node, email = pending["node"], pending.get("email", "")
        challenge = pending.get("challenge", "dns")
        print("继续此前未完成的安装，保留原凭据。")
    else:
        domain = args.domain or input("专用子域名（先设置灰云 A 记录）：").strip()
        email = args.email or ""
        if email and (not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or email.startswith("-")):
            raise Error("无效邮箱")
        name = args.name or input("节点名称 [我的 HY2]：").strip() or "我的 HY2"
        node = model.new_node(domain, name, obfs=getattr(args, "obfs", "none"))
        challenge = args.challenge
        save(DATA / "pending.json", {"node": node, "email": email, "challenge": challenge})
    tokenfile = ETC / "cloudflare.token"
    if challenge == "dns" and args.token_file:
        supplied = Path(args.token_file)
        stat = supplied.stat()
        if stat.st_mode & 0o077 or stat.st_uid != 0:
            raise Error("Token 文件必须由 root 持有且权限为 600/400")
        token = supplied.read_text().strip()
        atomic(tokenfile, token + "\n")
    elif challenge == "dns" and not tokenfile.exists():
        if not sys.stdin.isatty():
            raise Error("无人值守安装需要 --token-file，禁止将 Token 直接写入命令参数")
        token = getpass.getpass("Cloudflare API Token（隐藏输入，仅指定 Zone 的 DNS Edit + Zone Read）：").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,256}", token):
            raise Error("Token 格式异常")
        atomic(tokenfile, token + "\n")
    token = tokenfile.read_text().strip() if challenge == "dns" else None
    dns_preflight(node, token, args.server_ip)
    resolver_privacy()
    identities()
    ingress(args.firewall_external)
    stage = generation()
    lockdata = read_json(APP / "dependencies.lock.json")
    releases.validate_lock(lockdata)
    versions = {}
    event("downloading")
    for binary in ("sing-box", "caddy", "lego"):
        dep = lockdata["dependencies"][binary]
        spec = dep["assets"][args.arch]
        releases.install_binary(spec, binary, stage / "bin" / binary, DATA / "cache")
        versions[binary] = spec.get("version", dep["version"])
    save(stage / "versions.json", versions)
    cert, key = obtain_certificate(node, stage / "bin/lego", email, challenge, args.firewall_external)
    write_generation(stage, node, cert, key)
    tune()
    firewall()
    set_units()
    state = {"email": email, "challenge": challenge, "firewall_external": args.firewall_external, "schema": 1}
    activate(stage, state, public=True)
    run(["systemctl", "enable", "argosbx-network.service", "argosbx-firewall.service", *SERVICES,
         "argosbx-renew.timer", "argosbx-check-update.timer", "argosbx-health.timer"])
    run(["systemctl", "restart", "argosbx-renew.timer", "argosbx-check-update.timer", "argosbx-health.timer"])
    (DATA / "pending.json").unlink(missing_ok=True)
    if not os.environ.get("ARGOSBX_APP_DEPLOY"):
        subscription()


def subscription():
    node = read_json(current() / "node.json")
    url = model.subscription(node)
    print("私人订阅（Clash Verge Rev / Shadowrocket 使用同一个链接，请勿分享）：\n" + url)


def status():
    state = read_json(STATE, {})
    print("最近操作：" + json.dumps(read_json(DATA / "last-operation.json", {}), ensure_ascii=False))
    if not CURRENT.is_symlink():
        print("尚未完成安装；可重复执行安装命令继续。")
        return
    stage = current()
    print("版本：" + json.dumps(read_json(stage / "versions.json"), ensure_ascii=False))
    for service in SERVICES:
        data = run(["systemctl", "show", service, "-p", "ActiveState", "-p", "NRestarts", "-p", "Result"], check=False).stdout.strip()
        print(service + ": " + data.replace("\n", "; "))
    print("证书：" + run(["openssl", "x509", "-in", stage / "tls/fullchain.pem", "-noout", "-enddate"], check=False).stdout.strip())
    print(f"可用空间：{shutil.disk_usage(DATA).free // 1024**2} MiB")
    print("更新检查：" + json.dumps(read_json(DATA / "update.json", {}), ensure_ascii=False))
    print("健康检查：" + json.dumps(read_json(DATA / "health.json", {}), ensure_ascii=False))
    try:
        print("系统网络：" + json.dumps(network.check(run), ensure_ascii=False))
    except ValueError as exc:
        print("系统网络：" + str(exc))
    if state.get("blocked_version"):
        print("已暂停重复升级的版本：" + state["blocked_version"])


def check_update():
    lockdata = read_json(APP / "dependencies.lock.json")
    releases.validate_lock(lockdata)
    spec = releases.latest_sing_box(root_platform(), lockdata)
    state = read_json(STATE, {})
    installed = read_json(current() / "versions.json")["sing-box"]
    if releases.version_tuple(spec["version"]) < releases.version_tuple(installed):
        raise Error("官方 latest 版本低于已安装版本，拒绝自动降级")
    save(DATA / "update.json", {"checked_at": now(), "installed": installed,
                               "available": spec["version"], "blocked": state.get("blocked_version")})
    print(f'已安装 {installed}；官方稳定版 {spec["version"]}。使用 argosbx update 在维护时段切换。')
    return spec


def update(args):
    spec = check_update()
    old = current()
    versions = read_json(old / "versions.json")
    if versions["sing-box"] == spec["version"]:
        print("已是最新稳定版。")
        return
    state = read_json(STATE)
    if state.get("blocked_version") == spec["version"] and not args.retry:
        raise Error("此版本曾失败并已暂停；检查原因后使用 update --retry")
    preflight()
    stage = copy_generation(old)
    releases.install_binary(spec, "sing-box", stage / "bin/sing-box", DATA / "cache")
    versions["sing-box"] = spec["version"]
    save(stage / "versions.json", versions)
    try:
        activate(stage, state)
    except Exception:
        state = read_json(STATE)
        state["blocked_version"] = spec["version"]
        save(STATE, state)
        raise
    state = read_json(STATE)
    state.pop("blocked_version", None)
    save(STATE, state)
    print("内核更新与本地订阅检查通过。请在客户端运行外部 UDP 验证。")


def rollback():
    state = read_json(STATE, {})
    previous = state.get("previous")
    if not previous:
        raise Error("没有可回滚版本")
    stage = Path(previous)
    if stage.parent != DATA / "generations" or not stage.is_dir():
        raise Error("回滚目录异常")
    # Never restore revoked credentials through an ordinary binary rollback.
    oldnode = read_json(current() / "node.json")
    newnode = read_json(stage / "node.json")
    if oldnode != newnode:
        raise Error("上一版本含旧凭据；为避免恢复已撤销密码，不能普通回滚，请使用 repair 或再次 rotate")
    activate(stage, state)
    print("已回滚，并通过本地检查。")


def renew():
    old = current()
    node = read_json(old / "node.json")
    state = read_json(STATE)
    cert, key = obtain_certificate(node, old / "bin/lego", state["email"],
                                   state.get("challenge", "dns"), state.get("firewall_external", False))
    if cert.read_bytes() == (old / "tls/fullchain.pem").read_bytes():
        event("renew-not-due")
        return
    stage = copy_generation(old)
    write_generation(stage, node, cert, key, read_json(old / "ua.json", []))
    activate(stage, state)
    print("证书已校验并部署，服务完成短暂重启。")


def repair():
    preflight()
    security_updates()
    old = current()
    state = read_json(STATE)
    stage = copy_generation(old)
    write_generation(stage, read_json(old / "node.json"), old / "tls/fullchain.pem", old / "tls/key.pem", read_json(old / "ua.json", []))
    firewall()
    tune()
    resolver_privacy()
    ingress(state.get("firewall_external", False))
    set_units()
    activate(stage, state)
    run(["systemctl", "enable", "argosbx-network.service", "argosbx-firewall.service", *SERVICES,
         "argosbx-renew.timer", "argosbx-check-update.timer", "argosbx-health.timer"])
    run(["systemctl", "restart", "argosbx-renew.timer", "argosbx-check-update.timer", "argosbx-health.timer"])
    print("已重新生成配置并修复服务，凭据保持不变。")


def rotate(kind):
    old = current()
    node = read_json(old / "node.json")
    if kind in ("token", "both"):
        node["token"] = secrets.token_urlsafe(32)
    if kind in ("password", "both"):
        node["password"] = secrets.token_urlsafe(32)
        if node.get("obfs") == "salamander":
            node["obfs_password"] = secrets.token_urlsafe(32)
    stage = copy_generation(old)
    write_generation(stage, node, old / "tls/fullchain.pem", old / "tls/key.pem", read_json(old / "ua.json", []))
    activate(stage, read_json(STATE))
    print("凭据已轮换。已下载的密码只有在轮换节点密码后才会失效。")
    subscription()


def set_obfuscation(mode):
    old = current()
    node = read_json(old / "node.json")
    if node.get("obfs", "none") == mode:
        print("当前已是所选模式。")
        return subscription()
    if mode == "salamander":
        node.update(obfs="salamander", obfs_password=secrets.token_urlsafe(32))
    elif mode == "none":
        node.pop("obfs", None)
        node.pop("obfs_password", None)
    else:
        raise Error("未知传输模式")
    model.validate_node(node)
    stage = copy_generation(old)
    write_generation(stage, node, old / "tls/fullchain.pem", old / "tls/key.pem", read_json(old / "ua.json", []))
    activate(stage, read_json(STATE))
    print("模式已切换；请在两个客户端刷新原订阅。此模式不保证绕过所有识别或封锁。")
    subscription()


def add_ua(args):
    old = current()
    rules = read_json(old / "ua.json", [])
    rules.append({"pattern": args.pattern, "format": args.format, "verified_client": args.client_version})
    node = read_json(old / "node.json")
    model.caddy_config(node, ua_rules=rules)  # validate before creating the candidate
    stage = copy_generation(old)
    write_generation(stage, node, old / "tls/fullchain.pem", old / "tls/key.pem", rules)
    activate(stage, read_json(STATE))
    print("已登记实测 UA；显式 format 参数始终优先。")


def local_public_ip():
    interfaces = json.loads(run(["ip", "-j", "-4", "address", "show"]).stdout)
    for interface in interfaces:
        for address in interface.get("addr_info", []):
            ip = ipaddress.IPv4Address(address["local"])
            if ip.is_global:
                return str(ip)
    return None


def local_health_issues(stage):
    issues = {}
    for name, check in (("web", lambda: probe_web(stage)), ("hy2", probe_hy2),
                        ("firewall", verify_firewall)):
        try:
            check()
        except Exception as exc:
            issues[name] = safe_error(exc)
    return issues


def recover_runtime(stage, issues):
    # Only owned runtime state. Never download code, rotate credentials, issue a
    # certificate, reboot the machine or change unrelated firewall/SSH policy.
    if "firewall" in issues:
        firewall()
    # Infrastructure repairs may already have fixed the reported symptoms.
    # Reclassify before restarting any process so healthy streams stay alive.
    remaining = local_health_issues(stage)
    if "firewall" in remaining:
        raise Error("基础保护尚未恢复，停止重启服务")
    targets = [name for key, name in (("web", "argosbx-web.service"), ("hy2", "argosbx-hy2.service")) if key in remaining]
    if targets:
        validate_generation(stage)
        run(["systemctl", "reset-failed", *targets], check=False)
        run(["systemctl", "restart", *targets])
        wait_for_probe(stage)
    target_ip = local_public_ip()
    if target_ip:
        try:
            proxy_probe(stage, target_ip=target_ip)
        except Exception:
            if "argosbx-hy2.service" in targets:
                raise
            validate_generation(stage)
            run(["systemctl", "reset-failed", "argosbx-hy2.service"], check=False)
            run(["systemctl", "restart", "argosbx-hy2.service"])
            targets.append("argosbx-hy2.service")
            wait_for_probe(stage)
            proxy_probe(stage, target_ip=target_ip)
    wait_for_probe(stage)
    verify_firewall()
    return {"restarted_services": targets, "firewall_rebuilt": "firewall" in issues,
            "network_reapplied": False}


def health(heal=False):
    stage, timestamp = current(), time.time()
    history = read_json(DATA / "self-heal.json", {})
    issues, warnings = local_health_issues(stage), []
    target_ip = None
    if heal:
        target_ip = local_public_ip()
        if target_ip and not issues and (history.get("last_data_ok") is False or
                timestamp - history.get("last_data_check", 0) >= 1800):
            history["last_data_check"] = timestamp
            try:
                proxy_probe(stage, target_ip=target_ip)
                history["last_data_ok"] = True
            except Exception as exc:
                history["last_data_ok"] = False
                issues["data"] = safe_error(exc)
    recovery = "not_needed"
    actions = {}
    if heal:
        history["consecutive_failures"] = history.get("consecutive_failures", 0) + 1 if issues else 0
        allowed, attempts = maintenance.recovery_allowed(history, timestamp)
        history["attempts"] = attempts
        if issues and allowed:
            attempts.append(timestamp)
            save(DATA / "self-heal.json", history)  # Persist the budget before acting.
            try:
                actions = recover_runtime(stage, issues)
                if target_ip:
                    proxy_probe(stage, target_ip=target_ip)
                    history.update(last_data_check=timestamp, last_data_ok=True)
                issues = local_health_issues(stage)
                if issues:
                    raise Error("自动恢复后的本地核实失败")
                history["consecutive_failures"] = 0
                recovery = "recovered"
                event("self-healed", message="已恢复本项目运行状态，凭据保持不变")
            except Exception as exc:
                issues["recovery"] = safe_error(exc)
                recovery = "failed"
        elif issues:
            recovery = "waiting_or_rate_limited"
        save(DATA / "self-heal.json", history)
    try:
        diagnostics = {}
        run(["openssl", "x509", "-in", stage / "tls/fullchain.pem", "-checkend", str(14 * 86400), "-noout"])
    except Exception:
        warnings.append("证书有效期不足 14 天，请检查自动续期结果")
    try:
        if shutil.disk_usage(DATA).free < 512 * 1024**2:
            warnings.append("空闲磁盘低于 512 MiB")
        for service in SERVICES:
            output = run(["systemctl", "show", service, "-p", "NRestarts", "-p", "Result", "-p", "ActiveState",
                          "-p", "ExecMainStatus", "-p", "MemoryCurrent", "-p", "MemoryMax"]).stdout
            props = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
            diagnostics[service] = props
            if int(props.get("NRestarts", "0")) >= 5:
                warnings.append(service + " 重启次数较多")
        if heal and not issues:
            maintenance.cleanup_cache(DATA / "cache", timestamp)
            cleanup_generations()
    except Exception as exc:
        warnings.append(safe_error(exc))
    problems = list(issues.values()) + warnings
    save(DATA / "health.json", {"time": now(), "ok": not problems, "problems": problems,
                               "recovery": recovery, "auto_recovery": heal,
                               "recovery_actions": actions, "services": diagnostics,
                               "local_hy2_data": history.get("last_data_ok") if heal else "未执行",
                               "local_data_supported": bool(target_ip) if heal else None,
                               "external_udp": "未自动验证"})
    if problems:
        raise Error("健康检查未通过：" + "; ".join(problems))
    print("本地健康检查通过；外部 UDP 和客户端状态需单独验证。")


def uninstall(purge):
    # Only exact project-owned files/services; no shared package uninstallation.
    unit_names = []
    for name in system.units():
        path = UNIT_DIR / name
        if path.is_symlink() or (path.exists() and not path.read_text().startswith("# Managed by ArgoSBX-HY2\n")):
            raise Error(f"同名服务 {name} 已不属于本项目，停止卸载以保留它")
        if path.exists():
            unit_names.append(name)
    for name in unit_names:
        run(["systemctl", "disable", "--now", name], check=False)
    rollback_ingress()
    cleanup_http_challenge()
    undo_tune()
    resolver_privacy(remove=True)
    policy = APT_POLICY
    if policy.exists() and policy.read_text().startswith("// Managed by ArgoSBX-HY2\n"):
        policy.unlink()
    if read_json(ETC / "firewall.json", {}).get("guard_owned"):
        run(["nft", "delete", "table", "inet", "argosbx_hy2"], check=False)
    for name in unit_names:
        (UNIT_DIR / name).unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"])
    CURRENT.unlink(missing_ok=True)
    sync_directory(CURRENT.parent)
    if purge:
        for path in (ETC, DATA):
            if path.is_symlink():
                raise Error("敏感目录为符号链接，拒绝递归删除")
            shutil.rmtree(path)
        print("本项目配置、Cloudflare 凭据及敏感历史版本已清除。")
    else:
        print("服务与规则已移除；敏感备份仍在 /var/lib/argosbx-hy2，API Token 仍在 /etc/argosbx-hy2。")
        print("重新安装会从备份恢复原凭据。彻底清除请运行 argosbx uninstall --purge。")
    print("管理程序保留以便恢复/清除；未移除系统软件包、用户或任何其他站点。")


def menu():
    choices = {"1": subscription, "2": status, "3": repair, "4": lambda: update(argparse.Namespace(retry=False)),
               "5": rollback, "6": renew, "7": lambda: rotate("both"), "8": health,
               "9": lambda: uninstall(False)}
    print("\nArgoSBX-HY2\n1 订阅链接\n2 状态\n3 修复/重启\n4 更新内核\n5 回滚\n6 续期证书\n7 轮换全部凭据\n8 健康检查\n9 卸载（保留备份）\n0 退出")
    choice = input("选择：").strip()
    if choice in choices:
        choices[choice]()


def safe_error(exc):
    if isinstance(exc, (Error, releases.ReleaseError, ValueError)):
        # Our errors are fixed text, but sanitize any accidental secrets defensively.
        message = str(exc)
    else:
        message = type(exc).__name__ + "；请检查网络、磁盘和系统服务后重试"
    return re.sub(r"[A-Za-z0-9_-]{20,}", "[REDACTED]", message)[:500]


def main(argv=None):
    parser = argparse.ArgumentParser(description="ArgoSBX-HY2 管理命令")
    sub = parser.add_subparsers(dest="command")
    ins = sub.add_parser("install")
    ins.add_argument("--domain")
    ins.add_argument("--email", default="", help="可选联系邮箱；默认不设置，也不询问")
    ins.add_argument("--name")
    ins.add_argument("--obfs", choices=["none", "salamander"], default="none",
                     help="默认标准 HTTP/3；确认网络限制 QUIC 时可用 Salamander，非通用抗识别保证")
    ins.add_argument("--token-file")
    ins.add_argument("--challenge", choices=["http", "dns"], default="http")
    ins.add_argument("--server-ip", help="NAT VPS 的显式公网 IPv4；默认与本机接口地址比对")
    ins.add_argument("--firewall-external", action="store_true")
    upd = sub.add_parser("update")
    upd.add_argument("--retry", action="store_true")
    for name in ("subscription", "status", "repair", "rollback", "renew", "check-update", "firewall", "network", "recover"):
        sub.add_parser(name)
    health_parser = sub.add_parser("health")
    health_parser.add_argument("--heal", action="store_true", help="连续失败后限次恢复本项目服务、规则与网络参数")
    rot = sub.add_parser("rotate")
    rot.add_argument("kind", choices=["token", "password", "both"], default="both", nargs="?")
    transport = sub.add_parser("obfs", help="切换传输模式，保留订阅地址；客户端需要刷新")
    transport.add_argument("mode", choices=["none", "salamander"])
    ua = sub.add_parser("add-ua")
    ua.add_argument("--pattern", required=True)
    ua.add_argument("--format", choices=["clash", "shadowrocket", "singbox"], required=True)
    ua.add_argument("--client-version", required=True, help="实际验证的客户端版本与平台")
    un = sub.add_parser("uninstall")
    un.add_argument("--purge", action="store_true")
    args = parser.parse_args(argv)
    try:
        args.arch = root_platform()
        os.umask(0o077)
        # systemd starts the guard while activation holds the lifecycle lock.
        # Its own lock serializes nft changes without deadlocking activation.
        with lock("argosbx-" + args.command if args.command in ("firewall", "network") else "argosbx-hy2"):
            for path in (ETC, DATA):
                if path.is_symlink():
                    raise Error("本项目目录为符号链接，停止修改")
                if path.exists() and not (path / ".argosbx-owned").exists():
                    raise Error("发现未知同名目录，拒绝覆盖；请使用发布包安装器")
                path.mkdir(mode=0o755 if path == ETC else 0o711, exist_ok=True)
                path.chmod(0o755 if path == ETC else 0o711)
                (path / ".argosbx-owned").touch(mode=0o600, exist_ok=True)
            for path in (DATA / "generations", DATA / "public"):
                if path.is_symlink():
                    raise Error("本项目数据子目录不能是符号链接")
            (DATA / "generations").mkdir(mode=0o711, exist_ok=True)
            (DATA / "generations").chmod(0o711)
            (DATA / "public").mkdir(mode=0o755, exist_ok=True)
            (DATA / "public").chmod(0o755)
            ensure_public_page()
            if args.command == "recover":
                recover(restart=False)
                return 0
            if args.command in ("firewall", "network"):
                recover(restart=False)
            else:
                recover()
                cleanup_http_challenge()
            command = args.command
            if command == "install":
                # Restore preserved installation after an uninstall, keeping credentials.
                state = read_json(STATE, {})
                if not CURRENT.is_symlink() and state.get("current") and Path(state["current"]).is_dir():
                    linked(CURRENT, state["current"])
                install(args)
            elif command == "update":
                update(args)
            elif command == "rotate":
                rotate(args.kind)
            elif command == "obfs":
                set_obfuscation(args.mode)
            elif command == "add-ua":
                add_ua(args)
            elif command == "uninstall":
                uninstall(args.purge)
            elif command == "health":
                health(args.heal)
            elif command:
                {"subscription": subscription, "status": status, "repair": repair, "rollback": rollback,
                 "renew": renew, "check-update": check_update, "health": health, "firewall": firewall, "network": tune}[command]()
            else:
                menu()
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        message = safe_error(exc)
        print("未完成：" + message, file=sys.stderr)
        if DATA.exists():
            try:
                event("failed", error=message)
                if not CURRENT.is_symlink() and args.command == "install":
                    rollback_ingress()
                    undo_tune()
            except Exception:
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
