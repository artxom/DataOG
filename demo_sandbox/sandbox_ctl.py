"""启明商业银行信创数仓治理演示沙箱一键运维控制台 (sandbox_ctl.py).

支持子命令：
  up                 启动三引擎 Docker 容器 -> 等待就绪 -> 灌入金融样本与五大暗雷 -> 采集生成双工作区
  reset              不重启容器极速复原活库靶表、业务数据与演练工作区（5~15秒）
  status             检测三引擎连通性、核心表行数与五大业务暗雷精确数字
  generate-assets    免 Docker 离线生成 46 列标准字典 Excel、绑标模板与两份 `.dataog.db`
  bootstrap-demo-home  向指定隔离 DATAOG_HOME 一键预装双工作区与三引擎本机连接凭据
  down               停止并清理演示容器
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.platform.context import PlatformContext  # noqa: E402
from demo_sandbox.live_seeder import (  # noqa: E402
    DWS_DB,
    DWS_HOST,
    DWS_PASSWORD,
    DWS_PORT,
    DWS_USER,
    INCEPTOR_DB,
    INCEPTOR_HOST,
    INCEPTOR_PORT,
    INCEPTOR_USER,
    MYSQL_DB,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
    _open_dws_conn,
    _open_inceptor_client,
    _open_mysql_conn,
    seed_all,
    verify_live_anomalies,
)
from demo_sandbox.workspace_builder import (  # noqa: E402
    build_all_workspaces,
    build_demo_connection_profiles,
    build_live_sandbox_workspace,
)

SANDBOX_DIR = Path(__file__).resolve().parent
COMPOSE_FILE = SANDBOX_DIR / "docker-compose.yml"
DEFAULT_OUTPUT_DIR = SANDBOX_DIR / "output"
DEFAULT_DEMO_HOME = SANDBOX_DIR / ".demo_home"


def _compose_cmd(extra_args: List[str], *, registry_prefix: str = "") -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if registry_prefix:
        norm = registry_prefix.rstrip("/") + "/"
        env["DOCKER_REGISTRY_PREFIX"] = norm
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *extra_args]
    return subprocess.run(cmd, env=env, text=True, check=True)


def _wait_for_engines(timeout_sec: int = 120) -> None:
    """轮询等待 PG16(DWS) / MySQL8 / Spark Thrift(Inceptor) 全部可用."""
    deadline = time.time() + timeout_sec
    ready = {"dws": False, "mysql": False, "inceptor": False}
    print("[等待] 正在探测三引擎就绪状态...")

    while time.time() < deadline:
        if not ready["dws"]:
            try:
                with _open_dws_conn() as c, c.cursor() as cur:
                    cur.execute("SELECT 1;")
                ready["dws"] = True
                print("  [OK] qiming-dws (PostgreSQL 16 @ 127.0.0.1:15432) 已就绪")
            except Exception:
                pass

        if not ready["mysql"]:
            try:
                with _open_mysql_conn() as mc, mc.cursor() as mcur:
                    mcur.execute("SELECT 1;")
                ready["mysql"] = True
                print("  [OK] qiming-mysql (MySQL 8.0 @ 127.0.0.1:13306) 已就绪")
            except Exception:
                pass

        if not ready["inceptor"]:
            try:
                with _open_inceptor_client("default") as ic:
                    ic.execute_query("SELECT 1")
                ready["inceptor"] = True
                print("  [OK] qiming-inceptor (Spark Thrift @ 127.0.0.1:11000) 已就绪")
            except Exception:
                pass

        if all(ready.values()):
            return
        time.sleep(2.5)

    not_ready = [k for k, v in ready.items() if not v]
    raise TimeoutError(f"等待以下容器就绪超时 ({timeout_sec}s): {', '.join(not_ready)}")


def _print_connection_card(anomalies: Optional[Dict[str, Any]] = None) -> None:
    print("\n" + "=" * 72)
    print("  启明商业银行信创数仓演示沙箱 · 连接与暗雷速查卡")
    print("=" * 72)
    print(f"  1) 华为 DWS (仿真)     | db_type=DWS      | {DWS_HOST}:{DWS_PORT}/{DWS_DB}")
    print(f"     账号/密码/默认Schema | {DWS_USER} / {DWS_PASSWORD} / dwd_bank")
    print(f"  2) 星环 Inceptor (仿真) | db_type=INCEPTOR | {INCEPTOR_HOST}:{INCEPTOR_PORT}/{INCEPTOR_DB}")
    print(f"     账号/认证/默认库     | {INCEPTOR_USER} / NOSASL (空密码) / {INCEPTOR_DB}")
    print(f"  3) 核心系统 MySQL      | db_type=MYSQL    | {MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}")
    print(f"     账号/密码            | {MYSQL_USER} / {MYSQL_PASSWORD}")
    if anomalies:
        d = anomalies["dws"]
        m = anomalies["mysql"]
        i = anomalies["inceptor"]
        print("-" * 72)
        print("  五大金融业务暗雷实测核验:")
        print(f"   • 暗雷1a 负余额不良贷款: {d['neg_npl_count']} 笔 (合计 {d['neg_npl_sum']:,.2f} 元)")
        print(f"   • 暗雷1b 逾期>180天错标正常: {d['concealed_npl_count']} 笔 (合计 {d['concealed_npl_sum']:,.2f} 元)")
        print(f"   • 暗雷2  反洗钱拆单(4.95~4.99万): {d['aml_split_count']} 笔 (合计 {d['aml_split_sum']:,.2f} 元, 对手={d['aml_opp_count']} 户)")
        print(f"   • 暗雷3  核心MySQL vs DWS 授信对账差额: {m['recon_gap_vs_dws']:,.2f} 元 (MySQL {m['core_credit_count']} vs DWS {d['dws_credit_count']})")
        print(f"   • 暗雷4  >=1000万零/超低息无押品贷款: {d['zero_rate_unsecured_count']} 笔 (合计 {d['zero_rate_unsecured_sum']:,.2f} 元)")
        print(f"   • 靶表   dwd_loan_contract={d['contract_count']} 行 | dwd_acct_swap_demo={d['swap_demo_count']} 行")
        print(f"   • 星环   bank_inceptor 对象={i['bank_inceptor_objects']} 个 | AML 拆单={i['aml_split_count']} 笔")
    print("=" * 72 + "\n")


def cmd_up(args: argparse.Namespace) -> int:
    if not args.skip_docker:
        print("[1/4] 正在启动启明银行三引擎 Docker 沙箱容器...")
        _compose_cmd(["up", "-d"], registry_prefix=args.registry_prefix or "")
    print("[2/4] 正在等待三引擎就绪...")
    _wait_for_engines(timeout_sec=args.timeout)
    print("[3/4] 正在初始化库表与灌入 25,000+ 行金融样本数据...")
    seed_all()
    print("[4/4] 正在实采活库元数据并生成双 `.dataog.db` 工作区与标准字典 Excel...")
    res = build_all_workspaces(str(DEFAULT_OUTPUT_DIR), live_probe=True)
    anomalies = verify_live_anomalies()
    _print_connection_card(anomalies)
    print(f"产物目录: {res['output_dir']}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    t0 = time.perf_counter()
    targets = ["dws"] if args.dws_only else ["dws", "mysql", "inceptor"]
    print(f"[重置] 正在极速复原 {', '.join(targets)} 活库靶表与数据（不重启容器）...")
    seed_all(targets=targets)
    build_live_sandbox_workspace(str(DEFAULT_OUTPUT_DIR), live_probe=True)
    anomalies = verify_live_anomalies()
    elapsed = time.perf_counter() - t0
    _print_connection_card(anomalies)
    print(f"[OK] 沙箱已恢复到演前初始完美态（耗时 {elapsed:.1f}s）。")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    states = {}
    for name, probe in (
        ("qiming-dws (PG16:15432)", lambda: _open_dws_conn().close()),
        ("qiming-mysql (MySQL8:13306)", lambda: _open_mysql_conn().close()),
        ("qiming-inceptor (Spark:11000)", lambda: _open_inceptor_client("default").close()),
    ):
        try:
            probe()
            states[name] = "UP"
        except Exception as exc:
            states[name] = f"DOWN ({exc})"

    print("沙箱容器实时探测:")
    for k, v in states.items():
        print(f"  - {k}: {v}")

    if all(v == "UP" for v in states.values()):
        anomalies = verify_live_anomalies()
        _print_connection_card(anomalies)
        return 0
    return 1


def cmd_generate_assets(args: argparse.Namespace) -> int:
    out_dir = os.path.abspath(args.output_dir or str(DEFAULT_OUTPUT_DIR))
    print(f"[离线生成] 正在免 Docker 生成金融标准字典 Excel 与双工作区至 {out_dir} ...")
    res = build_all_workspaces(out_dir, live_probe=False)
    print("[OK] 生成完成:")
    print(f"  - 活库工作区: {res['live_workspace']} ({res['live_stats']})")
    print(f"  - 150表工作区: {res['enterprise_workspace']} ({res['enterprise_stats']})")
    return 0


def cmd_bootstrap_demo_home(args: argparse.Namespace) -> int:
    home_dir = os.path.abspath(args.home_dir or str(DEFAULT_DEMO_HOME))
    out_dir = Path(args.output_dir or DEFAULT_OUTPUT_DIR)
    live_ws = out_dir / "qiming_live_sandbox.dataog.db"
    ent_ws = out_dir / "qiming_enterprise_150tables.dataog.db"
    if not live_ws.is_file() or not ent_ws.is_file():
        print("[提示] 尚未生成工作区文件，先执行离线生成...")
        build_all_workspaces(str(out_dir), live_probe=False)

    os.makedirs(home_dir, exist_ok=True)
    ctx = PlatformContext(private_root=home_dir, legacy_data_dir="")

    # 1. 注册 3 组本地沙箱凭据至 Layer 2 私有保险箱（支持跨工作区端点指纹自动匹配）
    for prof in build_demo_connection_profiles(include_secrets=True):
        ctx.store.upsert_connection_profile(prof)

    # 2. 导入两份零密钥工作区（先导入 150 表，再导入并切换为活库工作区）
    ent_res = ctx.workspace_hub.import_workspace_as_new(
        filename=ent_ws.name,
        raw_bytes=ent_ws.read_bytes(),
        name="启明银行 · 全行150表信创中台工作区",
        description="156星环表 × 162 DWS表，适合百表检索与离线大报告演示",
        switch_immediately=False,
    )
    live_res = ctx.workspace_hub.import_workspace_as_new(
        filename=live_ws.name,
        raw_bytes=live_ws.read_bytes(),
        name="启明银行 · 活库现场演练工作区",
        description="直连本地 Docker 华为DWS/星环Inceptor/MySQL 活库",
        switch_immediately=True,
    )
    print(f"[OK] 已向隔离演示目录 {home_dir} 预装双工作区与三引擎连接凭据：")
    print(f"  - 150表工作区 ID: {ent_res['workspace']['workspace_id']}")
    print(f"  - 当前激活工作区 ID: {live_res['workspace']['workspace_id']}")
    print(f"\n启动演示命令:\n  DATAOG_HOME=\"{home_dir}\" python3 app.py")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    extra = ["down", "-v"] if args.volumes else ["down"]
    _compose_cmd(extra)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启明商业银行信创数仓治理演示沙箱运维工具")
    sub = parser.add_subparsers(dest="command", required=True)

    p_up = sub.add_parser("up", help="启动 Docker 三引擎并灌入金融样本数据与工作区")
    p_up.add_argument("--skip-docker", action="store_true", help="跳过 docker compose up，直接探测并灌数")
    p_up.add_argument("--registry-prefix", default="", help="可选 Docker 镜像加速前缀，如 docker.m.daocloud.io/")
    p_up.add_argument("--timeout", type=int, default=120, help="等待容器就绪超时秒数")
    p_up.set_defaults(func=cmd_up)

    p_reset = sub.add_parser("reset", help="不重启容器极速复原活库表结构、业务数据与演练工作区")
    p_reset.add_argument("--dws-only", action="store_true", help="仅极速重置 DWS(PG16) 与演练工作区")
    p_reset.set_defaults(func=cmd_reset)

    p_status = sub.add_parser("status", help="探测三引擎状态并核验五大业务暗雷精确数字")
    p_status.set_defaults(func=cmd_status)

    p_gen = sub.add_parser("generate-assets", help="免 Docker 离线生成标准字典 Excel 与双工作区")
    p_gen.add_argument("--output-dir", default="", help="输出目录，默认 demo_sandbox/output")
    p_gen.set_defaults(func=cmd_generate_assets)

    p_boot = sub.add_parser("bootstrap-demo-home", help="向指定隔离 DATAOG_HOME 预装双工作区与凭据")
    p_boot.add_argument("--home-dir", default="", help="隔离目录，默认 demo_sandbox/.demo_home")
    p_boot.add_argument("--output-dir", default="", help="工作区文件来源目录")
    p_boot.set_defaults(func=cmd_bootstrap_demo_home)

    p_down = sub.add_parser("down", help="关闭演示容器")
    p_down.add_argument("--volumes", action="store_true", help="同时移除数据卷")
    p_down.set_defaults(func=cmd_down)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
