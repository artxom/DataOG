"""单库批量落标贯标执行台模块."""

from modules.standard_migrator.router import router
from modules.standard_migrator.service import StandardMigratorService

__all__ = ["StandardMigratorService", "router"]
