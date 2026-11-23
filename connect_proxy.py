#!/usr/bin/env python3
"""SSH ProxyCommand: tunnel SSH through an HTTP CONNECT proxy.

Usage (in ssh options):
  -o "ProxyCommand=python3 /path/to/connect_proxy.py %h %p"

Reads the proxy URL from HTTPS_PROXY / https_proxy, e.g.
  https_proxy=http://user:pass@proxy-host:3128

Needed when the VM can only reach the internet through an HTTP proxy
(direct egress blocked or TLS-intercepted). Pure stdlib.
"""
import os, socket, sys, select, base64
from urllib.parse import urlparse, unquote

proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
if not proxy:
    sys.stderr.write("HTTPS_PROXY not set\n"); sys.exit(1)
u = urlparse(proxy)
host, port = sys.argv[1], int(sys.argv[2])
s = socket.create_connection((u.hostname, u.port or 3128), timeout=15)
auth = ""
if u.username:
    cred = f"{unquote(u.username)}:{unquote(u.password or '')}".encode()
    auth = f"Proxy-Authorization: Basic {base64.b64encode(cred).decode()}\r\n"
s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode())
buf = b""
while b"\r\n\r\n" not in buf:
    d = s.recv(4096)
    if not d: sys.exit(1)
    buf += d
if b" 200 " not in buf.split(b"\r\n")[0]:
    sys.stderr.write(buf[:200].decode(errors="ignore")); sys.exit(1)
# leftover bytes after header belong to stream
leftover = buf.split(b"\r\n\r\n", 1)[1]
if leftover: os.write(1, leftover)
s.setblocking(False); os.set_blocking(0, False)
fds = [s.fileno(), 0]
while True:
    r, _, _ = select.select(fds, [], [], 120)
    if not r: break
    for fd in r:
        try: data = s.recv(65536) if fd == s.fileno() else os.read(0, 65536)
        except BlockingIOError: continue
        if not data: sys.exit(0)
        if fd == s.fileno(): os.write(1, data)
        else: s.sendall(data)
