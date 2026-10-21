"""Independent, immutable application transaction runner. No candidate imports."""
import ast
import base64
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time


class Layout:
    def __init__(self, root=Path('/')):
        self.root = Path(root)
        self.base = self.root / 'opt/argosbx-hy2'
        self.etc = self.root / 'etc/argosbx-hy2'
        self.data = self.root / 'var/lib/argosbx-hy2'
        self.units = self.root / 'etc/systemd/system'
        self.launcher = self.root / 'usr/local/bin/argosbx'
        self.journal = self.base / 'app-transaction.json'
        self.link = self.base / 'current'
        self.managed = [self.root / p for p in (
            'etc/sysctl.d/90-argosbx-hy2.conf', 'etc/modules-load.d/argosbx-hy2.conf',
            'etc/systemd/resolved.conf.d/argosbx-hy2.conf',
            'etc/apt/apt.conf.d/52argosbx-hy2-auto-upgrades')]


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def remove_journal(layout):
    layout.journal.unlink(missing_ok=True)
    sync_directory(layout.journal.parent)


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.app-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(path, value):
    atomic(path, json.dumps(value).encode())


def link(path, target):
    if path.exists() and not path.is_symlink():
        raise RuntimeError('发现未知管理程序指向，停止修改')
    if target is None:
        path.unlink(missing_ok=True)
    else:
        temp = path.with_name(path.name + '.app-next')
        temp.unlink(missing_ok=True)
        temp.symlink_to(target)
        os.replace(temp, path)
    sync_directory(path.parent)


def process_identity(pid=None):
    pid = pid or os.getpid()
    try:
        start = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
        return {'pid': pid, 'start': start, 'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except (OSError, IndexError):
        return None


def owner_alive(tx):
    owner = tx.get('owner')
    return bool(owner and process_identity(owner['pid']) == owner)


@contextlib.contextmanager
def locked(path, seconds=60, shared=False):
    import fcntl
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, 'r+') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                info.st_mode & 0o022 or info.st_nlink != 1):
            raise RuntimeError('应用锁类型、归属或权限异常')
        until = time.monotonic() + seconds
        while True:
            try:
                fcntl.flock(stream, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise TimeoutError('已有管理任务正在执行，请稍后重试') from None
                time.sleep(.1)
        yield stream


def command(args, check=True, timeout=120):
    result = subprocess.run(list(map(str, args)), capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError('管理程序恢复步骤失败：' + Path(str(args[0])).name)
    return result


def runtime_ready(app):
    # Advisory warnings (e.g. a certificate nearing renewal) must not block the
    # old manager from recovering or prevent the user running its renew command.
    script = ('import sys; sys.path.insert(0,sys.argv[1]); '
              'from argosbx import manager as m; m.wait_for_probe(m.current()); '
              'm.verify_firewall(); '
              'ip=m.local_public_ip() if hasattr(m,"local_public_ip") else None; '
              'm.proxy_probe(m.current(),target_ip=ip) if ip else None')
    command([sys.executable, '-I', '-B', '-c', script, app])


def own_units(layout):
    result = []
    for path in sorted(layout.units.glob('argosbx-*.*')):
        if path.suffix not in ('.service', '.timer'):
            continue
        if path.is_symlink() or not path.read_bytes().startswith(b'# Managed by ArgoSBX-HY2\n'):
            raise RuntimeError('存在未知同名服务，停止修改')
        result.append(path)
    return result


DATA_FILES = ('state.json', 'transaction.json', 'pending.json', 'health.json',
              'self-heal.json', 'last-operation.json', 'update.json')


def state_paths(layout):
    paths = [layout.launcher, *layout.managed, *own_units(layout)]
    if layout.etc.exists():
        for path in layout.etc.iterdir():
            if path.name == 'current':
                continue
            if path.is_symlink() or not path.is_file():
                raise RuntimeError('项目配置目录存在未知类型，停止升级')
            paths.append(path)
    paths += [layout.data / name for name in DATA_FILES]
    return sorted(set(paths))


def snapshot(layout):
    records = {}
    for path in state_paths(layout):
        if path.is_symlink():
            raise RuntimeError('待备份配置为符号链接，停止升级')
        if path.exists():
            records[str(path)] = {'data': base64.b64encode(path.read_bytes()).decode(),
                                  'mode': path.stat().st_mode & 0o777}
        else:
            records[str(path)] = None
    units = {}
    for path in own_units(layout):
        units[path.name] = {
            'enabled': command(['systemctl', 'is-enabled', path.name], False).stdout.decode().strip(),
            'active': command(['systemctl', 'is-active', '--quiet', path.name], False).returncode == 0}
    return {'schema': 1, 'owner': process_identity(), 'phase': 'prepared',
            'old_app': str(layout.link.resolve()) if layout.link.is_symlink() else None,
            'old_deployment': os.readlink(layout.etc / 'current') if (layout.etc / 'current').is_symlink() else None,
            'files': records, 'units': units}


def restore_files(layout, tx):
    old = Path(tx['old_app'])
    if old.parent != layout.base or not old.is_dir():
        raise RuntimeError('旧管理程序不可恢复，保留恢复记录')
    # Validate all paths before writing/removing any file from the journal.
    allowed = set(state_paths(layout))
    for value in tx['files']:
        path = Path(value)
        if (path not in allowed and not (path.parent == layout.units and path.name in tx['units'])
                and not (path.parent == layout.etc and path.name != 'current')):
            raise RuntimeError('管理恢复记录路径异常')
    for path in allowed | {Path(x) for x in tx['files']}:
        if path == layout.launcher:
            continue  # Keep the recovery-capable launcher until recovery commits.
        record = tx['files'].get(str(path))
        if record is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, base64.b64decode(record['data'], validate=True), record['mode'])
    link(layout.link, old)
    link(layout.etc / 'current', tx['old_deployment'])
    command(['systemctl', 'daemon-reload'])


def recover(layout, tx, context=''):
    worker = tx.get('worker')
    if worker and process_identity(worker['pid']) == worker and os.getpgid(worker['pid']) == worker['pid']:
        os.killpg(worker['pid'], signal.SIGTERM)
        until = time.monotonic() + 5
        while process_identity(worker['pid']) == worker and time.monotonic() < until:
            time.sleep(.1)
        if process_identity(worker['pid']) == worker:
            os.killpg(worker['pid'], signal.SIGKILL)
    if not tx.get('old_app'):
        # First installation has no previous manager. Keep pending credentials,
        # ACME account and the staged app so the installer can resume safely.
        remove_journal(layout)
        return
    tx['owner'] = process_identity()
    names = sorted(set([p.name for p in own_units(layout)] + tx.get('touched_units', [])))
    tx['touched_units'] = names
    save(layout.journal, tx)
    deferred = context in ('network', 'firewall', 'recover')
    with locked(layout.root / 'run/lock/argosbx-hy2.lock'):
        if not deferred:
            stopping = [name for name in names if name != f'argosbx-{context}.service']
            if stopping:
                command(['systemctl', 'stop', *stopping])
            if names:
                command(['systemctl', 'disable', *names], False)
        if tx['phase'] != 'runtime_pending' or not deferred:
            restore_files(layout, tx)
        for path in layout.managed[:2]:
            if path.exists() and path.parent.name == 'sysctl.d':
                command(['sysctl', '-p', path])
    if deferred:
        # Never stop the network/firewall unit that is invoking us during boot.
        tx.update(phase='runtime_pending', owner=None)
        save(layout.journal, tx)
        return
    entry = Path(tx['old_app']) / 'entry.py'
    if tx['old_deployment']:
        command([sys.executable, '-I', '-B', entry, 'network'])
        command([sys.executable, '-I', '-B', entry, 'firewall'])
    if layout.managed[2].exists():
        if command(['systemctl', 'is-active', '--quiet', 'systemd-resolved.service'], False).returncode == 0:
            command(['systemctl', 'restart', 'systemd-resolved.service'])
    enabled = [name for name, state in tx['units'].items() if state['enabled'] in (True, 'enabled')]
    if enabled:
        command(['systemctl', 'enable', *enabled])
    runtime = [name for name, state in tx['units'].items() if state['enabled'] == 'enabled-runtime']
    if runtime:
        command(['systemctl', 'enable', '--runtime', *runtime])
    active = [name for name, state in tx['units'].items() if state['active'] and
              (name.endswith('.timer') or name in ('argosbx-hy2.service', 'argosbx-web.service',
                                                  'argosbx-network.service', 'argosbx-firewall.service'))]
    if active:
        command(['systemctl', 'reset-failed', *active], False)
        command(['systemctl', 'start', *active])
    if all(tx['units'].get(name, {}).get('active') for name in ('argosbx-hy2.service', 'argosbx-web.service')):
        runtime_ready(Path(tx['old_app']))
    original = tx['files'].get(str(layout.launcher))
    if original:
        atomic(layout.launcher, base64.b64decode(original['data']), original['mode'])
    remove_journal(layout)


def start_candidate(layout, app, args, tx):
    # A child must not import/execute candidate code until its process identity
    # is durable. If the parent dies in the Popen/save window, pipe EOF makes
    # the trusted wrapper exit without changing any project state.
    gate = ('import os,sys\nfd=int(sys.argv[1])\nready=os.read(fd,1)\nos.close(fd)\n'
            'if ready != b"1": sys.exit(1)\n'
            'os.execv(sys.executable,[sys.executable,"-I","-B",sys.argv[2],"install",*sys.argv[3:]])\n')
    reader_fd, writer_fd = os.pipe()
    worker = None
    try:
        with os.fdopen(reader_fd, 'rb') as reader, os.fdopen(writer_fd, 'wb', buffering=0) as writer:
            worker = subprocess.Popen([sys.executable, '-I', '-B', '-c', gate, str(reader.fileno()),
                                       str(app / 'entry.py'), *args], pass_fds=(reader.fileno(),),
                                      env=dict(os.environ, ARGOSBX_APP_DEPLOY='1'), start_new_session=True)
            reader.close()
            tx['worker'] = process_identity(worker.pid)
            if tx['worker'] is None:
                raise RuntimeError('候选进程身份无法登记，停止切换')
            save(layout.journal, tx)
            writer.write(b'1')
        return worker
    except BaseException:
        if worker is not None:
            worker.wait(timeout=5)
        raise


def deploy(layout, app, args):
    app = Path(app).resolve()
    if app.parent != layout.base or not re.fullmatch(r'app-\d+\.\d+\.\d+-[0-9a-f]{16}', app.name):
        raise RuntimeError('候选管理程序路径异常')
    if layout.link.exists() and not layout.link.is_symlink():
        raise RuntimeError('当前管理程序指向不是符号链接')
    if layout.link.is_symlink() and (layout.link.resolve().parent != layout.base or not layout.link.resolve().is_dir()):
        raise RuntimeError('原管理程序目录不属于本项目，停止升级')
    paths = list(app.rglob('*'))
    if any(path.is_symlink() for path in paths):
        raise RuntimeError('候选应用存在符号链接')
    for path in [app / 'entry.py', *(app / 'argosbx').glob('*.py')]:
        ast.parse(path.read_text())
    command([sys.executable, '-I', '-B', app / 'entry.py', '--help'])
    for path in paths:
        if path.is_file():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
    for path in sorted((p for p in paths if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        sync_directory(path)
    sync_directory(app)
    sync_directory(app.parent)
    with locked(layout.base / 'app.lock'):
        if layout.journal.exists():
            recover(layout, json.loads(layout.journal.read_text()))
        tx = None
        try:
            with locked(layout.root / 'run/lock/argosbx-hy2.lock'):
                tx = snapshot(layout)
                save(layout.journal, tx)
                launcher = ('#!/bin/sh\nexport PATH=/usr/sbin:/usr/bin:/sbin:/bin\nexport LC_ALL=C.UTF-8\n'
                            f'exec /usr/bin/python3 -I {shlex.quote(str(Path(__file__).resolve()))} run "$@"\n')
                atomic(layout.launcher, launcher.encode(), 0o755)
                link(layout.link, app)
            worker = start_candidate(layout, app, args, tx)
            code = worker.wait()
            if code:
                raise RuntimeError('候选管理程序安装未完成')
            runtime_ready(app)
            subscription = command([sys.executable, '-I', '-B', app / 'entry.py', 'subscription']).stdout
            if subscription.count(b'https://') != 1:
                raise RuntimeError('候选程序未输出唯一订阅')
            remove_journal(layout)
        except BaseException:
            if tx is not None:
                recover(layout, tx)
            raise
        # A disconnected terminal after commit must not roll back healthy code.
        sys.stdout.flush()
        sys.stdout.buffer.write(subscription)
        sys.stdout.buffer.flush()


def main():
    layout = Layout()
    if os.geteuid() != 0:
        raise RuntimeError('请使用 root 或 sudo')
    os.umask(0o077)
    if sys.argv[1] == 'deploy':
        deploy(layout, sys.argv[2], sys.argv[3:])
        return
    args = sys.argv[2:]
    tx = json.loads(layout.journal.read_text()) if layout.journal.exists() else None
    if args and args[0] in ('network', 'firewall') and tx and owner_alive(tx):
        # Candidate activation holds the exclusive app lock and waits for these
        # systemd prerequisites. Only these infrastructure commands may bypass it.
        os.execv(sys.executable, [sys.executable, '-I', '-B', str(layout.link / 'entry.py'), *args])
    try:
        with locked(layout.base / 'app.lock', shared=True) as stream:
            if layout.journal.exists():
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
                with locked(layout.base / 'app.lock') as recovery_stream:
                    if layout.journal.exists():
                        recover(layout, json.loads(layout.journal.read_text()), args[0] if args else '')
                    # Retain a shared lock across exec before any manager action.
                    fcntl.flock(recovery_stream, fcntl.LOCK_SH)
                    os.set_inheritable(recovery_stream.fileno(), True)
                    os.execv(sys.executable, [sys.executable, '-I', '-B', str(layout.link / 'entry.py'), *args])
            os.set_inheritable(stream.fileno(), True)
            os.execv(sys.executable, [sys.executable, '-I', '-B', str(layout.link / 'entry.py'), *args])
    except TimeoutError:
        if args[:2] == ['health', '--heal']:
            return
        raise


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt):
        # Journals may contain credentials. Never echo their contents/tool output.
        print('管理程序安装/恢复未完成；旧部署已尝试恢复，失败时保留受限恢复记录。', file=sys.stderr)
        sys.exit(1)
