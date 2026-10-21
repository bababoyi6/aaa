"""Systemd definitions. Services own neither API tokens nor management state."""
BASE = "/etc/argosbx-hy2/current"


def service(binary, user, config, description):
    command = (f"{BASE}/bin/sing-box run -c {BASE}/server.json" if binary == "sing-box" else
               f"{BASE}/bin/caddy run --config {BASE}/Caddyfile --adapter caddyfile")
    return f'''# Managed by ArgoSBX-HY2
[Unit]
Description={description}
Wants=network-online.target
Wants=argosbx-network.service
After=network-online.target argosbx-firewall.service argosbx-network.service
Requires=argosbx-firewall.service
StartLimitIntervalSec=300
StartLimitBurst=8

[Service]
Type=simple
User={user}
Group={user}
SupplementaryGroups=argosbx-tls
ExecStart={command}
Restart=on-failure
RestartSec=10s
TimeoutStopSec=20s
LimitNOFILE=65536
TasksMax=256
MemoryAccounting=true
MemoryHigh={"40%" if binary == "sing-box" else "15%"}
MemoryMax={"50%" if binary == "sing-box" else "20%"}
OOMPolicy=stop
UMask=0027
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK
StandardOutput=null
StandardError=null

[Install]
WantedBy=multi-user.target
'''


def units():
    result = {
        "argosbx-hy2.service": service("sing-box", "argosbx-hy2", "server.json", "ArgoSBX Hysteria2"),
        "argosbx-web.service": service("caddy", "argosbx-web", "Caddyfile", "ArgoSBX HTTPS subscription"),
        "argosbx-network.service": '''# Managed by ArgoSBX-HY2
[Unit]
Description=ArgoSBX optional UDP socket buffer limits
Wants=network-online.target
After=network-online.target systemd-sysctl.service
Before=argosbx-hy2.service argosbx-web.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/argosbx network
RemainAfterExit=yes
TimeoutStartSec=60

[Install]
WantedBy=multi-user.target
''',
        "argosbx-firewall.service": '''# Managed by ArgoSBX-HY2
[Unit]
Description=ArgoSBX private egress guard
Before=argosbx-hy2.service argosbx-web.service
After=ufw.service nftables.service

[Service]
Type=oneshot
ExecStart=/usr/local/bin/argosbx firewall
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
''',
    }
    for task, calendar in [("renew", "*-*-* 03,15:15:00"),
                           ("check-update", "*-*-* 05:30:00"),
                           ("health", "*-*-* *:0/5:00")]:
        result[f"argosbx-{task}.service"] = f'''# Managed by ArgoSBX-HY2
[Unit]
Description=ArgoSBX {task}
After=network-online.target argosbx-network.service argosbx-firewall.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/argosbx {task}{" --heal" if task == "health" else ""}
UMask=0077
TimeoutStartSec=1800
StandardOutput=journal
StandardError=journal
'''
        result[f"argosbx-{task}.timer"] = f'''# Managed by ArgoSBX-HY2
[Unit]
Description=ArgoSBX {task} schedule

[Timer]
OnCalendar={calendar}
RandomizedDelaySec={30 if task == "health" else 900}
Persistent=true

[Install]
WantedBy=timers.target
'''
    return result
