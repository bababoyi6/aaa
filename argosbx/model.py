"""Pure configuration generation. No network, process or filesystem mutations."""
import base64
import ipaddress
import json
import re
import secrets
from urllib.parse import quote

ROOT = "/etc/argosbx-hy2/current"
# Explicitly includes multicast, CGNAT, documentation, mapped IPv4 and metadata.
BLOCKED = [
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4", "240.0.0.0/4", "::/128", "::1/128", "::ffff:0:0/96",
    "64:ff9b::/96", "64:ff9b:1::/48", "100::/64", "2001::/32",
    "2001:db8::/32", "2002::/16", "fc00::/7", "fe80::/10", "ff00::/8",
]


def domain_name(value):
    value = value.strip().rstrip(".").lower()
    if len(value) > 253 or "." not in value:
        raise ValueError("请填写专用的完整子域名")
    if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x)
               for x in value.split(".")):
        raise ValueError("域名须为 ASCII/Punycode，不允许通配符、URL 或特殊字符")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return value
    raise ValueError("第一版使用域名证书，请勿填写 IP")


def validate_node(node):
    if domain_name(node["domain"]) != node["domain"]:
        raise ValueError("元数据域名必须使用规范形式")
    for key in ("password", "token"):
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", node[key]):
            raise ValueError(f"无效的 {key}（要求 32 字节随机值）")
    if node["password"] == node["token"]:
        raise ValueError("节点密码不得与订阅 Token 复用")
    mode = node.get("obfs", "none")
    if mode not in ("none", "salamander"):
        raise ValueError("仅支持标准 HTTP/3 或 Salamander 模式")
    if mode == "salamander":
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", node.get("obfs_password", "")):
            raise ValueError("混淆密码必须是独立的 32 字节随机值")
        if node["obfs_password"] in (node["password"], node["token"]):
            raise ValueError("混淆密码不得复用节点密码或订阅 Token")
    elif "obfs_password" in node:
        raise ValueError("标准模式不能保留混淆密码")
    name = node["name"]
    if not isinstance(name, str) or not name or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ValueError("节点名应为 1–80 个无控制字符的字符")
    if name.upper() in {"PROXY", "DIRECT", "REJECT", "GLOBAL", "PASS", "REJECT-DROP", "COMPATIBLE"}:
        raise ValueError("节点名称与客户端保留策略名冲突，请换一个名称")
    return node


def new_node(domain, name="我的 HY2", *, obfs="none"):
    node = {"schema": 1, "domain": domain_name(domain), "name": name,
            "password": secrets.token_urlsafe(32), "token": secrets.token_urlsafe(32)}
    if obfs != "none":
        node.update(obfs=obfs, obfs_password=secrets.token_urlsafe(32))
    return validate_node(node)


def obfuscation(node):
    validate_node(node)
    return ({"obfs": {"type": "salamander", "password": node["obfs_password"]}}
            if node.get("obfs") == "salamander" else {})


def subscription(node):
    return f'https://{node["domain"]}/s/{node["token"]}'


def uri(node):
    validate_node(node)
    obfs = ("&obfs=salamander&obfs-password=" + quote(node["obfs_password"], safe="")
            if node.get("obfs") == "salamander" else "")
    return (f'hysteria2://{quote(node["password"], safe="")}@{node["domain"]}:443/'
            f'?sni={node["domain"]}&insecure=0{obfs}#{quote(node["name"], safe="")}')


def server_config(node, root=ROOT):
    validate_node(node)
    return {
        "log": {"level": "error", "timestamp": True},
        "dns": {"servers": [{"type": "local", "tag": "local"}]},
        "inbounds": [{"type": "hysteria2", "tag": "hy2-in", "listen": "0.0.0.0",
                      "listen_port": 443, "users": [{"name": "personal", "password": node["password"]}],
                      "ignore_client_bandwidth": True, **obfuscation(node),
                      "tls": {"enabled": True, "server_name": node["domain"],
                              "certificate_path": f"{root}/tls/fullchain.pem",
                              "key_path": f"{root}/tls/key.pem"},
                      "masquerade": {"type": "file", "directory": "/var/lib/argosbx-hy2/public"}}],
        "outbounds": [{"type": "direct", "tag": "direct"}],
        "route": {"default_domain_resolver": {"server": "local", "strategy": "ipv4_only"},
                  "rules": [{"action": "resolve", "strategy": "ipv4_only"},
                            {"ip_is_private": True, "action": "reject"},
                            {"ip_cidr": BLOCKED, "action": "reject"}], "final": "direct"},
    }


def client_config(node):
    return {
        "log": {"level": "warn"},
        "dns": {"servers": [{"type": "local", "tag": "local"}]},
        "inbounds": [{"type": "mixed", "tag": "local-in", "listen": "127.0.0.1", "listen_port": 7890}],
        "outbounds": [{"type": "hysteria2", "tag": "proxy", "server": node["domain"],
                       "server_port": 443, "password": node["password"], **obfuscation(node),
                       "tls": {"enabled": True, "server_name": node["domain"], "insecure": False}}],
        "route": {"default_domain_resolver": "local", "final": "proxy"},
    }


def clash_config(node):
    validate_node(node)
    # JSON quoted strings are valid YAML scalars; never interpolate raw user text.
    q = lambda value: json.dumps(value, ensure_ascii=False)
    obfs = ('    obfs: salamander\n    obfs-password: ' + q(node["obfs_password"]) + '\n'
            if node.get("obfs") == "salamander" else '')
    return f'''# ArgoSBX-HY2 — Mihomo; local mixed proxy at 127.0.0.1:7890
mixed-port: 7890
allow-lan: false
bind-address: 127.0.0.1
mode: rule
log-level: warning
ipv6: false
dns:
  enable: true
  listen: 127.0.0.1:1053
  ipv6: false
  enhanced-mode: redir-host
  respect-rules: true
  default-nameserver: [223.5.5.5, 119.29.29.29]
  proxy-server-nameserver: [https://dns.alidns.com/dns-query, https://doh.pub/dns-query]
  nameserver: [https://1.1.1.1/dns-query, https://8.8.8.8/dns-query]
proxies:
  - name: {q(node["name"])}
    type: hysteria2
    server: {q(node["domain"])}
    port: 443
    password: {q(node["password"])}
{obfs}    sni: {q(node["domain"])}
    skip-cert-verify: false
proxy-groups:
  - name: PROXY
    type: select
    proxies: [{q(node["name"])}]
rules:
  - MATCH,PROXY
'''


def format_page(node):
    # No extra subscription URLs: the same address serves both supported apps.
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>私人订阅</title>
<body><h1>私人订阅</h1><p>请将当前地址直接粘贴到 Clash Verge Rev 或 Shadowrocket 的订阅导入框。</p>
<p>两个客户端使用同一个地址，服务器自动返回对应格式。请保留客户端默认 User-Agent。</p>
<p>这是私人订阅，请勿分享地址。</p></body></html>'''


def exports(node):
    validate_node(node)
    return {"clash.yaml": clash_config(node),
            "shadowrocket.txt": base64.b64encode((uri(node) + "\n").encode()).decode() + "\n",
            "singbox.json": json.dumps(client_config(node), ensure_ascii=False, indent=2) + "\n",
            "index.html": format_page(node)}


def caddy_config(node, root=ROOT, ua_rules=None):
    """One URL, explicit format overrides, custom UA overrides, built-in app UAs."""
    validate_node(node)
    path = f'/s/{node["token"]}'
    routes = []
    # Explicit unknown format must never fall back to an inferred client format.
    for fmt, filename, mime in [
        ("clash", "clash.yaml", "text/yaml; charset=utf-8"),
        ("shadowrocket", "shadowrocket.txt", "text/plain; charset=utf-8"),
        ("singbox", "singbox.json", "application/json; charset=utf-8"),
    ]:
        routes.append(f'''            @fmt_{fmt} query format={fmt}
            handle @fmt_{fmt} {{
                header Content-Type "{mime}"
                rewrite * /{filename}
                file_server
            }}''')
    for i, rule in enumerate(ua_rules or []):
        if rule["format"] not in ("clash", "shadowrocket", "singbox"):
            raise ValueError("未知 UA 格式")
        pattern = rule["pattern"]
        if not re.fullmatch(r"[A-Za-z0-9 ._/*()+-]{1,160}", pattern):
            raise ValueError("UA 模式包含不允许的字符")
        filename, mime = {"clash": ("clash.yaml", "text/yaml"),
                          "shadowrocket": ("shadowrocket.txt", "text/plain"),
                          "singbox": ("singbox.json", "application/json")}[rule["format"]]
        routes.append(f'''            @ua_{i} {{
                not query format=*
                header User-Agent "{pattern}"
            }}
            handle @ua_{i} {{
                header Content-Type "{mime}; charset=utf-8"
                rewrite * /{filename}
                file_server
            }}''')
    # Clash Verge Rev defaults to clash-verge/vVERSION (upstream network.rs).
    # Shadowrocket uses its product prefix; match case-insensitively and anchor
    # the product boundary so browser strings containing the name cannot match.
    for name, pattern, filename, mime in [
        ("clash", r"(?i)^(clash-verge|clash[.]meta|clash|mihomo)([/ ]|$)", "clash.yaml", "text/yaml"),
        ("shadowrocket", r"(?i)^shadowrocket([/ ]|$)", "shadowrocket.txt", "text/plain"),
    ]:
        routes.append(f'''            @app_{name} {{
                not query format=*
                header_regexp User-Agent "{pattern}"
            }}
            handle @app_{name} {{
                header Content-Type "{mime}; charset=utf-8"
                rewrite * /{filename}
                file_server
            }}''')
    return f'''{{
    admin off
    persist_config off
    auto_https off
    servers {{
        protocols h1 h2
        max_header_size 32KB
        timeouts {{
            read_header 10s
            read_body 15s
            write 30s
            idle 2m
        }}
    }}
}}
https://{node["domain"]} {{
    bind 0.0.0.0
    tls {root}/tls/fullchain.pem {root}/tls/key.pem
    header {{
        -Server
        -Alt-Svc
        X-Content-Type-Options nosniff
        Referrer-Policy no-referrer
    }}
    route {{
        handle {path} {{
            header {{
                Cache-Control "private, no-store"
                >Vary "User-Agent, Accept-Encoding"
                X-Robots-Tag "noindex, nofollow"
                Content-Disposition "attachment; filename=ArgoSBX-HY2"
                Profile-Update-Interval "24"
            }}
            root * {root}/exports
{chr(10).join(routes)}
            handle {{
                header Content-Type "text/html; charset=utf-8"
                rewrite * /index.html
                file_server
            }}
        }}
        handle /s/* {{
            respond 404
        }}
        handle {{
            root * /var/lib/argosbx-hy2/public
            file_server
        }}
    }}
}}
'''


PUBLIC_PAGE = '''<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Welcome</title><body><h1>Welcome</h1><p>This site is online.</p></body></html>'''
