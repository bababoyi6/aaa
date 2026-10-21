#!/usr/bin/env bash
# GPL-3.0-or-later. Run from a verified release archive or a reviewed checkout.
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
export LC_ALL=C.UTF-8
if [[ ${EUID} -ne 0 ]]; then
  printf '%s\n' '请使用 sudo bash install.sh（不要把 Cloudflare Token 写入命令）。' >&2
  exit 1
fi
# shellcheck source=/dev/null
source /etc/os-release
if [[ ${ID:-} != debian || ${VERSION_ID:-} != 13 ]]; then
  printf '%s\n' '当前仅支持 Debian 13（trixie）。' >&2
  exit 1
fi
case "$(uname -m)" in x86_64|aarch64) ;; *) printf '%s\n' '仅支持 amd64 / arm64。' >&2; exit 1;; esac
[[ -d /run/systemd/system ]] || { printf '%s\n' '需要 systemd VPS。' >&2; exit 1; }
src=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
[[ -f "$src/argosbx/manager.py" && -f "$src/dependencies.lock.json" ]] || exit 1
# Serialize installer copies independently from manager's lifecycle lock.
runtime=/run/argosbx-hy2
[[ ! -L "$runtime" && ( ! -e "$runtime" || ( -d "$runtime" && -O "$runtime" ) ) ]] || {
  printf '%s\n' '安装器运行目录类型或归属异常。' >&2; exit 1;
}
install -d -m 700 "$runtime"
[[ ! -L "$runtime/installer.lock" ]] || { printf '%s\n' '安装器锁不能是符号链接。' >&2; exit 1; }
exec 8>"$runtime/installer.lock"
flock -n 8 || { printf '%s\n' '另一个安装器正在执行。' >&2; exit 1; }
base=/opt/argosbx-hy2
for location in "$base" /etc/argosbx-hy2 /var/lib/argosbx-hy2; do
  if [[ -e "$location" && ! -f "$location/.argosbx-owned" ]]; then
    printf '发现未知目录 %s，拒绝覆盖。\n' "$location" >&2
    exit 1
  fi
  [[ ! -L "$location" ]] || { printf '%s\n' '项目目录不能是符号链接。' >&2; exit 1; }
done
for unit in /etc/systemd/system/argosbx-*.service /etc/systemd/system/argosbx-*.timer; do
  if [[ -e "$unit" && ! -f /etc/argosbx-hy2/.argosbx-owned ]]; then
    printf '发现未知服务 %s，拒绝覆盖。\n' "$unit" >&2
    exit 1
  fi
done
if [[ -e /usr/local/bin/argosbx && ! -f "$base/.argosbx-owned" ]]; then
  printf '%s\n' '已有未知 argosbx 管理命令，拒绝覆盖。' >&2
  exit 1
fi
printf '%s\n' '安装 Debian 签名源中的必要工具；不会关闭现有防火墙。'
export DEBIAN_FRONTEND=noninteractive
need_packages=false
[[ -f "$base/.argosbx-owned" ]] || need_packages=true
for tool in python3 curl openssl nft ss dig sysctl flock tc ip modprobe; do
  command -v "$tool" >/dev/null || need_packages=true
done
if ! dpkg-query -W -f='${db:Status-Status}' unattended-upgrades 2>/dev/null | grep -qx installed; then
  need_packages=true
fi
if [[ "$need_packages" == true ]]; then
  apt-get update -qq
  apt-get install -y --no-install-recommends python3 ca-certificates curl openssl nftables iproute2 dnsutils unattended-upgrades procps util-linux kmod
else
  printf '%s\n' '复用已安装依赖，直接进入项目安装或管理。'
fi
ensure_time_sync() {
  if [[ $(timedatectl show -p NTPSynchronized --value) == yes ]]; then
    return
  fi
  if [[ $(timedatectl show -p CanNTP --value) != yes ]]; then
    # Do not replace an existing time provider with a conflicting package.
    provider_found=false
    for provider in chrony ntpsec openntpd systemd-timesyncd; do
      if dpkg-query -W -f='${db:Status-Status}' "$provider" 2>/dev/null | grep -qx installed; then
        provider_found=true
      fi
    done
    if [[ "$provider_found" == false ]]; then
      apt-get install -y --no-install-recommends systemd-timesyncd
    fi
  fi
  # timedated may have cached an empty NTP unit list before apt installed a
  # provider. Reload units and refresh that broker, without restarting the
  # time provider itself or replacing a working chrony/ntpsec installation.
  systemctl daemon-reload
  systemctl try-restart systemd-timedated.service
  # The package post-install may already have started and synchronized it.
  if [[ $(timedatectl show -p NTPSynchronized --value) != yes ]]; then
    timedatectl set-ntp true || {
      printf '%s\n' '无法启用时间同步；已刷新服务列表，请检查现有 NTP 服务是否被屏蔽或缺失。' >&2
      return 1
    }
  fi
  for ((attempt=0; attempt<30; attempt++)); do
    [[ $(timedatectl show -p NTPSynchronized --value) == yes ]] && break
    sleep 1
  done
  [[ $(timedatectl show -p NTPSynchronized --value) == yes ]] || {
    printf '%s\n' 'NTP 尚未同步，请检查现有时间服务和 UDP 123 可达性，再重新执行安装。' >&2
    return 1
  }
}
ensure_time_sync
install -d -m 755 "$base" /etc/argosbx-hy2
install -d -m 711 /var/lib/argosbx-hy2
touch "$base/.argosbx-owned" /etc/argosbx-hy2/.argosbx-owned /var/lib/argosbx-hy2/.argosbx-owned
# A content-derived destination makes interrupted/repeated copies recoverable.
bundle=$(python3 -I -c 'import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); h=hashlib.sha256(); files=sorted(p.glob("argosbx/*.py"))+[p/x for x in ("entry.py","bootstrap.py","dependencies.lock.json","VERSION")]; [(h.update(x.relative_to(p).as_posix().encode()),h.update(x.read_bytes())) for x in files]; print(h.hexdigest()[:16])' "$src")
version=$(cat "$src/VERSION")
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 1
app="$base/app-$version-$bundle"
stage=$(mktemp -d "$base/.app-XXXXXXXX")
trap '[[ -z ${stage:-} ]] || rm -rf -- "$stage"' EXIT
install -d -m 755 "$stage/argosbx"
install -m 644 "$src"/argosbx/*.py "$stage/argosbx/"
install -m 644 "$src/entry.py" "$src/bootstrap.py" "$src/dependencies.lock.json" "$src/VERSION" "$src/LICENSE" "$src/NOTICE.md" "$stage/"
chmod 755 "$stage"
if [[ ! -d "$app" ]]; then mv -- "$stage" "$app"; else rm -rf -- "$stage"; fi
stage=''
bootstrap="$base/bootstrap-$bundle.py"
if [[ -e "$bootstrap" ]]; then
  cmp -s -- "$app/bootstrap.py" "$bootstrap" || { printf '%s\n' '已有同名引导器内容异常，停止安装。' >&2; exit 1; }
else
  install -m 644 "$app/bootstrap.py" "$base/.bootstrap-next"
  mv -T -- "$base/.bootstrap-next" "$bootstrap"
fi
exec python3 -I -B "$bootstrap" deploy "$app" "$@"
