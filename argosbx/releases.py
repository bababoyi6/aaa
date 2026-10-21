"""Official release selection, digest verification and safe binary extraction."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
import time
import urllib.error
import urllib.request


class ReleaseError(RuntimeError):
    pass


def version_tuple(tag):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", tag)
    if not match:
        raise ReleaseError("拒绝非稳定版本号")
    return tuple(map(int, match.groups()))


def supported(tag, lock):
    ver = version_tuple(tag)
    if not (version_tuple(lock["template_sing_box_min"]) <= ver <
            version_tuple(lock["template_sing_box_max_exclusive"])):
        raise ReleaseError("最新 sing-box 超出模板已验证范围；保留现有版本，等待项目更新")


def validate_lock(lock):
    repositories = {"sing-box": "SagerNet/sing-box", "caddy": "caddyserver/caddy", "lego": "go-acme/lego"}
    if lock.get("schema") != 1 or set(lock["dependencies"]) != set(repositories):
        raise ReleaseError("依赖清单结构异常")
    supported(lock["dependencies"]["sing-box"]["version"], lock)
    for name, repo in repositories.items():
        dep = lock["dependencies"][name]
        version_tuple(dep["version"])
        if dep["repository"] != repo or set(dep["assets"]) != {"amd64", "arm64"}:
            raise ReleaseError("依赖来源或架构异常")
        for arch, spec in dep["assets"].items():
            version = dep['version'].lstrip('v')
            expected = {'sing-box': f'sing-box-{version}-linux-{arch}.tar.gz',
                        'caddy': f'caddy_{version}_linux_{arch}.tar.gz',
                        'lego': f'lego_v{version}_linux_{arch}.tar.gz'}[name]
            if (spec["name"] != expected or
                    spec["url"] != f'https://github.com/{repo}/releases/download/{dep["version"]}/{spec["name"]}' or
                    not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"])):
                raise ReleaseError("依赖地址或 SHA-256 异常")


def fetch(url, destination=None):
    if not url.startswith("https://"):
        raise ReleaseError("下载必须使用 HTTPS")
    error = None
    for attempt in range(3):
        partial = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ArgoSBX-HY2/0.1"})
            with urllib.request.urlopen(req, timeout=60) as response:
                if not response.url.startswith("https://"):
                    raise ReleaseError("拒绝不安全的重定向")
                limit = (8 if destination is None else 256) * 1024 * 1024
                if int(response.headers.get("Content-Length", "0")) > limit:
                    raise ReleaseError("下载超过大小限制")
                started, size = time.monotonic(), 0
                def chunks():
                    nonlocal size
                    while True:
                        # One underlying read returns available bytes; a slow
                        # peer cannot keep read(n) filling a whole block forever.
                        block = response.read1(65536)
                        size += len(block)
                        if size > limit or time.monotonic() - started > 180:
                            raise ReleaseError("下载超过大小或时间限制")
                        if not block:
                            return
                        yield block
                if destination is None:
                    return b"".join(chunks())
                destination = Path(destination)
                if destination.is_symlink():
                    raise ReleaseError("下载目标不能是符号链接")
                fd, name = tempfile.mkstemp(prefix=".download-", dir=destination.parent)
                partial = Path(name)
                with os.fdopen(fd, "wb") as out:
                    for block in chunks():
                        out.write(block)
                    out.flush()
                    os.fsync(out.fileno())
                partial.replace(destination)
                return destination
        except (OSError, urllib.error.URLError) as exc:
            error = exc
            if attempt < 2:
                time.sleep(2 ** attempt)
        finally:
            if partial is not None:
                partial.unlink(missing_ok=True)
    raise ReleaseError(f"下载失败：{url}（{type(error).__name__}）")


def latest_sing_box(arch, lock):
    repo = "SagerNet/sing-box"
    release = json.loads(fetch(f"https://api.github.com/repos/{repo}/releases/latest"))
    if release.get("draft") or release.get("prerelease"):
        raise ReleaseError("拒绝草稿或预发布")
    tag = release["tag_name"]
    supported(tag, lock)
    name = f"sing-box-{tag.lstrip('v')}-linux-{arch}.tar.gz"
    asset = next((a for a in release["assets"] if a["name"] == name), None)
    if asset is None:
        raise ReleaseError(f"官方发布缺少 {name}")
    url = f"https://github.com/{repo}/releases/download/{tag}/{name}"
    if asset["browser_download_url"] != url:
        raise ReleaseError("官方附件地址不匹配")
    digest = asset.get("digest") or ""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        # Fail closed, rather than downloading unverified executable code.
        raise ReleaseError("GitHub 官方附件缺少 SHA-256 digest，停止更新")
    return {"version": tag, "name": name, "url": url, "sha256": digest[7:]}


def sha256(path):
    with Path(path).open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def extract_binary(archive, binary, destination):
    # Do not extract a remote archive as root: copy only one regular member.
    with tarfile.open(archive, "r:gz") as tar:
        members, total = [], 0
        for index, member in enumerate(tar):
            total += member.size
            if index >= 4096 or total > 512 * 1024 * 1024:
                raise ReleaseError("发行包超过解压限制")
            if Path(member.name).name == binary and member.isfile():
                members.append(member)
        if len(members) != 1 or members[0].size > 512 * 1024 * 1024:
            raise ReleaseError("发行包的二进制结构异常")
        member = members[0]
        if member.name.startswith("/") or ".." in Path(member.name).parts:
            raise ReleaseError("发行包包含不安全路径")
        destination = Path(destination)
        if destination.is_symlink():
            raise ReleaseError("二进制目标不能是符号链接")
        fd, name = tempfile.mkstemp(prefix=".binary-", dir=destination.parent)
        try:
            with os.fdopen(fd, "wb") as dst, tar.extractfile(member) as src:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
                os.fchmod(dst.fileno(), 0o755)
            os.replace(name, destination)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def install_binary(spec, binary, destination, cache):
    cache = Path(cache)
    if cache.is_symlink():
        raise ReleaseError("缓存目录不能是符号链接")
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = spec["sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ReleaseError("无效的固定校验值")
    archive = cache / (digest + ".tar.gz")
    if archive.is_symlink():
        raise ReleaseError("缓存附件不能是符号链接")
    if not archive.exists() or sha256(archive) != digest:
        fetch(spec["url"], archive)
    if sha256(archive) != digest:
        archive.unlink()
        raise ReleaseError("官方发行包校验失败，未执行该文件")
    extract_binary(archive, binary, destination)
