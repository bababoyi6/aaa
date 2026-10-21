"""Conservative UDP buffer limits; legacy TCP/qdisc records remain undoable."""
import json
from pathlib import Path
import re

MARKER = "# Managed by ArgoSBX-HY2\n"
SYSCTL = Path("/etc/sysctl.d/90-argosbx-hy2.conf")
MODULES = Path("/etc/modules-load.d/argosbx-hy2.conf")
MEMINFO = Path("/proc/meminfo")
KEYS = ("net.core.rmem_max", "net.core.wmem_max",
        "net.core.default_qdisc", "net.ipv4.tcp_congestion_control")


def load(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def interfaces(run):
    devices = set()
    for family in ("-4", "-6"):
        routes = json.loads(run(["ip", "-j", family, "route", "show", "default"]).stdout)
        for route in routes:
            for item in [route, *route.get("nexthops", [])]:
                if item.get("dev"):
                    devices.add(item["dev"])
    if not devices or any(not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", d) or d == "lo" for d in devices):
        raise ValueError("无法安全确定默认出口网卡，BBR+fq 未完成")
    return sorted(devices)


def queues(run, device):
    data = json.loads(run(["tc", "-j", "qdisc", "show", "dev", device]).stdout)
    roots = [q for q in data if q.get("root")]
    if len(roots) != 1:
        raise ValueError("出口网卡队列结构不明确，停止修改")
    root = roots[0]
    if root["kind"] == "mq":
        # Keep the multiqueue root and change only its existing leaves.
        major = root["handle"].split(":")[0] or "0"
        leaves = [q for q in data if "parent" in q and (q["parent"].split(":")[0] or "0") == major]
        if not leaves or len(data) != len(leaves) + 1:
            raise ValueError("出口网卡含自定义队列层级，停止修改")
        return leaves
    if len(data) != 1:
        raise ValueError("出口网卡含自定义队列层级，停止修改")
    return roots


def restore_options(queue):
    kind, opts = queue["kind"], queue.get("options", {})
    if kind == "fq":
        return []  # An existing fq is never changed.
    if kind == "pfifo_fast":
        return []
    if kind != "fq_codel" or set(opts) - {"limit", "flows", "quantum", "target", "interval", "memory_limit", "ecn", "drop_batch"}:
        raise ValueError("发现自定义或不支持的出口队列；未覆盖现有流控，BBR+fq 未完成")
    result = []
    for key, value in opts.items():
        if key == "ecn":
            result.append("ecn" if value else "noecn")
        else:
            result += [key, str(value) + ("us" if key in ("target", "interval") else "")]
    return result


def position(queue):
    return ["root"] if queue.get("root") else ["parent", queue["parent"]]


def replace(run, device, queue, kind, options=()):
    handle = queue.get("handle", "0:")
    args = ["tc", "qdisc", "replace", "dev", device, *position(queue)]
    # Linux cannot change a qdisc's kind while reusing its existing handle.
    # Let tc allocate a new handle when switching kind; restore the old handle
    # only when recreating the saved original queue.
    if handle != "0:" and kind == queue["kind"]:
        args += ["handle", handle]
    run([*args, kind, *options])


def check(run):
    values = {k: run(["sysctl", "-n", k]).stdout.strip() for k in KEYS[:2]}
    return {"rmem_max": values[KEYS[0]], "wmem_max": values[KEYS[1]],
            "host_tcp_qdisc": "由宿主机管理员管理，不作为 HY2 健康条件"}


def enable(etc, run, save, atomic):
    owned = etc / "sysctl.json"
    settings = load(owned, {})
    for path in (SYSCTL, MODULES):
        if path.is_symlink() or (path.exists() and not path.read_text().startswith(MARKER)):
            raise ValueError("发现未知同名网络配置文件，拒绝覆盖")
    memory_kb = int(next(line.split()[1] for line in MEMINFO.read_text().splitlines() if line.startswith("MemTotal:")))
    target = 4 * 1024**2 if memory_kb < 1024**2 else 8 * 1024**2
    for key in KEYS[:2]:
        actual = run(["sysctl", "-n", key]).stdout.strip()
        after = max(int(actual), target)
        if key not in settings:
            settings[key] = {"before": actual, "after": after}
        else:
            settings[key]["after"] = after  # Preserve first-install originals.
    # Preserve old TCP/qdisc undo records without reapplying those policies.
    save(owned, settings)
    try:
        atomic(SYSCTL, MARKER + "".join(f'{k} = {settings[k]["after"]}\n' for k in KEYS[:2]), 0o644)
        run(["sysctl", "-p", SYSCTL])
        if MODULES.exists():
            MODULES.unlink()
        return check(run)
    except BaseException:
        # Undo only the buffers touched here; never restore legacy queues during
        # a failed optional tune. Keep the journal if sysctl restoration fails.
        for key in KEYS[:2]:
            value = settings[key]
            if run(["sysctl", "-n", key], check=False).stdout.strip() == str(value["after"]):
                run(["sysctl", "-w", f'{key}={value["before"]}'])
        raise


def undo(etc, run, pending=False):
    qstate, owned = etc / "qdisc.json", etc / "sysctl.json"
    for record in reversed(load(qstate, [])):
        device, before = record["device"], record["before"]
        try:
            actual = next((q for q in queues(run, device) if position(q) == position(before)), None)
        except (ValueError, RuntimeError):
            # A removed/changed interface is no longer ours to modify.
            continue
        matches = actual and actual.get("options", {}) == record.get("after")
        incomplete = "after" not in record
        if actual and actual["kind"] == "fq" and incomplete and not pending:
            raise ValueError("队列恢复记录未提交，请先运行 argosbx repair 后重试")
        if actual and actual["kind"] == "fq" and (matches or (incomplete and pending)):
            # A restore failure must retain the undo record for a later retry.
            replace(run, device, before, before["kind"], restore_options(before))
    for key, value in load(owned, {}).items():
        if key not in KEYS:
            raise ValueError("网络恢复记录包含未知设置，停止恢复")
        if run(["sysctl", "-n", key], check=False).stdout.strip() == str(value["after"]):
            run(["sysctl", "-w", f'{key}={value["before"]}'])
    for path in (SYSCTL, MODULES):
        if not path.is_symlink() and path.exists() and path.read_text().startswith(MARKER):
            path.unlink()
    qstate.unlink(missing_ok=True)
    owned.unlink(missing_ok=True)
