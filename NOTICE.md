# 来源、许可与实现范围

ArgoSBX-HY2 参考 [yonggekkk/argosbx](https://github.com/yonggekkk/argosbx) 的一行安装、中文管理菜单与多客户端导出使用方式。

固定参考提交：`ef04c342f90070df64e4cf3483946fc7ebebc9ab`（2026-10-20 查询）。

参考脚本 `argosbx.sh` 的 SHA-256：`e59f46a609d7468a1c286dbb70af4b8aca50016e49b656bbf5b202ac5eb94b65`。

本仓库重新实现配置模型与生命周期管理，不在安装时执行上游脚本，不混用上游二进制附件。相对于参考项目，仅保留 sing-box/Hysteria2，加入 HTTPS 订阅、HTTP-01 / DNS-01 证书、角色分权、systemd、事务切换和恢复。原项目的 Argo、其他协议、WARP、容器和端口跳跃不属于首版功能。

本仓库以 GPL-3.0-or-later 发布，完整许可证见 [LICENSE](LICENSE)。分发修改版本时保留来源与许可，并按 GPL 提供对应源码。发布包包括全部本项目运行源码。未将第三方二进制纳入仓库或项目发行包；安装时从各自官方 GitHub Release 下载，相关软件遵循各自的许可证：

- [SagerNet/sing-box](https://github.com/SagerNet/sing-box) — GPL-3.0-or-later，见该版本的 LICENSE 与构建信息。
- [caddyserver/caddy](https://github.com/caddyserver/caddy) — Apache-2.0。
- [go-acme/lego](https://github.com/go-acme/lego) — MIT。

版本和官方附件 SHA-256 见 `dependencies.lock.json`；系统软件包来自 Debian 签名软件源，由 apt 验证并接收安全维护，不执行额外远程安装脚本。
