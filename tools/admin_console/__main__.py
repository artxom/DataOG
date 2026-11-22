"""Data.O.G 管理员可视化总控台 CLI 与服务启动入口 (`python3 -m tools.admin_console`)."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import urllib.request
import webbrowser

import uvicorn

from tools.admin_console.auth import AdminAuthManager
from tools.admin_console.server import create_admin_app
from tools.admin_console.service import AdminConsoleService


def _ensure_utf8_stdio() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _find_available_port(host: str, preferred_port: int) -> int:
    for candidate in range(preferred_port, preferred_port + 40):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, candidate))
                return candidate
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def _is_console_already_running(host: str, port: int) -> bool:
    url = f"http://{host}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=0.6) as resp:
            if resp.status != 200:
                return False
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            return isinstance(data, dict) and data.get("service") == "dataog-admin-console"
    except Exception:
        return False


def main() -> int:
    _ensure_utf8_stdio()
    parser = argparse.ArgumentParser(description="Data.O.G 本地可视化管理员总控台")
    parser.add_argument("--port", type=int, default=18090, help="监听端口 (默认 18090)")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    parser.add_argument("--set-admin-user", default="", help="配置或重置管理员账号")
    parser.add_argument(
        "--set-admin-password-stdin",
        action="store_true",
        help="从标准输入读取管理员密码",
    )
    args = parser.parse_args()

    auth = AdminAuthManager()

    if args.set_admin_user:
        if args.set_admin_password_stdin:
            raw_pw = sys.stdin.readline().rstrip("\r\n")
        else:
            import getpass

            raw_pw = getpass.getpass("请输入管理员密码: ")
        meta = auth.set_credentials(args.set_admin_user, raw_pw)
        print(f"[OK] 管理员凭据已安全保存（账号: {meta['username']}，采用 PBKDF2-HMAC-SHA256 加密存储）。")
        return 0

    host = "127.0.0.1"
    if _is_console_already_running(host, args.port):
        url = f"http://{host}:{args.port}"
        print(f"[*] 管理员总控台已在运行: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    port = _find_available_port(host, args.port)
    url = f"http://{host}:{port}"

    svc = AdminConsoleService(console_port_provider=lambda: port)
    app = create_admin_app(auth_manager=auth, service=svc)

    print("=" * 74)
    print("  Data.O.G 管理员可视化总控台 — 演示沙箱 · 工作台启停 · 激活码签发")
    print("=" * 74)
    if port != args.port:
        print(f"  [!] 提示: 端口 {args.port} 已被占用，已自动切换至 {port}")
    print(f"  [*] 总控台访问地址 : {url}")
    print(f"  [*] 管理员账号状态 : {'已配置' if auth.is_initialized() else '待首次网页初始化'}")
    print("  [*] 按 Ctrl + C 可停止总控台并自动清理托管的演示工作台进程。")
    print("=" * 74)

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        loop="asyncio",
        http="h11",
        ws="none",
    )
    uvicorn.Server(config).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
