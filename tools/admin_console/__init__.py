"""Data.O.G 管理员可视化总控台 (`tools.admin_console`)。

仅面向管理员/演示人员在本机运维 Data.O.G 激活码签发、演示沙箱与工作台启停，
已被 `build_exe.py` (`tools`) 排除，绝不进入客户端发行包。
"""

from tools.admin_console.auth import AdminAuthManager, SESSION_COOKIE_NAME
from tools.admin_console.server import create_admin_app
from tools.admin_console.service import AdminConsoleService

__all__ = [
    "AdminAuthManager",
    "AdminConsoleService",
    "SESSION_COOKIE_NAME",
    "create_admin_app",
]
