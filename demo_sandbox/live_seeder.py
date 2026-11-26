"""启明商业银行 (Qiming Commercial Bank) 三引擎活库建模、确定性造数与五大业务暗雷注入器.

覆盖三大活库容器：
1. `qiming-dws` (PostgreSQL 16 @ 127.0.0.1:15432, 扮演华为 GaussDB(DWS)):
   - 3 个业务 Schema (`dwd_bank` 14 表 + `dws_bank` 8 表 + `ads_reg` 5 表 + 4 层级联视图 = 31 对象)
   - 约 30,000+ 行确定性金融样本数据（`Random(20261119)`）
   - 含 1 张 100% 合标样板表 (`dwd_org_branch_info`)、1 张 `DIRECT_ALTER` 真改表靶表 (`dwd_loan_contract`)、
     1 张 `TABLE_REBUILD` 链式改名保序靶表 (`dwd_acct_swap_demo`)
2. `qiming-inceptor` (Spark HiveThriftServer2 @ 127.0.0.1:11000, 扮演星环 Transwarp Inceptor):
   - 2 个库 (`bank_inceptor` 默认库 22 表 + 4 层级联视图、`bank_ods_hist` 5 归档表 = 31 对象)
   - 采用 Hive/Parquet DDL 风格 (`STRING` / `DOUBLE` / `DECIMAL(20,4)` / `VARCHAR(200)`) 制造真实跨环境差异
   - 同步灌入全量客户、借据、反洗钱流水与授信样本数据（支持直连星环做 SQL & AI 探查）
3. `qiming-mysql` (MySQL 8.0 @ 127.0.0.1:13306, `core_bank_ods`, 扮演核心交易源库):
   - 10 张 OLTP 核心源表，含比数仓多出 3,650 万元的 3 户未入仓对公授信敞口（暗雷 3）
"""

from __future__ import annotations

import datetime
import os
import random
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import psycopg2
from psycopg2.extras import execute_values
import pymysql

from core.platform.inceptor_client import InceptorThriftClient

DEMO_SEED = 20261119

DWS_HOST = os.environ.get("DEMO_DWS_HOST", "127.0.0.1")
DWS_PORT = int(os.environ.get("DEMO_DWS_PORT", "15432"))
DWS_DB = os.environ.get("DEMO_DWS_DB", "qiming_dws")
DWS_USER = os.environ.get("DEMO_DWS_USER", "dws_admin")
DWS_PASSWORD = os.environ.get("DEMO_DWS_PASSWORD", "DemoDws@2026")

INCEPTOR_HOST = os.environ.get("DEMO_INCEPTOR_HOST", "127.0.0.1")
INCEPTOR_PORT = int(os.environ.get("DEMO_INCEPTOR_PORT", "11000"))
INCEPTOR_DB = os.environ.get("DEMO_INCEPTOR_DB", "bank_inceptor")
INCEPTOR_USER = os.environ.get("DEMO_INCEPTOR_USER", "hive")
INCEPTOR_PASSWORD = os.environ.get("DEMO_INCEPTOR_PASSWORD", "")

MYSQL_HOST = os.environ.get("DEMO_MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.environ.get("DEMO_MYSQL_PORT", "13306"))
MYSQL_DB = os.environ.get("DEMO_MYSQL_DB", "core_bank_ods")
MYSQL_USER = os.environ.get("DEMO_MYSQL_USER", "core_reader")
MYSQL_PASSWORD = os.environ.get("DEMO_MYSQL_PASSWORD", "DemoMysql@2026")


def _open_dws_conn() -> Any:
    return psycopg2.connect(
        host=DWS_HOST,
        port=DWS_PORT,
        dbname=DWS_DB,
        user=DWS_USER,
        password=DWS_PASSWORD,
        connect_timeout=10,
    )


def _open_mysql_conn() -> Any:
    return pymysql.connect(
        host=MYSQL_HOST,
        port=MYSQL_PORT,
        database=MYSQL_DB,
        user=MYSQL_USER,
        password=MYSQL_PASSWORD,
        charset="utf8mb4",
        connect_timeout=10,
        autocommit=False,
    )


def _open_inceptor_client(dbname: str = "default") -> InceptorThriftClient:
    client = InceptorThriftClient(
        host=INCEPTOR_HOST,
        port=INCEPTOR_PORT,
        dbname=dbname,
        user=INCEPTOR_USER,
        password=INCEPTOR_PASSWORD,
        auth_mode="NOSASL",
        timeout=60.0,
    )
    client.connect()
    return client


# =============================================================================
# 确定性业务数据集生成器
# =============================================================================


def generate_banking_dataset() -> Dict[str, Any]:
    """基于固定种子生成启明银行全套对齐数据集与 5 大精准业务暗雷."""
    rng = random.Random(DEMO_SEED)

    # 1. 12 家支行机构
    branches = [
        ("BR001", "启明银行总行营业部", "BR000", "2", "110102", "B0001H111000001", "1"),
        ("BR002", "启明银行滨海分行", "BR001", "2", "120116", "B0001H212000002", "1"),
        ("BR003", "启明银行高新区支行", "BR002", "4", "120116", "B0001S312000003", "1"),
        ("BR004", "启明银行城东支行", "BR002", "4", "120102", "B0001S312000004", "1"),
        ("BR005", "启明银行城西支行", "BR002", "4", "120104", "B0001S312000005", "1"),
        ("BR006", "启明银行自贸区支行", "BR002", "4", "120116", "B0001S312000006", "1"),
        ("BR007", "启明银行江南分行", "BR001", "2", "310115", "B0001H231000007", "1"),
        ("BR008", "启明银行陆家嘴支行", "BR007", "4", "310115", "B0001S331000008", "1"),
        ("BR009", "启明银行科技园支行", "BR007", "4", "310104", "B0001S331000009", "1"),
        ("BR010", "启明银行钱塘支行", "BR007", "4", "330106", "B0001S333000010", "1"),
        ("BR011", "启明银行华南分行", "BR001", "2", "440304", "B0001H244000011", "1"),
        ("BR012", "启明银行前海支行", "BR011", "4", "440305", "B0001S344000012", "1"),
    ]
    branch_ids = [b[0] for b in branches]

    # 2. 60 名客户经理（固定 EMP_042 为城东支行 BR004 对公经理 张明远）
    surnames = list("李王张刘陈杨赵黄周吴徐孙朱马胡郭林何高罗")
    given = ["伟", "敏", "磊", "静", "强", "军", "洋", "勇", "艳", "杰", "明远", "思齐", "宇航", "嘉欣"]
    depts = ["CORP", "RET", "SME", "CORP"]
    ts_base = datetime.datetime(2026, 10, 15, 2, 0, 0)
    managers: List[Tuple[Any, ...]] = []
    for i in range(1, 61):
        eid = f"EMP_{i:03d}"
        if eid == "EMP_042":
            managers.append(("EMP_042", "张明远", "BR004", "CORP", "1", ts_base))
        else:
            br = branch_ids[(i - 1) % len(branch_ids)]
            nm = rng.choice(surnames) + rng.choice(given)
            managers.append((eid, nm, br, depts[i % len(depts)], "1", ts_base))
    mgr_by_branch: Dict[str, List[str]] = {}
    for eid, _nm, br, *_ in managers:
        if eid != "EMP_042":
            mgr_by_branch.setdefault(br, []).append(eid)

    # 3. 2,000 名客户（500 对公 + 1,500 零售）
    corp_prefixes = ["华泰", "锦绣", "宏远", "智联", "盛达", "中科", "蓝海", "永信", "金桥", "博通"]
    corp_suffixes = ["实业有限公司", "供应链有限公司", "科技有限公司", "商贸有限公司", "制造集团有限公司"]
    industries = ["C39", "F51", "I65", "M74", "G54", "K70", "E48", "C26"]
    risk_lvls = ["R1", "R1", "R2", "R2", "R3", "R4"]
    vips = ["MASS", "MASS", "GOLD", "PLAT", "PRIV"]

    customers: List[Tuple[Any, ...]] = []
    corp_exts: List[Tuple[Any, ...]] = []
    for i in range(1, 501):
        cid = f"CUST_C_{i:04d}"
        br = "BR004" if i in (88, 89, 101, 102, 103) else branch_ids[i % len(branch_ids)]
        emp = "EMP_042" if br == "BR004" and i % 2 == 0 else rng.choice(mgr_by_branch.get(br, ["EMP_001"]))
        if i == 88:
            cname = "天津锦绣跨境商贸有限公司"
        elif i == 89:
            cname = "滨海宏远供应链管理有限公司"
        else:
            cname = f"启明{corp_prefixes[i % len(corp_prefixes)]}{i:03d}{corp_suffixes[i % len(corp_suffixes)]}"
        uscc = f"91120116MA0{i:07d}"[:18]
        ind = industries[i % len(industries)]
        open_dt = datetime.date(2019, 1, 1) + datetime.timedelta(days=(i * 11) % 2100)
        customers.append(
            (
                cid,
                cname,
                "01",
                "201",
                uscc,
                "CHN",
                ind,
                rng.choice(risk_lvls),
                f"138{10000000 + i:08d}"[:11],
                f"天津市滨海新区金融示范区路{i}号",
                rng.choice(vips[2:]),
                "01",
                open_dt,
                br,
                emp,
                ts_base,
            )
        )
        corp_exts.append(
            (
                cid,
                uscc,
                rng.choice(["01", "02", "03", "04"]),
                open_dt - datetime.timedelta(days=365 + (i % 1500)),
                round(rng.uniform(500.0, 50000.0) * 10000, 2),
                rng.choice(surnames) + rng.choice(given),
                "1" if i % 9 == 0 else "0",
                f"GRP_{i // 9:03d}" if i % 9 == 0 else "",
                ind,
                br,
            )
        )

    for i in range(1, 1501):
        cid = f"CUST_P_{i:04d}"
        br = branch_ids[i % len(branch_ids)]
        emp = rng.choice(mgr_by_branch.get(br, ["EMP_001"]))
        pname = rng.choice(surnames) + rng.choice(given)
        cert_no = f"12010119{(70 + i % 30):02d}0{(1 + i % 9)}15{1000 + i:04d}"[:18]
        open_dt = datetime.date(2018, 1, 1) + datetime.timedelta(days=(i * 7) % 2400)
        customers.append(
            (
                cid,
                pname,
                "02",
                "101",
                cert_no,
                "CHN",
                "Z99",
                rng.choice(risk_lvls[:4]),
                f"139{20000000 + i:08d}"[:11],
                f"星海区兴业街道{i}号院",
                rng.choice(vips),
                "01",
                open_dt,
                br,
                emp,
                ts_base,
            )
        )

    # 4. 3,200 个存款账户（包含两个 AML 拆单主账户 ACCT_CORP_88001 / ACCT_CORP_88002）
    accounts: List[Tuple[Any, ...]] = [
        (
            "ACCT_CORP_88001",
            "CUST_C_0088",
            "天津锦绣跨境商贸有限公司基本户",
            "BASIC",
            "01",
            datetime.date(2022, 6, 15),
            6850000.00,
            6850000.00,
            0.00,
            "CNY",
            0.003500,
            "BR004",
        ),
        (
            "ACCT_CORP_88002",
            "CUST_C_0089",
            "滨海宏远供应链管理有限公司一般户",
            "GEN",
            "01",
            datetime.date(2023, 3, 20),
            4920000.00,
            4920000.00,
            0.00,
            "CNY",
            0.003500,
            "BR004",
        ),
    ]
    for i in range(1, 3199):
        cust = customers[i % len(customers)]
        cid, cname, ctype = cust[0], cust[1], cust[2]
        br = cust[13]
        ano = f"622890{10000000 + i:010d}"
        bal = round(rng.uniform(2500.0, 4800000.0 if ctype == "01" else 280000.0), 2)
        frozen = round(bal * 0.05, 2) if i % 40 == 0 else 0.00
        accounts.append(
            (
                ano,
                cid,
                f"{cname[:36]}结算户",
                "BASIC" if ctype == "01" else "SAV",
                "01" if i % 50 != 0 else "02",
                cust[12],
                bal,
                round(bal - frozen, 2),
                frozen,
                "CNY",
                0.003500 if i % 3 else 0.016500,
                br,
            )
        )

    # 5. 1,500 份贷款合同（用于 DIRECT_ALTER 靶表 dwd_loan_contract，保证全列可安全转换）
    prod_codes = ["CORP_WC", "CORP_FA", "RET_MTG", "RET_CON"]
    guar_codes = ["01", "02", "03", "04"]
    contracts: List[Tuple[Any, ...]] = []
    for i in range(1, 1501):
        cid = f"CUST_C_{((i - 1) % 500) + 1:04d}" if i <= 750 else f"CUST_P_{((i - 1) % 1500) + 1:04d}"
        br = "BR004" if i <= 60 else branch_ids[i % len(branch_ids)]
        emp = "EMP_042" if br == "BR004" and i <= 25 else rng.choice(mgr_by_branch.get(br, ["EMP_001"]))
        amt = round(rng.uniform(200000.0, 28000000.0 if i <= 750 else 3500000.0), 2)
        rate = round(rng.uniform(0.0365, 0.0620), 4)
        term = rng.choice([6, 12, 24, 36, 60, 120])
        sdt = datetime.date(2023, 1, 5) + datetime.timedelta(days=(i * 3) % 1000)
        contracts.append(
            (
                f"HT2026{i:06d}",
                cid,
                prod_codes[i % len(prod_codes)],
                amt,
                rate,
                term,
                sdt.strftime("%Y-%m-%d"),
                guar_codes[i % len(guar_codes)],
                br,
                emp,
            )
        )

    # 6. 3,000 笔贷款借据（精确注入暗雷 1a, 1b 与暗雷 4）
    receipts: List[Tuple[Any, ...]] = []
    collaterals: List[Tuple[Any, ...]] = []
    repay_plans: List[Tuple[Any, ...]] = []
    zero_loan_contracts: set[str] = set()

    # 6.1 暗雷 1a：7 笔五级分类次级/可疑/损失但余额为负数的坏账（总计 -16,800,000.00）
    neg_specs = [
        ("RCPT_NEG_001", "HT2026000001", "CUST_C_0011", "BR004", "EMP_042", "30", -1800000.00),
        ("RCPT_NEG_002", "HT2026000002", "CUST_C_0012", "BR004", "EMP_042", "40", -2400000.00),
        ("RCPT_NEG_003", "HT2026000003", "CUST_C_0013", "BR004", "EMP_042", "50", -3200000.00),
        ("RCPT_NEG_004", "HT2026000004", "CUST_C_0014", "BR004", "EMP_042", "30", -1500000.00),
        ("RCPT_NEG_005", "HT2026000005", "CUST_C_0015", "BR004", "EMP_042", "40", -2900000.00),
        ("RCPT_NEG_006", "HT2026000006", "CUST_C_0016", "BR004", "EMP_042", "50", -3800000.00),
        ("RCPT_NEG_007", "HT2026000061", "CUST_C_0017", "BR002", "EMP_002", "30", -1200000.00),
    ]
    for idx, (rno, cno, cid, br, emp, fc, bal) in enumerate(neg_specs, start=1):
        vdt = datetime.date(2024, 3, 1) + datetime.timedelta(days=idx * 10)
        receipts.append(
            (
                rno, cno, cid, br, emp, "CORP_WC", "CNY",
                5000000.00, bal, 0.048500, vdt, vdt + datetime.timedelta(days=365),
                fc, 120 + idx * 15, abs(bal), 42000.00, 18500.00, "02", "02",
            )
        )

    # 6.2 暗雷 1b：12 笔逾期 > 180 天但五级分类仍错标为 '10'(正常) 的隐匿不良贷款（总额精确 148,000,000.00）
    hide_specs = [
        ("RCPT_HIDE_001", "HT2026000007", "CUST_C_0021", "BR004", "EMP_042", 18500000.00, 215),
        ("RCPT_HIDE_002", "HT2026000008", "CUST_C_0022", "BR004", "EMP_042", 16000000.00, 240),
        ("RCPT_HIDE_003", "HT2026000009", "CUST_C_0023", "BR004", "EMP_042", 14500000.00, 275),
        ("RCPT_HIDE_004", "HT2026000010", "CUST_C_0024", "BR004", "EMP_042", 13000000.00, 310),
        ("RCPT_HIDE_005", "HT2026000011", "CUST_C_0025", "BR004", "EMP_042", 12500000.00, 198),
        ("RCPT_HIDE_006", "HT2026000012", "CUST_C_0026", "BR004", "EMP_042", 11800000.00, 260),
        ("RCPT_HIDE_007", "HT2026000013", "CUST_C_0027", "BR004", "EMP_042", 11200000.00, 345),
        ("RCPT_HIDE_008", "HT2026000014", "CUST_C_0028", "BR004", "EMP_042", 10800000.00, 222),
        ("RCPT_HIDE_009", "HT2026000015", "CUST_C_0029", "BR004", "EMP_042", 10200000.00, 388),
        ("RCPT_HIDE_010", "HT2026000070", "CUST_C_0030", "BR007", "EMP_007", 10500000.00, 205),
        ("RCPT_HIDE_011", "HT2026000071", "CUST_C_0031", "BR007", "EMP_007", 9800000.00, 255),
        ("RCPT_HIDE_012", "HT2026000072", "CUST_C_0032", "BR007", "EMP_007", 9200000.00, 290),
    ]
    for idx, (rno, cno, cid, br, emp, bal, ovd) in enumerate(hide_specs, start=1):
        vdt = datetime.date(2024, 1, 10) + datetime.timedelta(days=idx * 5)
        receipts.append(
            (
                rno, cno, cid, br, emp, "CORP_FA", "CNY",
                bal + 1500000.00, bal, 0.049500, vdt, datetime.date(2026, 2, 1),
                "10", ovd, round(bal * 0.35, 2), 125000.00, 210000.00, "03", "02",
            )
        )

    # 6.3 暗雷 4：5 笔单笔 >= 1,000 万但执行利率 <= 0.0005（零/违规低息）且无任何押品的对公放款（90,000,000.00）
    zero_specs = [
        ("RCPT_ZERO_001", "HT2026000016", "CUST_C_0041", "BR004", "EMP_042", 12000000.00, 0.000000),
        ("RCPT_ZERO_002", "HT2026000017", "CUST_C_0042", "BR004", "EMP_042", 15000000.00, 0.000100),
        ("RCPT_ZERO_003", "HT2026000081", "CUST_C_0043", "BR008", "EMP_008", 18000000.00, 0.000000),
        ("RCPT_ZERO_004", "HT2026000082", "CUST_C_0044", "BR008", "EMP_008", 20000000.00, 0.000200),
        ("RCPT_ZERO_005", "HT2026000083", "CUST_C_0045", "BR003", "EMP_003", 25000000.00, 0.000000),
    ]
    for idx, (rno, cno, cid, br, emp, amt, rate) in enumerate(zero_specs, start=1):
        zero_loan_contracts.add(cno)
        vdt = datetime.date(2025, 8, 1) + datetime.timedelta(days=idx * 7)
        receipts.append(
            (
                rno, cno, cid, br, emp, "CORP_WC", "CNY",
                amt, amt, rate, vdt, vdt + datetime.timedelta(days=365),
                "10", 0, 0.00, 0.00, 0.00, "01", "01",
            )
        )

    # 6.4 剩余 2,976 笔正常/合规逾期借据
    used_rcpt = len(receipts)
    for i in range(1, 3000 - used_rcpt + 1):
        ct = contracts[i % len(contracts)]
        cno, cid, prod, c_amt, _c_rate, _term, sdt_s, guar, br, emp = ct
        if cno in zero_loan_contracts:
            cno = f"HT2026{(i % 1400) + 100:06d}"
        rno = f"RCPT_NORM_{i:05d}"
        disb = round(min(c_amt, rng.uniform(150000.0, 9500000.0)), 2)
        if i % 80 == 0 and cno not in zero_loan_contracts:
            disb = round(rng.uniform(11000000.0, 22000000.0), 2)
            guar = "03"
        bal = round(disb * rng.uniform(0.35, 0.98), 2)
        rate = round(rng.uniform(0.0345, 0.0640), 6)
        vdt = datetime.date.fromisoformat(sdt_s)
        mdt = vdt + datetime.timedelta(days=365)
        if i % 25 == 0:
            fc = rng.choice(["20", "30", "40"])
            ovd = rng.randint(15, 160) if fc == "20" else rng.randint(95, 260)
            ovd_p = round(bal * 0.2, 2)
            rstat = "02"
        else:
            fc = "10"
            ovd = 0 if i % 19 else rng.randint(1, 20)
            ovd_p = 0.00
            rstat = "01"
        receipts.append(
            (
                rno, cno, cid, br, emp, prod, "CNY",
                disb, bal, rate, vdt, mdt,
                fc, ovd, ovd_p, round(ovd_p * 0.02, 2), 0.00, guar, rstat,
            )
        )
        if len(collaterals) < 1200 and (disb >= 10000000.0 or i % 2 == 0) and cno not in zero_loan_contracts:
            coll_id = f"COLL_{len(collaterals) + 1:05d}"
            eval_v = round(disb * rng.uniform(1.25, 1.85), 2)
            collaterals.append(
                (
                    f"GUAR_{len(collaterals) + 1:05d}",
                    coll_id,
                    cno,
                    cid,
                    rng.choice(["REAL", "LAND", "FIN", "EQP"]),
                    f"标准抵质押资产_{len(collaterals) + 1:04d}",
                    eval_v,
                    0.6500,
                    round(eval_v * 0.65, 2),
                    "01",
                    datetime.date(2026, 4, 15),
                    "1",
                )
            )
        if len(repay_plans) < 3000:
            sched_dt = vdt + datetime.timedelta(days=30 * ((i % 12) + 1))
            sched_p = round(disb / 12.0, 2)
            act_p = sched_p if ovd == 0 else round(sched_p * 0.5, 2)
            repay_plans.append(
                (rno, (i % 12) + 1, sched_dt, sched_p, act_p, round(sched_p * 0.04, 2), "EPI")
            )

    # 补齐担保与还款计划行数至 1,200 / 3,000
    while len(collaterals) < 1200:
        idx = len(collaterals) + 1
        cno = f"HT2026{(idx % 1400) + 100:06d}"
        if cno in zero_loan_contracts:
            cno = "HT2026000500"
        collaterals.append(
            (
                f"GUAR_{idx:05d}", f"COLL_{idx:05d}", cno, f"CUST_C_{(idx % 500) + 1:04d}",
                "REAL", f"商业房产抵押物_{idx:04d}", 4500000.00, 0.6000, 2700000.00, "01",
                datetime.date(2026, 5, 1), "1",
            )
        )
    while len(repay_plans) < 3000:
        idx = len(repay_plans) + 1
        repay_plans.append(
            (
                f"RCPT_NORM_{(idx % 2500) + 1:05d}",
                (idx % 12) + 1,
                datetime.date(2026, 9, 20),
                125000.00,
                125000.00,
                4200.00,
                "EPI",
            )
        )

    # 7. 12,000 笔交易流水（精确注入暗雷 2：48 笔 4.95~4.99 万 AML 拆单 = 2,389,200.00）
    txns: List[Tuple[Any, ...]] = []
    # 48 笔均值 49,775.00（范围 49,591.00 ~ 49,959.00），总额精确 2,389,200.00
    aml_amounts = [round(49775.00 + ((k % 24) - 11.5) * 16.0, 2) for k in range(48)]
    assert len(aml_amounts) == 48 and all(49500.0 <= a < 50000.0 for a in aml_amounts)
    assert round(sum(aml_amounts), 2) == 2389200.00

    aml_dates = [datetime.date(2026, 10, 12), datetime.date(2026, 10, 13), datetime.date(2026, 10, 14)]
    for k in range(48):
        acct = "ACCT_CORP_88001" if k < 24 else "ACCT_CORP_88002"
        cid = "CUST_C_0088" if k < 24 else "CUST_C_0089"
        dt = aml_dates[k % 3]
        ts = datetime.datetime(dt.year, dt.month, dt.day, 9 + (k % 8), (k * 7) % 60, (k * 13) % 60)
        txns.append(
            (
                f"TXN_AML_{k + 1:05d}",
                acct,
                cid,
                dt,
                ts,
                "D",
                aml_amounts[k],
                round(6500000.00 - (k + 1) * 49000.0, 2),
                "EBANK",
                "TRF",
                "货款结算分批划转",
                "OPP_SHELL_999",
                "恒通虚贸易商行",
                "313110000018",
                "2",
                "0",
                "1",
                "BR004",
            )
        )

    normal_opps = [
        ("6214830001001", "京东供应链结算中心"),
        ("6222020002002", "国网华北电力公司"),
        ("6225880003003", "中建基础设施采购户"),
        ("6217000004004", "顺丰速运集团财务户"),
        ("6228480005005", "腾讯云企服结算专户"),
    ]
    channels = ["MBANK", "EBANK", "COUNTER", "POS", "ATM"]
    for i in range(1, 12000 - 48 + 1):
        acct_row = accounts[2 + (i % (len(accounts) - 2))]
        acct_no, cid, _anm, _at, _as, _od, bal, *_br_list = acct_row
        br = acct_row[-1]
        dt = datetime.date(2026, 10, 1) + datetime.timedelta(days=i % 20)
        ts = datetime.datetime(dt.year, dt.month, dt.day, 8 + (i % 12), i % 60, (i * 3) % 60)
        # 确保普通流水绝不落入 [45000, 50000) 拆单监测敏感区间，让 AML 异常清晰无噪音
        amt = round(rng.uniform(120.0, 42000.0), 2) if i % 15 else round(rng.uniform(52000.0, 260000.0), 2)
        large_ind = "1" if amt >= 50000.0 else "0"
        opp_no, opp_nm = normal_opps[i % len(normal_opps)]
        txns.append(
            (
                f"TXN_NRM_{i:06d}",
                acct_no,
                cid,
                dt,
                ts,
                "D" if i % 2 == 0 else "C",
                amt,
                round(max(100.0, bal - amt * 0.1), 2),
                channels[i % len(channels)],
                "TRF" if i % 3 else "PAY",
                "日常经营往来结算",
                opp_no,
                opp_nm,
                "102100099996",
                "2",
                large_ind,
                "0",
                br,
            )
        )

    # 8. 600 条对公授信额度 + 暗雷 3（MySQL 核心多出 3 户 3,650 万元未入仓敞口）
    # 500 个对公客户对应主授信记录（前 497 户同步至 DWS 与 Inceptor，后 3 户 CUST_C_0498~0500 仅存于核心 MySQL，合计精确 36,500,000.00）
    gap_amts = {498: 11500000.00, 499: 12000000.00, 500: 13000000.00}
    mysql_corp_credits: List[Tuple[Any, ...]] = []
    dw_corp_credits: List[Tuple[Any, ...]] = []
    for i in range(1, 501):
        cid = f"CUST_C_{i:04d}"
        br = branch_ids[i % len(branch_ids)]
        if i in gap_amts:
            approved = gap_amts[i]
            used = gap_amts[i]
            # 核心系统脏日期导致 DWS ETL 漏采
            mysql_corp_credits.append(
                (f"CL_CORP_{i:04d}", cid, br, approved, used, datetime.date(2026, 10, 15), "01", "CORP_SYNC_MISS")
            )
        else:
            approved = round(rng.uniform(6000000.0, 12000000.0), 2)
            used = round(approved * 0.65, 2)
            rec = (f"CL_CORP_{i:04d}", cid, br, approved, used, datetime.date(2026, 10, 15), "01", "SYNC_OK")
            mysql_corp_credits.append(rec)
            dw_corp_credits.append(rec)

    credit_limits_extra: List[Tuple[Any, ...]] = []
    for i in range(1, 601):
        cid = f"CUST_C_{((i - 1) % 497) + 1:04d}"
        br = branch_ids[i % len(branch_ids)]
        app = round(rng.uniform(3000000.0, 15000000.0), 2)
        used = round(app * rng.uniform(0.3, 0.85), 2)
        credit_limits_extra.append(
            (f"CR_LMT_{i:05d}", cid, br, app, used, datetime.date(2025, 1, 1), datetime.date(2027, 1, 1), "1")
        )

    # 9. 800 张信用卡账户 + 900 条理财持仓 + 200 条账户互换重建靶表
    cards = [
        (
            f"62598800{i:08d}",
            f"CUST_P_{((i - 1) % 1500) + 1:04d}",
            round(rng.choice([20000.0, 50000.0, 100000.0, 150000.0]), 2),
            round(rng.uniform(800.0, 18000.0), 2),
            "01",
            branch_ids[i % len(branch_ids)],
        )
        for i in range(1, 801)
    ]
    wealths = [
        (
            f"WM_PRD_{(i % 12) + 1:03d}",
            f"CUST_P_{((i - 1) % 1500) + 1:04d}",
            round(rng.uniform(10000.0, 500000.0), 4),
            round(rng.uniform(10500.0, 525000.0), 2),
            datetime.date(2026, 10, 15),
            branch_ids[i % len(branch_ids)],
        )
        for i in range(1, 901)
    ]
    swap_demos = [
        (
            f"62289900{i:06d}",
            f"OLD900{i:06d}",
            f"CUST_P_{((i - 1) % 1500) + 1:04d}",
            round(rng.uniform(5000.0, 150000.0), 2),
            (datetime.date(2024, 1, 1) + datetime.timedelta(days=i)).strftime("%Y-%m-%d"),
            branch_ids[i % len(branch_ids)],
        )
        for i in range(1, 201)
    ]

    return {
        "branches": branches,
        "managers": managers,
        "customers": customers,
        "corp_exts": corp_exts,
        "accounts": accounts,
        "contracts": contracts,
        "receipts": receipts,
        "repay_plans": repay_plans,
        "txns": txns,
        "collaterals": collaterals,
        "credit_limits": credit_limits_extra,
        "cards": cards,
        "wealths": wealths,
        "swap_demos": swap_demos,
        "mysql_corp_credits": mysql_corp_credits,
        "dw_corp_credits": dw_corp_credits,
    }


# =============================================================================
# 1. 华为 GaussDB(DWS) 仿真库 (PostgreSQL 16) 初始化与灌数
# =============================================================================

_DWS_DDL_SQL = """
DROP SCHEMA IF EXISTS ads_reg CASCADE;
DROP SCHEMA IF EXISTS dws_bank CASCADE;
DROP SCHEMA IF EXISTS dwd_bank CASCADE;

CREATE SCHEMA dwd_bank;
CREATE SCHEMA dws_bank;
CREATE SCHEMA ads_reg;

-- 1. 100% 合标样板表：支行机构信息表
CREATE TABLE dwd_bank.dwd_org_branch_info (
    branch_id VARCHAR(16) PRIMARY KEY,
    branch_name VARCHAR(80) NOT NULL,
    parent_branch_id VARCHAR(16) NOT NULL,
    branch_lvl_cd CHAR(1) NOT NULL,
    region_cd CHAR(6) NOT NULL,
    fin_license_no VARCHAR(24) NOT NULL,
    valid_ind CHAR(1) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_org_branch_info IS '支行机构信息表（全表100%落标样板）';
COMMENT ON COLUMN dwd_bank.dwd_org_branch_info.branch_id IS '机构编号';
COMMENT ON COLUMN dwd_bank.dwd_org_branch_info.branch_name IS '机构中文名称';
COMMENT ON COLUMN dwd_bank.dwd_org_branch_info.parent_branch_id IS '上级机构编号';
COMMENT ON COLUMN dwd_bank.dwd_org_branch_info.branch_lvl_cd IS '机构层级代码';
COMMENT ON COLUMN dwd_bank.dwd_org_branch_info.region_cd IS '行政区划代码';
COMMENT ON COLUMN dwd_bank.dwd_org_branch_info.fin_license_no IS '金融许可证号';
COMMENT ON COLUMN dwd_bank.dwd_org_branch_info.valid_ind IS '记录有效标识';

-- 2. 客户经理主数据表
CREATE TABLE dwd_bank.dwd_emp_manager_info (
    emp_id VARCHAR(16) PRIMARY KEY,
    emp_name VARCHAR(48) NOT NULL,
    branch_id VARCHAR(16) NOT NULL,
    dept_cd VARCHAR(16) NOT NULL,
    valid_ind CHAR(1) NOT NULL,
    etl_load_ts TIMESTAMP NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_emp_manager_info IS '客户经理主数据表';
COMMENT ON COLUMN dwd_bank.dwd_emp_manager_info.emp_id IS '客户经理工号';
COMMENT ON COLUMN dwd_bank.dwd_emp_manager_info.emp_name IS '员工姓名';
COMMENT ON COLUMN dwd_bank.dwd_emp_manager_info.branch_id IS '机构编号';
COMMENT ON COLUMN dwd_bank.dwd_emp_manager_info.dept_cd IS '所属条线部门代码';
COMMENT ON COLUMN dwd_bank.dwd_emp_manager_info.valid_ind IS '记录有效标识';
COMMENT ON COLUMN dwd_bank.dwd_emp_manager_info.etl_load_ts IS 'ETL加载时间戳';

-- 3. 客户基础信息表（含老旧字段命名、长度缩水及缺失注释，用于跨环境与落标诊断演示）
CREATE TABLE dwd_bank.dwd_cust_base_info (
    khbh VARCHAR(18) PRIMARY KEY,
    cust_nm VARCHAR(60) NOT NULL,
    cust_type_cd CHAR(2) NOT NULL,
    cert_type_cd VARCHAR(8) NOT NULL,
    zjhm VARCHAR(18) NOT NULL,
    country_cd CHAR(3) NOT NULL,
    industry_cd VARCHAR(10) NOT NULL,
    cust_risk_lvl_cd VARCHAR(4) NOT NULL,
    mobile_no VARCHAR(20) NOT NULL,
    reg_addr VARCHAR(120) NOT NULL,
    vip_tier_cd VARCHAR(8) NOT NULL,
    cust_status_cd CHAR(2) NOT NULL,
    cust_open_dt DATE NOT NULL,
    branch_id VARCHAR(16) NOT NULL,
    emp_id VARCHAR(16) NOT NULL,
    etl_load_ts TIMESTAMP NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_cust_base_info IS '全行客户基础信息明细表（含待治理历史字段）';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.khbh IS '老客户编号';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.cust_nm IS '客户中文简称';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.cust_type_cd IS '客户类型代码';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.zjhm IS '证件号';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.country_cd IS '国别地区代码';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.industry_cd IS '国民经济行业代码';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.cust_risk_lvl_cd IS '客户风险评级代码';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.reg_addr IS '注册或联系地址';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.vip_tier_cd IS '客户价值分层代码';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.cust_status_cd IS '客户状态代码';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.cust_open_dt IS '开户建档日期';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.branch_id IS '机构编号';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.emp_id IS '客户经理工号';
COMMENT ON COLUMN dwd_bank.dwd_cust_base_info.etl_load_ts IS 'ETL加载时间戳';

-- 4. 对公客户扩展信息表
CREATE TABLE dwd_bank.dwd_corp_cust_ext (
    cust_id VARCHAR(32) PRIMARY KEY,
    uscc_cd VARCHAR(18) NOT NULL,
    ent_scale_cd CHAR(2) NOT NULL,
    estab_dt DATE NOT NULL,
    reg_capital_amt NUMERIC(18,2) NOT NULL,
    legal_rep_name VARCHAR(64) NOT NULL,
    group_cust_ind CHAR(1) NOT NULL,
    group_id VARCHAR(32) NOT NULL,
    industry_cd VARCHAR(10) NOT NULL,
    branch_id VARCHAR(16) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_corp_cust_ext IS '对公客户扩展信息明细表';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.cust_id IS '客户统一编号';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.uscc_cd IS '统一社会信用代码';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.ent_scale_cd IS '企业规模代码';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.estab_dt IS '企业成立日期';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.reg_capital_amt IS '注册资本金额';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.legal_rep_name IS '法定代表人姓名';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.group_cust_ind IS '集团客户标识';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.group_id IS '所属集团编号';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.industry_cd IS '国民经济行业代码';
COMMENT ON COLUMN dwd_bank.dwd_corp_cust_ext.branch_id IS '机构编号';

-- 5. 存款账户主档表
CREATE TABLE dwd_bank.dwd_acct_deposit_info (
    acct_no VARCHAR(32) PRIMARY KEY,
    cust_id VARCHAR(32) NOT NULL,
    acct_name VARCHAR(120) NOT NULL,
    acct_type_cd VARCHAR(8) NOT NULL,
    acct_status_cd CHAR(2) NOT NULL,
    open_dt DATE NOT NULL,
    acct_bal_amt NUMERIC(18,2) NOT NULL,
    avail_bal_amt NUMERIC(18,2) NOT NULL,
    frozen_amt NUMERIC(18,2) NOT NULL,
    currency_cd CHAR(3) NOT NULL,
    dep_int_rate NUMERIC(10,6) NOT NULL,
    branch_id VARCHAR(16) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_acct_deposit_info IS '存款账户主档明细表';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.acct_no IS '银行账号';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.cust_id IS '客户统一编号';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.acct_name IS '账户户名';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.acct_type_cd IS '账户类型代码';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.acct_status_cd IS '账户状态代码';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.open_dt IS '开户日期';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.acct_bal_amt IS '账户活期余额';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.avail_bal_amt IS '账户可用余额';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.frozen_amt IS '冻结金额';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.currency_cd IS '币种代码';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.dep_int_rate IS '存款利率';
COMMENT ON COLUMN dwd_bank.dwd_acct_deposit_info.branch_id IS '机构编号';

-- 6. 贷款合同明细表（DIRECT_ALTER 现场真改表最佳靶表：1,500 行安全可转）
CREATE TABLE dwd_bank.dwd_loan_contract (
    htbh VARCHAR(24) PRIMARY KEY,
    khbh VARCHAR(18) NOT NULL,
    loan_prod_cd VARCHAR(16) NOT NULL,
    dkje NUMERIC(14,2) NOT NULL,
    zxlv NUMERIC(8,4) NOT NULL,
    dkqx SMALLINT NOT NULL,
    sign_dt_str VARCHAR(10) NOT NULL,
    main_guar_type_cd CHAR(2) NOT NULL,
    branch_id VARCHAR(16) NOT NULL,
    emp_id VARCHAR(16) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_loan_contract IS '贷款合同明细表（DIRECT_ALTER 贯标改表演示靶表）';
COMMENT ON COLUMN dwd_bank.dwd_loan_contract.htbh IS '老合同号';
COMMENT ON COLUMN dwd_bank.dwd_loan_contract.khbh IS '客户号';
COMMENT ON COLUMN dwd_bank.dwd_loan_contract.dkje IS '合同金额';
COMMENT ON COLUMN dwd_bank.dwd_loan_contract.zxlv IS '利率';
COMMENT ON COLUMN dwd_bank.dwd_loan_contract.dkqx IS '期限';
COMMENT ON COLUMN dwd_bank.dwd_loan_contract.sign_dt_str IS '签约日期文本';
COMMENT ON COLUMN dwd_bank.dwd_loan_contract.main_guar_type_cd IS '主要担保方式代码';
COMMENT ON COLUMN dwd_bank.dwd_loan_contract.branch_id IS '机构编号';
COMMENT ON COLUMN dwd_bank.dwd_loan_contract.emp_id IS '客户经理工号';

-- 7. 贷款借据明细表（AI 探查暗雷 1 & 4 主战场）
CREATE TABLE dwd_bank.dwd_loan_receipt (
    loan_receipt_no VARCHAR(36) PRIMARY KEY,
    loan_contract_no VARCHAR(36) NOT NULL,
    cust_id VARCHAR(32) NOT NULL,
    branch_id VARCHAR(16) NOT NULL,
    emp_id VARCHAR(16) NOT NULL,
    loan_prod_cd VARCHAR(16) NOT NULL,
    currency_cd CHAR(3) NOT NULL,
    disburse_amt NUMERIC(18,2) NOT NULL,
    loan_bal_amt NUMERIC(18,2) NOT NULL,
    exec_int_rate NUMERIC(10,6) NOT NULL,
    value_dt DATE NOT NULL,
    maturity_dt DATE NOT NULL,
    five_class_cd CHAR(2) NOT NULL,
    overdue_days INTEGER NOT NULL,
    overdue_prin_amt NUMERIC(18,2) NOT NULL,
    on_bs_int_amt NUMERIC(18,2) NOT NULL,
    off_bs_int_amt NUMERIC(18,2) NOT NULL,
    main_guar_type_cd CHAR(2) NOT NULL,
    receipt_status_cd CHAR(2) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_loan_receipt IS '贷款借据明细表';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.loan_receipt_no IS '贷款借据编号';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.loan_contract_no IS '贷款合同编号';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.cust_id IS '客户统一编号';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.branch_id IS '机构编号';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.emp_id IS '客户经理工号';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.loan_prod_cd IS '贷款产品代码';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.currency_cd IS '币种代码';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.disburse_amt IS '放款发放金额';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.loan_bal_amt IS '贷款本金余额';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.exec_int_rate IS '执行年利率';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.value_dt IS '放款起息日期';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.maturity_dt IS '贷款到期日期';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.five_class_cd IS '五级分类代码';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.overdue_days IS '逾期天数';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.overdue_prin_amt IS '逾期本金金额';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.on_bs_int_amt IS '表内欠息金额';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.off_bs_int_amt IS '表外欠息金额';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.main_guar_type_cd IS '主要担保方式代码';
COMMENT ON COLUMN dwd_bank.dwd_loan_receipt.receipt_status_cd IS '借据核算状态代码';

-- 8. 贷款还款计划表
CREATE TABLE dwd_bank.dwd_loan_repay_plan (
    id SERIAL PRIMARY KEY,
    loan_receipt_no VARCHAR(36) NOT NULL,
    repay_term_no INTEGER NOT NULL,
    sched_repay_dt DATE NOT NULL,
    sched_prin_amt NUMERIC(18,2) NOT NULL,
    actual_prin_amt NUMERIC(18,2) NOT NULL,
    int_income_amt NUMERIC(18,2) NOT NULL,
    repay_method_cd VARCHAR(8) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_loan_repay_plan IS '贷款还款计划明细表';
COMMENT ON COLUMN dwd_bank.dwd_loan_repay_plan.loan_receipt_no IS '贷款借据编号';
COMMENT ON COLUMN dwd_bank.dwd_loan_repay_plan.repay_term_no IS '还款期次';
COMMENT ON COLUMN dwd_bank.dwd_loan_repay_plan.sched_repay_dt IS '应还日期';
COMMENT ON COLUMN dwd_bank.dwd_loan_repay_plan.sched_prin_amt IS '本期应还本金';
COMMENT ON COLUMN dwd_bank.dwd_loan_repay_plan.actual_prin_amt IS '本期实还本金';
COMMENT ON COLUMN dwd_bank.dwd_loan_repay_plan.int_income_amt IS '利息收入金额';
COMMENT ON COLUMN dwd_bank.dwd_loan_repay_plan.repay_method_cd IS '还款方式代码';

-- 9. 核心交易流水明细表（AI 探查暗雷 2 反洗钱拆单主战场）
CREATE TABLE dwd_bank.dwd_txn_detail (
    txn_serial_no VARCHAR(40) PRIMARY KEY,
    acct_no VARCHAR(32) NOT NULL,
    cust_id VARCHAR(32) NOT NULL,
    txn_dt DATE NOT NULL,
    txn_ts TIMESTAMP NOT NULL,
    dc_flag_cd CHAR(1) NOT NULL,
    txn_amt NUMERIC(18,2) NOT NULL,
    after_bal_amt NUMERIC(18,2) NOT NULL,
    txn_channel_cd VARCHAR(12) NOT NULL,
    txn_type_cd VARCHAR(12) NOT NULL,
    txn_summary VARCHAR(200) NOT NULL,
    opp_acct_no VARCHAR(36) NOT NULL,
    opp_acct_name VARCHAR(120) NOT NULL,
    opp_bank_cd VARCHAR(16) NOT NULL,
    cash_trf_ind CHAR(1) NOT NULL,
    large_txn_ind CHAR(1) NOT NULL,
    aml_suspect_ind CHAR(1) NOT NULL,
    branch_id VARCHAR(16) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_txn_detail IS '核心交易流水明细表（含反洗钱拆分暗雷样本）';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.txn_serial_no IS '交易流水号';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.acct_no IS '银行账号';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.cust_id IS '客户统一编号';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.txn_dt IS '交易日期';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.txn_ts IS '交易发生时间戳';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.dc_flag_cd IS '借贷标志代码';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.txn_amt IS '交易发生金额';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.after_bal_amt IS '交易后账户余额';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.txn_channel_cd IS '交易渠道代码';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.txn_type_cd IS '交易类型代码';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.txn_summary IS '交易摘要说明';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.opp_acct_no IS '对手方账号';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.opp_acct_name IS '对手方户名';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.opp_bank_cd IS '对手方开户行号';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.cash_trf_ind IS '现转标志代码';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.large_txn_ind IS '大额交易上报标识';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.aml_suspect_ind IS '可疑拆单预警标识';
COMMENT ON COLUMN dwd_bank.dwd_txn_detail.branch_id IS '机构编号';

-- 10. 担保押品信息表
CREATE TABLE dwd_bank.dwd_collateral_info (
    guar_contract_no VARCHAR(36) NOT NULL,
    collateral_id VARCHAR(32) PRIMARY KEY,
    loan_contract_no VARCHAR(36) NOT NULL,
    cust_id VARCHAR(32) NOT NULL,
    coll_type_cd VARCHAR(8) NOT NULL,
    coll_name VARCHAR(120) NOT NULL,
    eval_value_amt NUMERIC(18,2) NOT NULL,
    pledge_ratio NUMERIC(8,4) NOT NULL,
    guar_cover_amt NUMERIC(18,2) NOT NULL,
    coll_status_cd CHAR(2) NOT NULL,
    reval_dt DATE NOT NULL,
    qualified_mitigant_ind CHAR(1) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_collateral_info IS '担保押品信息明细表';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.guar_contract_no IS '担保合同编号';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.collateral_id IS '押品统一编号';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.loan_contract_no IS '贷款合同编号';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.cust_id IS '客户统一编号';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.coll_type_cd IS '押品大类代码';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.coll_name IS '押品名称描述';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.eval_value_amt IS '押品评估价值';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.pledge_ratio IS '核定抵质押率';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.guar_cover_amt IS '担保覆盖金额';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.coll_status_cd IS '押品登记状态代码';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.reval_dt IS '最近重估日期';
COMMENT ON COLUMN dwd_bank.dwd_collateral_info.qualified_mitigant_ind IS '合格缓释工具标识';

-- 11. 对公授信额度表
CREATE TABLE dwd_bank.dwd_credit_limit (
    credit_limit_id VARCHAR(32) PRIMARY KEY,
    cust_id VARCHAR(32) NOT NULL,
    branch_id VARCHAR(16) NOT NULL,
    approved_limit_amt NUMERIC(18,2) NOT NULL,
    used_limit_amt NUMERIC(18,2) NOT NULL,
    value_dt DATE NOT NULL,
    maturity_dt DATE NOT NULL,
    valid_ind CHAR(1) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_credit_limit IS '对公综合授信额度表';
COMMENT ON COLUMN dwd_bank.dwd_credit_limit.credit_limit_id IS '授信额度编号';
COMMENT ON COLUMN dwd_bank.dwd_credit_limit.cust_id IS '客户统一编号';
COMMENT ON COLUMN dwd_bank.dwd_credit_limit.branch_id IS '机构编号';
COMMENT ON COLUMN dwd_bank.dwd_credit_limit.approved_limit_amt IS '核定授信总额';
COMMENT ON COLUMN dwd_bank.dwd_credit_limit.used_limit_amt IS '已用授信额度';
COMMENT ON COLUMN dwd_bank.dwd_credit_limit.value_dt IS '放款起息日期';
COMMENT ON COLUMN dwd_bank.dwd_credit_limit.maturity_dt IS '贷款到期日期';
COMMENT ON COLUMN dwd_bank.dwd_credit_limit.valid_ind IS '记录有效标识';

-- 12. 信用卡账户表
CREATE TABLE dwd_bank.dwd_credit_card_acct (
    card_no VARCHAR(24) PRIMARY KEY,
    cust_id VARCHAR(32) NOT NULL,
    card_limit_amt NUMERIC(18,2) NOT NULL,
    card_used_amt NUMERIC(18,2) NOT NULL,
    acct_status_cd CHAR(2) NOT NULL,
    branch_id VARCHAR(16) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_credit_card_acct IS '信用卡账户表';

-- 13. 理财产品持仓表
CREATE TABLE dwd_bank.dwd_wealth_holding (
    id SERIAL PRIMARY KEY,
    wealth_prod_cd VARCHAR(24) NOT NULL,
    cust_id VARCHAR(32) NOT NULL,
    wealth_share_qty NUMERIC(18,4) NOT NULL,
    wealth_mkt_val_amt NUMERIC(18,2) NOT NULL,
    stat_dt DATE NOT NULL,
    branch_id VARCHAR(16) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_wealth_holding IS '个人与机构理财持仓明细表';

-- 14. TABLE_REBUILD 链式改名保序重建演示靶表
CREATE TABLE dwd_bank.dwd_acct_swap_demo (
    acct_code VARCHAR(24) NOT NULL,
    acct_no VARCHAR(24) NOT NULL,
    cust_id VARCHAR(32) NOT NULL,
    acct_bal_amt NUMERIC(14,2) NOT NULL,
    open_dt_txt VARCHAR(10) NOT NULL,
    branch_id VARCHAR(16) NOT NULL
);
COMMENT ON TABLE dwd_bank.dwd_acct_swap_demo IS '账户链式改名与保序重建演示靶表（TABLE_REBUILD）';
COMMENT ON COLUMN dwd_bank.dwd_acct_swap_demo.acct_code IS '新主账号候选';
COMMENT ON COLUMN dwd_bank.dwd_acct_swap_demo.acct_no IS '历史老账号';
COMMENT ON COLUMN dwd_bank.dwd_acct_swap_demo.cust_id IS '客户统一编号';
COMMENT ON COLUMN dwd_bank.dwd_acct_swap_demo.acct_bal_amt IS '余额';
COMMENT ON COLUMN dwd_bank.dwd_acct_swap_demo.open_dt_txt IS '开户日期字符';
COMMENT ON COLUMN dwd_bank.dwd_acct_swap_demo.branch_id IS '机构编号';

-- ==================== DWS 主题汇总层 (8 表) ====================
CREATE TABLE dws_bank.dws_cust_panoramic_360 (
    cust_id VARCHAR(32) PRIMARY KEY,
    cust_name VARCHAR(120) NOT NULL,
    cust_type_cd CHAR(2) NOT NULL,
    vip_tier_cd VARCHAR(8) NOT NULL,
    acct_bal_amt NUMERIC(18,2) NOT NULL,
    loan_bal_amt NUMERIC(18,2) NOT NULL,
    branch_id VARCHAR(16) NOT NULL,
    stat_dt DATE NOT NULL
);
COMMENT ON TABLE dws_bank.dws_cust_panoramic_360 IS '全行客户资产负债全景宽表';

CREATE TABLE dws_bank.dws_corp_credit_exposure (
    credit_limit_id VARCHAR(32) PRIMARY KEY,
    cust_id VARCHAR(32) NOT NULL,
    branch_id VARCHAR(16) NOT NULL,
    approved_limit_amt NUMERIC(18,2) NOT NULL,
    used_limit_amt NUMERIC(18,2) NOT NULL,
    stat_dt DATE NOT NULL,
    acct_status_cd CHAR(2) NOT NULL,
    sync_tag VARCHAR(24) NOT NULL
);
COMMENT ON TABLE dws_bank.dws_corp_credit_exposure IS '对公授信风险敞口汇总表（相对核心存在3户未入仓差异）';

CREATE TABLE dws_bank.dws_retail_loan_risk_m (
    stat_dt DATE NOT NULL,
    branch_id VARCHAR(16) NOT NULL,
    loan_bal_amt NUMERIC(18,2) NOT NULL,
    npl_bal_amt NUMERIC(18,2) NOT NULL,
    npl_ratio NUMERIC(8,4) NOT NULL
);
COMMENT ON TABLE dws_bank.dws_retail_loan_risk_m IS '零售贷款资产质量月度汇总表';

CREATE TABLE dws_bank.dws_branch_deposit_loan_d (
    stat_dt DATE NOT NULL,
    branch_id VARCHAR(16) PRIMARY KEY,
    branch_name VARCHAR(80) NOT NULL,
    acct_bal_amt NUMERIC(18,2) NOT NULL,
    loan_bal_amt NUMERIC(18,2) NOT NULL,
    loan_to_dep_ratio NUMERIC(8,4) NOT NULL
);
COMMENT ON TABLE dws_bank.dws_branch_deposit_loan_d IS '分支机构存贷比日汇总表';

CREATE TABLE dws_bank.dws_overdue_five_class_d (
    stat_dt DATE NOT NULL,
    five_class_cd CHAR(2) NOT NULL,
    overdue_bucket_cd VARCHAR(8) NOT NULL,
    loan_bal_amt NUMERIC(18,2) NOT NULL,
    overdue_prin_amt NUMERIC(18,2) NOT NULL
);
COMMENT ON TABLE dws_bank.dws_overdue_five_class_d IS '逾期账龄与五级分类交叉汇总表';

CREATE TABLE dws_bank.dws_aml_large_txn_alert (
    aml_case_id VARCHAR(32) PRIMARY KEY,
    acct_no VARCHAR(32) NOT NULL,
    cust_id VARCHAR(32) NOT NULL,
    split_3d_cnt INTEGER NOT NULL,
    daily_txn_amt NUMERIC(18,2) NOT NULL,
    alert_level_cd VARCHAR(8) NOT NULL,
    alert_rule_cd VARCHAR(24) NOT NULL,
    stat_dt DATE NOT NULL
);
COMMENT ON TABLE dws_bank.dws_aml_large_txn_alert IS '反洗钱大额与拆单可疑交易监控表';

CREATE TABLE dws_bank.dws_cust_value_tier (
    vip_tier_cd VARCHAR(8) PRIMARY KEY,
    cust_cnt INTEGER NOT NULL,
    acct_bal_amt NUMERIC(18,2) NOT NULL,
    wealth_mkt_val_amt NUMERIC(18,2) NOT NULL
);
COMMENT ON TABLE dws_bank.dws_cust_value_tier IS '全行客户价值分层统计表';

CREATE TABLE dws_bank.dws_interest_pricing_anly (
    branch_id VARCHAR(16) NOT NULL,
    loan_prod_cd VARCHAR(16) NOT NULL,
    avg_exec_rate NUMERIC(10,6) NOT NULL,
    lpr_base_rate NUMERIC(10,6) NOT NULL,
    rate_spread_bp NUMERIC(10,2) NOT NULL,
    disburse_amt NUMERIC(18,2) NOT NULL
);
COMMENT ON TABLE dws_bank.dws_interest_pricing_anly IS '贷款利率定价偏离分析表';

-- ==================== ADS 监管报送层 (5 表 + 4 层级联视图) ====================
CREATE TABLE ads_reg.ads_east_cust_info (
    east_batch_no VARCHAR(24) NOT NULL,
    cust_id VARCHAR(32) PRIMARY KEY,
    cust_name VARCHAR(120) NOT NULL,
    cert_type_cd VARCHAR(8) NOT NULL,
    cert_no VARCHAR(32) NOT NULL,
    branch_id VARCHAR(16) NOT NULL
);
COMMENT ON TABLE ads_reg.ads_east_cust_info IS 'EAST 5.0 客户信息报送表';

CREATE TABLE ads_reg.ads_east_loan_detail (
    east_batch_no VARCHAR(24) NOT NULL,
    loan_receipt_no VARCHAR(36) PRIMARY KEY,
    cust_id VARCHAR(32) NOT NULL,
    loan_bal_amt NUMERIC(18,2) NOT NULL,
    five_class_cd CHAR(2) NOT NULL,
    overdue_days INTEGER NOT NULL,
    dq_anomaly_ind CHAR(1) NOT NULL
);
COMMENT ON TABLE ads_reg.ads_east_loan_detail IS 'EAST 5.0 贷款借据报送表';

CREATE TABLE ads_reg.ads_east_large_txn (
    east_batch_no VARCHAR(24) NOT NULL,
    txn_serial_no VARCHAR(40) PRIMARY KEY,
    acct_no VARCHAR(32) NOT NULL,
    txn_amt NUMERIC(18,2) NOT NULL,
    opp_acct_name VARCHAR(120) NOT NULL,
    large_txn_ind CHAR(1) NOT NULL
);
COMMENT ON TABLE ads_reg.ads_east_large_txn IS 'EAST 5.0 大额及可疑交易报送表';

CREATE TABLE ads_reg.ads_1104_risk_exposure (
    reg_ind_cd VARCHAR(20) NOT NULL,
    branch_id VARCHAR(16) NOT NULL,
    risk_exposure_amt NUMERIC(18,2) NOT NULL,
    npl_bal_amt NUMERIC(18,2) NOT NULL,
    provision_cov_ratio NUMERIC(10,4) NOT NULL
);
COMMENT ON TABLE ads_reg.ads_1104_risk_exposure IS '1104 非现场监管信用风险敞口报表';

CREATE TABLE ads_reg.ads_aml_suspect_case (
    aml_case_id VARCHAR(32) PRIMARY KEY,
    cust_id VARCHAR(32) NOT NULL,
    acct_no VARCHAR(32) NOT NULL,
    anomaly_reason VARCHAR(200) NOT NULL,
    alert_level_cd VARCHAR(8) NOT NULL
);
COMMENT ON TABLE ads_reg.ads_aml_suspect_case IS '反洗钱可疑案件甄别报送表';

-- 4 层级联监管视图（供四层视图血缘穿透与多邻国方言互译演示）
CREATE OR REPLACE VIEW ads_reg.v_l1_loan_base AS
SELECT
    r.loan_receipt_no,
    r.cust_id,
    r.branch_id,
    r.emp_id,
    r.loan_bal_amt,
    r.exec_int_rate,
    r.five_class_cd,
    r.overdue_days,
    r.main_guar_type_cd
FROM dwd_bank.dwd_loan_receipt r
WHERE r.receipt_status_cd IN ('01', '02');

CREATE OR REPLACE VIEW ads_reg.v_l2_loan_overdue AS
SELECT
    b.loan_receipt_no,
    b.cust_id,
    b.branch_id,
    b.emp_id,
    b.loan_bal_amt,
    b.five_class_cd,
    b.overdue_days,
    CASE WHEN b.overdue_days > 180 AND b.five_class_cd = '10' THEN '1' ELSE '0' END AS dq_anomaly_ind
FROM ads_reg.v_l1_loan_base b
WHERE b.overdue_days > 0 OR b.loan_bal_amt < 0;

CREATE OR REPLACE VIEW ads_reg.v_l3_branch_risk AS
SELECT
    o.branch_id,
    COUNT(*) AS anomaly_loan_cnt,
    SUM(o.loan_bal_amt) AS risk_exposure_amt,
    SUM(CASE WHEN o.dq_anomaly_ind = '1' THEN o.loan_bal_amt ELSE 0 END) AS concealed_npl_amt
FROM ads_reg.v_l2_loan_overdue o
GROUP BY o.branch_id;

CREATE OR REPLACE VIEW ads_reg.v_l4_east_credit_summary AS
SELECT
    br.branch_id,
    br.branch_name,
    COALESCE(r.anomaly_loan_cnt, 0) AS anomaly_loan_cnt,
    COALESCE(r.risk_exposure_amt, 0) AS risk_exposure_amt,
    COALESCE(r.concealed_npl_amt, 0) AS concealed_npl_amt
FROM dwd_bank.dwd_org_branch_info br
LEFT JOIN ads_reg.v_l3_branch_risk r ON r.branch_id = br.branch_id;
"""


def seed_dws(dataset: Dict[str, Any], progress_fn: Callable[[str], None] = print) -> Dict[str, int]:
    """初始化并灌入华为 DWS 仿真库 (PostgreSQL 16)."""
    progress_fn("[DWS/PG16] 正在重建 Schema 与 27 表 + 4 层视图结构...")
    with _open_dws_conn() as conn:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(_DWS_DDL_SQL)

            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_org_branch_info VALUES %s",
                dataset["branches"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_emp_manager_info VALUES %s",
                dataset["managers"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_cust_base_info VALUES %s",
                dataset["customers"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_corp_cust_ext VALUES %s",
                dataset["corp_exts"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_acct_deposit_info VALUES %s",
                dataset["accounts"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_loan_contract VALUES %s",
                dataset["contracts"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_loan_receipt VALUES %s",
                dataset["receipts"],
            )
            execute_values(
                cur,
                """INSERT INTO dwd_bank.dwd_loan_repay_plan
                (loan_receipt_no, repay_term_no, sched_repay_dt, sched_prin_amt, actual_prin_amt, int_income_amt, repay_method_cd)
                VALUES %s""",
                dataset["repay_plans"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_txn_detail VALUES %s",
                dataset["txns"],
                page_size=1000,
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_collateral_info VALUES %s",
                dataset["collaterals"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_credit_limit VALUES %s",
                dataset["credit_limits"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_credit_card_acct VALUES %s",
                dataset["cards"],
            )
            execute_values(
                cur,
                """INSERT INTO dwd_bank.dwd_wealth_holding
                (wealth_prod_cd, cust_id, wealth_share_qty, wealth_mkt_val_amt, stat_dt, branch_id)
                VALUES %s""",
                dataset["wealths"],
            )
            execute_values(
                cur,
                "INSERT INTO dwd_bank.dwd_acct_swap_demo VALUES %s",
                dataset["swap_demos"],
            )

            # DWS 汇总层灌入（通过聚合与同步数据集生成）
            execute_values(
                cur,
                "INSERT INTO dws_bank.dws_corp_credit_exposure VALUES %s",
                dataset["dw_corp_credits"],
            )
            cur.execute(
                """
                INSERT INTO dws_bank.dws_cust_panoramic_360
                SELECT
                    c.khbh,
                    c.cust_nm,
                    c.cust_type_cd,
                    c.vip_tier_cd,
                    COALESCE(a.sum_bal, 0),
                    COALESCE(l.sum_bal, 0),
                    c.branch_id,
                    DATE '2026-10-15'
                FROM dwd_bank.dwd_cust_base_info c
                LEFT JOIN (
                    SELECT cust_id, SUM(acct_bal_amt) AS sum_bal FROM dwd_bank.dwd_acct_deposit_info GROUP BY cust_id
                ) a ON a.cust_id = c.khbh
                LEFT JOIN (
                    SELECT cust_id, SUM(loan_bal_amt) AS sum_bal FROM dwd_bank.dwd_loan_receipt GROUP BY cust_id
                ) l ON l.cust_id = c.khbh;
                """
            )
            cur.execute(
                """
                INSERT INTO dws_bank.dws_branch_deposit_loan_d
                SELECT
                    DATE '2026-10-15',
                    b.branch_id,
                    b.branch_name,
                    COALESCE(a.dep_bal, 0),
                    COALESCE(l.ln_bal, 0),
                    CASE WHEN COALESCE(a.dep_bal, 0) > 0
                         THEN ROUND(COALESCE(l.ln_bal, 0) / a.dep_bal, 4) ELSE 0 END
                FROM dwd_bank.dwd_org_branch_info b
                LEFT JOIN (SELECT branch_id, SUM(acct_bal_amt) AS dep_bal FROM dwd_bank.dwd_acct_deposit_info GROUP BY branch_id) a
                    ON a.branch_id = b.branch_id
                LEFT JOIN (SELECT branch_id, SUM(loan_bal_amt) AS ln_bal FROM dwd_bank.dwd_loan_receipt GROUP BY branch_id) l
                    ON l.branch_id = b.branch_id;
                """
            )
            cur.execute(
                """
                INSERT INTO dws_bank.dws_retail_loan_risk_m
                SELECT
                    DATE '2026-10-15',
                    branch_id,
                    SUM(loan_bal_amt),
                    SUM(CASE WHEN five_class_cd IN ('30', '40', '50') THEN loan_bal_amt ELSE 0 END),
                    CASE WHEN SUM(loan_bal_amt) > 0
                         THEN ROUND(SUM(CASE WHEN five_class_cd IN ('30', '40', '50') THEN loan_bal_amt ELSE 0 END) / SUM(loan_bal_amt), 4)
                         ELSE 0 END
                FROM dwd_bank.dwd_loan_receipt
                GROUP BY branch_id;
                """
            )
            cur.execute(
                """
                INSERT INTO dws_bank.dws_overdue_five_class_d
                SELECT
                    DATE '2026-10-15',
                    five_class_cd,
                    CASE
                        WHEN overdue_days = 0 THEN 'M0'
                        WHEN overdue_days <= 30 THEN 'M1'
                        WHEN overdue_days <= 90 THEN 'M2'
                        WHEN overdue_days <= 180 THEN 'M3'
                        ELSE 'M6+'
                    END,
                    SUM(loan_bal_amt),
                    SUM(overdue_prin_amt)
                FROM dwd_bank.dwd_loan_receipt
                GROUP BY 2, 3;
                """
            )
            cur.execute(
                """
                INSERT INTO dws_bank.dws_aml_large_txn_alert
                VALUES
                ('AML_CASE_2026_001', 'ACCT_CORP_88001', 'CUST_C_0088', 24, 1194600.00, 'CRIT', 'RULE_SPLIT_5W', DATE '2026-10-14'),
                ('AML_CASE_2026_002', 'ACCT_CORP_88002', 'CUST_C_0089', 24, 1194600.00, 'CRIT', 'RULE_SPLIT_5W', DATE '2026-10-14');
                """
            )
            cur.execute(
                """
                INSERT INTO dws_bank.dws_cust_value_tier
                SELECT
                    c.vip_tier_cd,
                    COUNT(*),
                    COALESCE(SUM(p.acct_bal_amt), 0),
                    COALESCE(SUM(w.w_amt), 0)
                FROM dwd_bank.dwd_cust_base_info c
                LEFT JOIN dws_bank.dws_cust_panoramic_360 p ON p.cust_id = c.khbh
                LEFT JOIN (SELECT cust_id, SUM(wealth_mkt_val_amt) AS w_amt FROM dwd_bank.dwd_wealth_holding GROUP BY cust_id) w
                    ON w.cust_id = c.khbh
                GROUP BY c.vip_tier_cd;
                """
            )
            cur.execute(
                """
                INSERT INTO dws_bank.dws_interest_pricing_anly
                SELECT
                    branch_id,
                    loan_prod_cd,
                    ROUND(AVG(exec_int_rate), 6),
                    0.034500,
                    ROUND((AVG(exec_int_rate) - 0.034500) * 10000, 2),
                    SUM(disburse_amt)
                FROM dwd_bank.dwd_loan_receipt
                GROUP BY branch_id, loan_prod_cd;
                """
            )

            # ADS 监管层灌入
            cur.execute(
                """
                INSERT INTO ads_reg.ads_east_cust_info
                SELECT 'EAST_20261015', khbh, cust_nm, cert_type_cd, zjhm, branch_id
                FROM dwd_bank.dwd_cust_base_info
                LIMIT 500;
                """
            )
            cur.execute(
                """
                INSERT INTO ads_reg.ads_east_loan_detail
                SELECT 'EAST_20261015', loan_receipt_no, cust_id, loan_bal_amt, five_class_cd, overdue_days,
                       CASE WHEN loan_bal_amt < 0 OR (overdue_days > 180 AND five_class_cd = '10') THEN '1' ELSE '0' END
                FROM dwd_bank.dwd_loan_receipt
                LIMIT 500;
                """
            )
            cur.execute(
                """
                INSERT INTO ads_reg.ads_east_large_txn
                SELECT 'EAST_20261015', txn_serial_no, acct_no, txn_amt, opp_acct_name, large_txn_ind
                FROM dwd_bank.dwd_txn_detail
                WHERE large_txn_ind = '1' OR aml_suspect_ind = '1'
                LIMIT 500;
                """
            )
            cur.execute(
                """
                INSERT INTO ads_reg.ads_1104_risk_exposure
                SELECT 'G01_001', branch_id, SUM(loan_bal_amt),
                       SUM(CASE WHEN five_class_cd IN ('30','40','50') THEN loan_bal_amt ELSE 0 END),
                       1.8520
                FROM dwd_bank.dwd_loan_receipt
                GROUP BY branch_id;
                """
            )
            cur.execute(
                """
                INSERT INTO ads_reg.ads_aml_suspect_case
                VALUES
                ('AML_CASE_2026_001', 'CUST_C_0088', 'ACCT_CORP_88001', '连续3日向恒通虚贸易商行拆单24笔卡位4.95~4.99万元', 'CRIT'),
                ('AML_CASE_2026_002', 'CUST_C_0089', 'ACCT_CORP_88002', '连续3日向恒通虚贸易商行拆单24笔卡位4.95~4.99万元', 'CRIT');
                """
            )
        conn.commit()
    progress_fn("[DWS/PG16] 数据初始化完成（31 对象、约 30,000+ 行）。")
    return {
        "customers": len(dataset["customers"]),
        "receipts": len(dataset["receipts"]),
        "txns": len(dataset["txns"]),
    }


# =============================================================================
# 2. 核心交易系统 MySQL (MySQL 8.0) 初始化与灌数
# =============================================================================

_MYSQL_DDL_STATEMENTS: Tuple[str, ...] = (
    "DROP TABLE IF EXISTS t_core_customer, t_core_corp_credit, t_core_loan_contract, t_core_loan_receipt, t_core_deposit_acct, t_core_txn_journal, t_core_collateral, t_core_repay_log, t_core_branch, t_core_emp",
    """CREATE TABLE t_core_branch (
        branch_id VARCHAR(16) PRIMARY KEY,
        branch_name VARCHAR(80) NOT NULL,
        parent_branch_id VARCHAR(16) NOT NULL,
        branch_lvl_cd CHAR(1) NOT NULL,
        region_cd CHAR(6) NOT NULL,
        fin_license_no VARCHAR(24) NOT NULL,
        valid_ind CHAR(1) NOT NULL
    ) COMMENT='核心系统机构表'""",
    """CREATE TABLE t_core_emp (
        emp_id VARCHAR(16) PRIMARY KEY,
        emp_name VARCHAR(48) NOT NULL,
        branch_id VARCHAR(16) NOT NULL,
        dept_cd VARCHAR(16) NOT NULL,
        valid_ind CHAR(1) NOT NULL,
        etl_load_ts DATETIME NOT NULL
    ) COMMENT='核心系统员工表'""",
    """CREATE TABLE t_core_customer (
        cust_id VARCHAR(32) PRIMARY KEY,
        cust_name VARCHAR(120) NOT NULL,
        cust_type_cd CHAR(2) NOT NULL,
        cert_type_cd VARCHAR(8) NOT NULL,
        cert_no VARCHAR(32) NOT NULL,
        country_cd CHAR(3) NOT NULL,
        industry_cd VARCHAR(10) NOT NULL,
        cust_risk_lvl_cd VARCHAR(4) NOT NULL,
        mobile_no VARCHAR(20) NOT NULL,
        reg_addr VARCHAR(200) NOT NULL,
        vip_tier_cd VARCHAR(8) NOT NULL,
        cust_status_cd CHAR(2) NOT NULL,
        cust_open_dt DATE NOT NULL,
        branch_id VARCHAR(16) NOT NULL,
        emp_id VARCHAR(16) NOT NULL,
        etl_load_ts DATETIME NOT NULL
    ) COMMENT='核心系统客户主档表'""",
    """CREATE TABLE t_core_corp_credit (
        credit_limit_id VARCHAR(32) PRIMARY KEY,
        cust_id VARCHAR(32) NOT NULL,
        branch_id VARCHAR(16) NOT NULL,
        approved_limit_amt DECIMAL(18,2) NOT NULL,
        used_limit_amt DECIMAL(18,2) NOT NULL,
        stat_dt DATE NOT NULL,
        acct_status_cd CHAR(2) NOT NULL,
        sync_tag VARCHAR(24) NOT NULL
    ) COMMENT='核心对公授信主表（含3户未入仓差额3650万）'""",
    """CREATE TABLE t_core_loan_contract (
        loan_contract_no VARCHAR(36) PRIMARY KEY,
        cust_id VARCHAR(32) NOT NULL,
        loan_prod_cd VARCHAR(16) NOT NULL,
        loan_amt DECIMAL(18,2) NOT NULL,
        exec_int_rate DECIMAL(10,6) NOT NULL,
        loan_term_mth INT NOT NULL,
        sign_dt DATE NOT NULL,
        main_guar_type_cd CHAR(2) NOT NULL,
        branch_id VARCHAR(16) NOT NULL,
        emp_id VARCHAR(16) NOT NULL
    ) COMMENT='核心贷款合同主表'""",
    """CREATE TABLE t_core_loan_receipt (
        loan_receipt_no VARCHAR(36) PRIMARY KEY,
        loan_contract_no VARCHAR(36) NOT NULL,
        cust_id VARCHAR(32) NOT NULL,
        branch_id VARCHAR(16) NOT NULL,
        emp_id VARCHAR(16) NOT NULL,
        loan_prod_cd VARCHAR(16) NOT NULL,
        currency_cd CHAR(3) NOT NULL,
        disburse_amt DECIMAL(18,2) NOT NULL,
        loan_bal_amt DECIMAL(18,2) NOT NULL,
        exec_int_rate DECIMAL(10,6) NOT NULL,
        value_dt DATE NOT NULL,
        maturity_dt DATE NOT NULL,
        five_class_cd CHAR(2) NOT NULL,
        overdue_days INT NOT NULL,
        overdue_prin_amt DECIMAL(18,2) NOT NULL,
        on_bs_int_amt DECIMAL(18,2) NOT NULL,
        off_bs_int_amt DECIMAL(18,2) NOT NULL,
        main_guar_type_cd CHAR(2) NOT NULL,
        receipt_status_cd CHAR(2) NOT NULL
    ) COMMENT='核心贷款借据分户账'""",
    """CREATE TABLE t_core_deposit_acct (
        acct_no VARCHAR(32) PRIMARY KEY,
        cust_id VARCHAR(32) NOT NULL,
        acct_name VARCHAR(120) NOT NULL,
        acct_type_cd VARCHAR(8) NOT NULL,
        acct_status_cd CHAR(2) NOT NULL,
        open_dt DATE NOT NULL,
        acct_bal_amt DECIMAL(18,2) NOT NULL,
        avail_bal_amt DECIMAL(18,2) NOT NULL,
        frozen_amt DECIMAL(18,2) NOT NULL,
        currency_cd CHAR(3) NOT NULL,
        dep_int_rate DECIMAL(10,6) NOT NULL,
        branch_id VARCHAR(16) NOT NULL
    ) COMMENT='核心存款账户主表'""",
    """CREATE TABLE t_core_txn_journal (
        txn_serial_no VARCHAR(40) PRIMARY KEY,
        acct_no VARCHAR(32) NOT NULL,
        cust_id VARCHAR(32) NOT NULL,
        txn_dt DATE NOT NULL,
        txn_ts DATETIME NOT NULL,
        dc_flag_cd CHAR(1) NOT NULL,
        txn_amt DECIMAL(18,2) NOT NULL,
        after_bal_amt DECIMAL(18,2) NOT NULL,
        txn_channel_cd VARCHAR(12) NOT NULL,
        txn_type_cd VARCHAR(12) NOT NULL,
        txn_summary VARCHAR(200) NOT NULL,
        opp_acct_no VARCHAR(36) NOT NULL,
        opp_acct_name VARCHAR(120) NOT NULL,
        opp_bank_cd VARCHAR(16) NOT NULL,
        cash_trf_ind CHAR(1) NOT NULL,
        large_txn_ind CHAR(1) NOT NULL,
        aml_suspect_ind CHAR(1) NOT NULL,
        branch_id VARCHAR(16) NOT NULL
    ) COMMENT='核心交易联机流水表'""",
    """CREATE TABLE t_core_collateral (
        guar_contract_no VARCHAR(36) NOT NULL,
        collateral_id VARCHAR(32) PRIMARY KEY,
        loan_contract_no VARCHAR(36) NOT NULL,
        cust_id VARCHAR(32) NOT NULL,
        coll_type_cd VARCHAR(8) NOT NULL,
        coll_name VARCHAR(120) NOT NULL,
        eval_value_amt DECIMAL(18,2) NOT NULL,
        pledge_ratio DECIMAL(8,4) NOT NULL,
        guar_cover_amt DECIMAL(18,2) NOT NULL,
        coll_status_cd CHAR(2) NOT NULL,
        reval_dt DATE NOT NULL,
        qualified_mitigant_ind CHAR(1) NOT NULL
    ) COMMENT='核心押品登记表'""",
    """CREATE TABLE t_core_repay_log (
        id INT AUTO_INCREMENT PRIMARY KEY,
        loan_receipt_no VARCHAR(36) NOT NULL,
        repay_term_no INT NOT NULL,
        sched_repay_dt DATE NOT NULL,
        sched_prin_amt DECIMAL(18,2) NOT NULL,
        actual_prin_amt DECIMAL(18,2) NOT NULL,
        int_income_amt DECIMAL(18,2) NOT NULL,
        repay_method_cd VARCHAR(8) NOT NULL
    ) COMMENT='核心还款计划流水表'""",
)


def seed_mysql(dataset: Dict[str, Any], progress_fn: Callable[[str], None] = print) -> Dict[str, int]:
    """初始化并灌入核心交易系统 MySQL 仿真库 (10 表)."""
    progress_fn("[MySQL8] 正在重建 core_bank_ods 10 张 OLTP 源表与数据...")
    with _open_mysql_conn() as conn:
        with conn.cursor() as cur:
            for stmt in _MYSQL_DDL_STATEMENTS:
                cur.execute(stmt)
            cur.executemany(
                "INSERT INTO t_core_branch VALUES (%s,%s,%s,%s,%s,%s,%s)",
                dataset["branches"],
            )
            cur.executemany(
                "INSERT INTO t_core_emp VALUES (%s,%s,%s,%s,%s,%s)",
                dataset["managers"],
            )
            cur.executemany(
                "INSERT INTO t_core_customer VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                dataset["customers"],
            )
            cur.executemany(
                "INSERT INTO t_core_corp_credit VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                dataset["mysql_corp_credits"],
            )
            cur.executemany(
                "INSERT INTO t_core_loan_contract VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                dataset["contracts"],
            )
            cur.executemany(
                "INSERT INTO t_core_loan_receipt VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                dataset["receipts"],
            )
            cur.executemany(
                "INSERT INTO t_core_deposit_acct VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                dataset["accounts"],
            )
            for idx in range(0, len(dataset["txns"]), 2000):
                cur.executemany(
                    "INSERT INTO t_core_txn_journal VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    dataset["txns"][idx : idx + 2000],
                )
            cur.executemany(
                "INSERT INTO t_core_collateral VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                dataset["collaterals"],
            )
            cur.executemany(
                """INSERT INTO t_core_repay_log
                (loan_receipt_no, repay_term_no, sched_repay_dt, sched_prin_amt, actual_prin_amt, int_income_amt, repay_method_cd)
                VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                dataset["repay_plans"],
            )
        conn.commit()
    progress_fn("[MySQL8] 数据初始化完成（10 表、约 27,000+ 行）。")
    return {"corp_credits": len(dataset["mysql_corp_credits"])}


# =============================================================================
# 3. 星环 Transwarp Inceptor 仿真库 (Spark HiveThriftServer2) 初始化与灌数
# =============================================================================


def _sql_lit(v: Any) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, datetime.datetime):
        return f"TIMESTAMP '{v.strftime('%Y-%m-%d %H:%M:%S')}'"
    if isinstance(v, datetime.date):
        return f"DATE '{v.strftime('%Y-%m-%d')}'"
    s = str(v).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{s}'"


def _batch_insert_spark(
    client: InceptorThriftClient,
    table_fqn: str,
    rows: Sequence[Tuple[Any, ...]],
    chunk_size: int = 350,
) -> None:
    if not rows:
        return
    for i in range(0, len(rows), chunk_size):
        batch = rows[i : i + chunk_size]
        values_sql = ", ".join("(" + ", ".join(_sql_lit(v) for v in r) + ")" for r in batch)
        client.execute_query(f"INSERT INTO {table_fqn} VALUES {values_sql}")


_INCEPTOR_DDL_STATEMENTS: Tuple[str, ...] = (
    "DROP DATABASE IF EXISTS bank_inceptor CASCADE",
    "DROP DATABASE IF EXISTS bank_ods_hist CASCADE",
    "CREATE DATABASE bank_inceptor COMMENT '启明银行星环大数据主库'",
    "CREATE DATABASE bank_ods_hist COMMENT '启明银行历史贴源归档库'",
    "USE bank_inceptor",
    # 1. 支行机构表（星环端 STRING/INT 老风格）
    """CREATE TABLE bank_inceptor.dwd_org_branch_info (
        branch_id STRING COMMENT '机构编号',
        branch_name STRING COMMENT '机构中文名称',
        parent_branch_id STRING COMMENT '上级机构编号',
        branch_lvl_cd STRING COMMENT '机构层级代码',
        region_cd STRING COMMENT '行政区划代码',
        fin_license_no STRING COMMENT '金融许可证号',
        valid_ind STRING COMMENT '记录有效标识'
    ) COMMENT '星环支行机构信息表' STORED AS PARQUET""",
    # 2. 客户经理表
    """CREATE TABLE bank_inceptor.dwd_emp_manager_info (
        emp_id STRING COMMENT '客户经理工号',
        emp_name STRING COMMENT '员工姓名',
        branch_id STRING COMMENT '机构编号',
        dept_cd STRING COMMENT '所属条线部门代码',
        valid_ind STRING COMMENT '记录有效标识',
        etl_load_ts TIMESTAMP COMMENT 'ETL加载时间戳'
    ) COMMENT '星环客户经理信息表' STORED AS PARQUET""",
    # 3. 客户基础表（故意使用 VARCHAR(200) / STRING 制造 DWS VARCHAR(60) 的 INSUFFICIENT 长度不足诊断）
    """CREATE TABLE bank_inceptor.dwd_cust_base_info (
        khbh VARCHAR(32) COMMENT '老客户编号',
        cust_nm VARCHAR(200) COMMENT '客户名称',
        cust_type_cd CHAR(2) COMMENT '客户类型代码',
        cert_type_cd VARCHAR(8) COMMENT '证件类型代码',
        zjhm VARCHAR(32) COMMENT '证件号码',
        country_cd CHAR(3) COMMENT '国别地区代码',
        industry_cd VARCHAR(10) COMMENT '国民经济行业代码',
        cust_risk_lvl_cd VARCHAR(4) COMMENT '客户风险评级代码',
        mobile_no VARCHAR(20) COMMENT '手机号码',
        reg_addr STRING COMMENT '注册地址(无界STRING)',
        vip_tier_cd VARCHAR(8) COMMENT '客户价值分层代码',
        cust_status_cd CHAR(2) COMMENT '客户状态代码',
        cust_open_dt DATE COMMENT '开户建档日期',
        branch_id VARCHAR(16) COMMENT '机构编号',
        emp_id VARCHAR(16) COMMENT '客户经理工号',
        etl_load_ts TIMESTAMP COMMENT 'ETL加载时间戳'
    ) COMMENT '星环客户基础信息明细表' STORED AS PARQUET""",
    # 4. 对公客户扩展表
    """CREATE TABLE bank_inceptor.dwd_corp_cust_ext (
        cust_id STRING COMMENT '客户统一编号',
        uscc_cd STRING COMMENT '统一社会信用代码',
        ent_scale_cd STRING COMMENT '企业规模代码',
        estab_dt DATE COMMENT '企业成立日期',
        reg_capital_amt DECIMAL(20,4) COMMENT '注册资本金额',
        legal_rep_name STRING COMMENT '法定代表人姓名',
        group_cust_ind CHAR(1) COMMENT '集团客户标识',
        group_id STRING COMMENT '所属集团编号',
        industry_cd STRING COMMENT '国民经济行业代码',
        branch_id STRING COMMENT '机构编号'
    ) COMMENT '星环对公客户扩展表' STORED AS PARQUET""",
    # 5. 存款账户表
    """CREATE TABLE bank_inceptor.dwd_acct_deposit_info (
        acct_no STRING COMMENT '银行账号',
        cust_id STRING COMMENT '客户统一编号',
        acct_name VARCHAR(200) COMMENT '账户户名',
        acct_type_cd STRING COMMENT '账户类型代码',
        acct_status_cd CHAR(2) COMMENT '账户状态代码',
        open_dt DATE COMMENT '开户日期',
        acct_bal_amt DOUBLE COMMENT '账户活期余额(老DOUBLE)',
        avail_bal_amt DECIMAL(20,4) COMMENT '账户可用余额',
        frozen_amt DECIMAL(20,4) COMMENT '冻结金额',
        currency_cd CHAR(3) COMMENT '币种代码',
        dep_int_rate DOUBLE COMMENT '存款利率',
        branch_id VARCHAR(16) COMMENT '机构编号'
    ) COMMENT '星环存款账户主档表' STORED AS PARQUET""",
    # 6. 贷款合同表
    """CREATE TABLE bank_inceptor.dwd_loan_contract (
        htbh STRING COMMENT '老合同号',
        khbh STRING COMMENT '客户号',
        loan_prod_cd STRING COMMENT '贷款产品代码',
        dkje DOUBLE COMMENT '贷款合同金额(DOUBLE需转DECIMAL)',
        zxlv DOUBLE COMMENT '执行年利率(DOUBLE)',
        dkqx BIGINT COMMENT '贷款期限月数(BIGINT->INT)',
        sign_dt_str STRING COMMENT '签约日期字符串',
        main_guar_type_cd CHAR(2) COMMENT '主要担保方式代码',
        branch_id VARCHAR(16) COMMENT '机构编号',
        emp_id VARCHAR(16) COMMENT '客户经理工号'
    ) COMMENT '星环贷款合同历史表' STORED AS PARQUET""",
    # 7. 贷款借据表（含老类型 DOUBLE / DECIMAL(20,4) / STRING）
    """CREATE TABLE bank_inceptor.dwd_loan_receipt (
        loan_receipt_no VARCHAR(36) COMMENT '贷款借据编号',
        loan_contract_no VARCHAR(36) COMMENT '贷款合同编号',
        cust_id VARCHAR(32) COMMENT '客户统一编号',
        branch_id VARCHAR(16) COMMENT '机构编号',
        emp_id VARCHAR(16) COMMENT '客户经理工号',
        loan_prod_cd VARCHAR(16) COMMENT '贷款产品代码',
        currency_cd CHAR(3) COMMENT '币种代码',
        disburse_amt DECIMAL(20,4) COMMENT '放款发放金额',
        loan_bal_amt DECIMAL(20,4) COMMENT '贷款本金余额',
        exec_int_rate DOUBLE COMMENT '执行年利率',
        value_dt DATE COMMENT '放款起息日期',
        maturity_dt DATE COMMENT '贷款到期日期',
        five_class_cd CHAR(2) COMMENT '五级分类代码',
        overdue_days BIGINT COMMENT '逾期天数',
        overdue_prin_amt DECIMAL(20,4) COMMENT '逾期本金金额',
        on_bs_int_amt DECIMAL(18,2) COMMENT '表内欠息金额',
        off_bs_int_amt DECIMAL(18,2) COMMENT '表外欠息金额',
        main_guar_type_cd CHAR(2) COMMENT '主要担保方式代码',
        receipt_status_cd CHAR(2) COMMENT '借据核算状态代码'
    ) COMMENT '星环贷款借据明细表' STORED AS PARQUET""",
    # 8. 还款计划表
    """CREATE TABLE bank_inceptor.dwd_loan_repay_plan (
        loan_receipt_no STRING COMMENT '贷款借据编号',
        repay_term_no INT COMMENT '还款期次',
        sched_repay_dt DATE COMMENT '应还日期',
        sched_prin_amt DECIMAL(18,2) COMMENT '本期应还本金',
        actual_prin_amt DECIMAL(18,2) COMMENT '本期实还本金',
        int_income_amt DECIMAL(18,2) COMMENT '利息收入金额',
        repay_method_cd STRING COMMENT '还款方式代码'
    ) COMMENT '星环贷款还款计划表' STORED AS PARQUET""",
    # 9. 核心交易流水明细表
    """CREATE TABLE bank_inceptor.dwd_txn_detail (
        txn_serial_no VARCHAR(40) COMMENT '交易流水号',
        acct_no VARCHAR(32) COMMENT '银行账号',
        cust_id VARCHAR(32) COMMENT '客户统一编号',
        txn_dt DATE COMMENT '交易日期',
        txn_ts TIMESTAMP COMMENT '交易发生时间戳',
        dc_flag_cd CHAR(1) COMMENT '借贷标志代码',
        txn_amt DECIMAL(20,4) COMMENT '交易发生金额',
        after_bal_amt DECIMAL(20,4) COMMENT '交易后账户余额',
        txn_channel_cd VARCHAR(12) COMMENT '交易渠道代码',
        txn_type_cd VARCHAR(12) COMMENT '交易类型代码',
        txn_summary STRING COMMENT '交易摘要说明',
        opp_acct_no VARCHAR(36) COMMENT '对手方账号',
        opp_acct_name VARCHAR(200) COMMENT '对手方户名',
        opp_bank_cd VARCHAR(16) COMMENT '对手方开户行号',
        cash_trf_ind CHAR(1) COMMENT '现转标志代码',
        large_txn_ind CHAR(1) COMMENT '大额交易上报标识',
        aml_suspect_ind CHAR(1) COMMENT '可疑拆单预警标识',
        branch_id VARCHAR(16) COMMENT '机构编号'
    ) COMMENT '星环核心交易流水表' STORED AS PARQUET""",
    # 10. 担保押品表
    """CREATE TABLE bank_inceptor.dwd_collateral_info (
        guar_contract_no STRING COMMENT '担保合同编号',
        collateral_id STRING COMMENT '押品统一编号',
        loan_contract_no STRING COMMENT '贷款合同编号',
        cust_id STRING COMMENT '客户统一编号',
        coll_type_cd STRING COMMENT '押品大类代码',
        coll_name STRING COMMENT '押品名称描述',
        eval_value_amt DECIMAL(18,2) COMMENT '押品评估价值',
        pledge_ratio DOUBLE COMMENT '核定抵质押率',
        guar_cover_amt DECIMAL(18,2) COMMENT '担保覆盖金额',
        coll_status_cd CHAR(2) COMMENT '押品登记状态代码',
        reval_dt DATE COMMENT '最近重估日期',
        qualified_mitigant_ind CHAR(1) COMMENT '合格缓释工具标识'
    ) COMMENT '星环担保押品信息表' STORED AS PARQUET""",
    # 11. 对公授信额度表
    """CREATE TABLE bank_inceptor.dwd_credit_limit (
        credit_limit_id STRING COMMENT '授信额度编号',
        cust_id STRING COMMENT '客户统一编号',
        branch_id STRING COMMENT '机构编号',
        approved_limit_amt DECIMAL(18,2) COMMENT '核定授信总额',
        used_limit_amt DECIMAL(18,2) COMMENT '已用授信额度',
        value_dt DATE COMMENT '放款起息日期',
        maturity_dt DATE COMMENT '贷款到期日期',
        valid_ind CHAR(1) COMMENT '记录有效标识'
    ) COMMENT '星环对公授信额度表' STORED AS PARQUET""",
    # 12. 信用卡账户表
    """CREATE TABLE bank_inceptor.dwd_credit_card_acct (
        card_no STRING COMMENT '银行卡号',
        cust_id STRING COMMENT '客户统一编号',
        card_limit_amt DECIMAL(18,2) COMMENT '信用卡授信额度',
        card_used_amt DECIMAL(18,2) COMMENT '信用卡已用额度',
        acct_status_cd CHAR(2) COMMENT '账户状态代码',
        branch_id STRING COMMENT '机构编号'
    ) COMMENT '星环信用卡账户表' STORED AS PARQUET""",
    # 13. 理财持仓表
    """CREATE TABLE bank_inceptor.dwd_wealth_holding (
        wealth_prod_cd STRING COMMENT '理财产品代码',
        cust_id STRING COMMENT '客户统一编号',
        wealth_share_qty DECIMAL(18,4) COMMENT '理财持有份额',
        wealth_mkt_val_amt DECIMAL(18,2) COMMENT '理财持有市值',
        stat_dt DATE COMMENT '数据统计日期',
        branch_id STRING COMMENT '机构编号'
    ) COMMENT '星环理财持仓表' STORED AS PARQUET""",
    # 14-18. 汇总与监管映射表（用于推荐未保存表与跨库对比）
    """CREATE TABLE bank_inceptor.dws_cust_panoramic_360 (
        cust_id STRING COMMENT '客户统一编号',
        cust_name VARCHAR(200) COMMENT '客户中文全称',
        cust_type_cd CHAR(2) COMMENT '客户类型代码',
        vip_tier_cd STRING COMMENT '客户价值分层代码',
        acct_bal_amt DECIMAL(20,4) COMMENT '账户活期余额',
        loan_bal_amt DECIMAL(20,4) COMMENT '贷款本金余额',
        branch_id STRING COMMENT '机构编号',
        stat_dt DATE COMMENT '数据统计日期'
    ) COMMENT '星环客户全景汇总宽表' STORED AS PARQUET""",
    """CREATE TABLE bank_inceptor.dws_corp_credit_exposure (
        credit_limit_id STRING COMMENT '授信额度编号',
        cust_id STRING COMMENT '客户统一编号',
        branch_id STRING COMMENT '机构编号',
        approved_limit_amt DECIMAL(18,2) COMMENT '核定授信总额',
        used_limit_amt DECIMAL(18,2) COMMENT '已用授信额度',
        stat_dt DATE COMMENT '数据统计日期',
        acct_status_cd CHAR(2) COMMENT '账户状态代码',
        sync_tag STRING COMMENT '同步标记'
    ) COMMENT '星环对公授信敞口表' STORED AS PARQUET""",
    """CREATE TABLE bank_inceptor.dws_branch_deposit_loan_d (
        stat_dt DATE COMMENT '数据统计日期',
        branch_id STRING COMMENT '机构编号',
        branch_name STRING COMMENT '机构中文名称',
        acct_bal_amt DECIMAL(18,2) COMMENT '账户活期余额',
        loan_bal_amt DECIMAL(18,2) COMMENT '贷款本金余额',
        loan_to_dep_ratio DOUBLE COMMENT '存贷比指标值'
    ) COMMENT '星环机构存贷比汇总表' STORED AS PARQUET""",
    """CREATE TABLE bank_inceptor.dws_aml_large_txn_alert (
        aml_case_id STRING COMMENT '可疑案件编号',
        acct_no STRING COMMENT '银行账号',
        cust_id STRING COMMENT '客户统一编号',
        split_3d_cnt INT COMMENT '近3日拆单笔数',
        daily_txn_amt DECIMAL(18,2) COMMENT '日累计交易金额',
        alert_level_cd STRING COMMENT '风险预警等级代码',
        alert_rule_cd STRING COMMENT '预警触发规则代码',
        stat_dt DATE COMMENT '数据统计日期'
    ) COMMENT '星环反洗钱拆单告警表' STORED AS PARQUET""",
    """CREATE TABLE bank_inceptor.ads_east_cust_info (
        east_batch_no STRING COMMENT 'EAST报送批次号',
        cust_id STRING COMMENT '客户统一编号',
        cust_name VARCHAR(200) COMMENT '客户中文全称',
        cert_type_cd STRING COMMENT '证件类型代码',
        cert_no STRING COMMENT '证件号码',
        branch_id STRING COMMENT '机构编号'
    ) COMMENT '星环EAST客户报送表' STORED AS PARQUET""",
    """CREATE TABLE bank_inceptor.ads_east_loan_detail (
        east_batch_no STRING COMMENT 'EAST报送批次号',
        loan_receipt_no STRING COMMENT '贷款借据编号',
        cust_id STRING COMMENT '客户统一编号',
        loan_bal_amt DECIMAL(18,2) COMMENT '贷款本金余额',
        five_class_cd CHAR(2) COMMENT '五级分类代码',
        overdue_days INT COMMENT '逾期天数',
        dq_anomaly_ind CHAR(1) COMMENT '数据质量异常标识'
    ) COMMENT '星环EAST借据报送表' STORED AS PARQUET""",
    """CREATE TABLE bank_inceptor.ads_1104_risk_exposure (
        reg_ind_cd STRING COMMENT '监管指标代码',
        branch_id STRING COMMENT '机构编号',
        risk_exposure_amt DECIMAL(18,2) COMMENT '风险暴露总金额',
        npl_bal_amt DECIMAL(18,2) COMMENT '不良贷款余额',
        provision_cov_ratio DOUBLE COMMENT '拨备覆盖率'
    ) COMMENT '星环1104风险暴露表' STORED AS PARQUET""",
    # 2 张仅存在于星环的独有历史归档表（跨环境比对时展示单侧独有表）
    """CREATE TABLE bank_inceptor.his_legacy_bill_2022 (
        bill_no STRING COMMENT '历史票据编号',
        cust_id STRING COMMENT '客户统一编号',
        bill_amt DECIMAL(18,2) COMMENT '票面金额',
        issue_dt STRING COMMENT '出票日期'
    ) COMMENT '星环2022旧票据系统归档表（单侧独有）' STORED AS PARQUET""",
    """CREATE TABLE bank_inceptor.his_legacy_guar_2022 (
        guar_no STRING COMMENT '旧保函编号',
        cust_id STRING COMMENT '客户统一编号',
        guar_amt DOUBLE COMMENT '担保金额',
        expire_dt STRING COMMENT '到期日'
    ) COMMENT '星环2022旧保函归档表（单侧独有）' STORED AS PARQUET""",
    # 4 层级联监管视图（供多邻国视图血缘穿透与方言互译演示）
    """CREATE VIEW bank_inceptor.v_l1_loan_base AS
    SELECT loan_receipt_no, cust_id, branch_id, emp_id, loan_bal_amt, exec_int_rate, five_class_cd, overdue_days
    FROM bank_inceptor.dwd_loan_receipt
    WHERE receipt_status_cd IN ('01', '02')""",
    """CREATE VIEW bank_inceptor.v_l2_loan_overdue AS
    SELECT loan_receipt_no, cust_id, branch_id, emp_id, loan_bal_amt, five_class_cd, overdue_days,
           CASE WHEN overdue_days > 180 AND five_class_cd = '10' THEN '1' ELSE '0' END AS dq_anomaly_ind
    FROM bank_inceptor.v_l1_loan_base
    WHERE overdue_days > 0 OR loan_bal_amt < 0""",
    """CREATE VIEW bank_inceptor.v_l3_branch_risk AS
    SELECT branch_id, COUNT(*) AS anomaly_loan_cnt,
           SUM(loan_bal_amt) AS risk_exposure_amt,
           SUM(CASE WHEN dq_anomaly_ind = '1' THEN loan_bal_amt ELSE 0 END) AS concealed_npl_amt
    FROM bank_inceptor.v_l2_loan_overdue
    GROUP BY branch_id""",
    """CREATE VIEW bank_inceptor.v_l4_east_credit_summary AS
    SELECT br.branch_id, br.branch_name,
           COALESCE(r.anomaly_loan_cnt, 0) AS anomaly_loan_cnt,
           COALESCE(r.risk_exposure_amt, 0) AS risk_exposure_amt,
           COALESCE(r.concealed_npl_amt, 0) AS concealed_npl_amt
    FROM bank_inceptor.dwd_org_branch_info br
    LEFT JOIN bank_inceptor.v_l3_branch_risk r ON r.branch_id = br.branch_id""",
    # 第二库 bank_ods_hist (5 张历史贴源表)
    "USE bank_ods_hist",
    "CREATE TABLE bank_ods_hist.ods_core_cust_snap (cust_id STRING, cust_name STRING, pt_dt STRING) STORED AS PARQUET",
    "CREATE TABLE bank_ods_hist.ods_core_acct_snap (acct_no STRING, cust_id STRING, acct_bal DOUBLE, pt_dt STRING) STORED AS PARQUET",
    "CREATE TABLE bank_ods_hist.ods_cms_loan_snap (loan_receipt_no STRING, loan_bal DECIMAL(18,2), pt_dt STRING) STORED AS PARQUET",
    "CREATE TABLE bank_ods_hist.ods_pay_txn_snap (txn_serial_no STRING, txn_amt DECIMAL(18,2), pt_dt STRING) STORED AS PARQUET",
    "CREATE TABLE bank_ods_hist.ods_coll_eval_snap (collateral_id STRING, eval_value DECIMAL(18,2), pt_dt STRING) STORED AS PARQUET",
    "USE bank_inceptor",
)


def seed_inceptor(dataset: Dict[str, Any], progress_fn: Callable[[str], None] = print) -> Dict[str, int]:
    """初始化并灌入星环 Inceptor 仿真库 (Spark HiveThriftServer2)."""
    progress_fn("[Inceptor/Spark] 正在重建 bank_inceptor (22 表 + 4 视图) 与 bank_ods_hist (5 表)...")
    with _open_inceptor_client("default") as client:
        for stmt in _INCEPTOR_DDL_STATEMENTS:
            client.execute_query(stmt)

        progress_fn("[Inceptor/Spark] 正在批量灌入客户、借据、交易流水（含全部 5 大业务暗雷）...")
        _batch_insert_spark(client, "bank_inceptor.dwd_org_branch_info", dataset["branches"], 50)
        _batch_insert_spark(client, "bank_inceptor.dwd_emp_manager_info", dataset["managers"], 100)
        # 500 对公 + 500 零售客户（覆盖全部业务关联主体）
        _batch_insert_spark(client, "bank_inceptor.dwd_cust_base_info", dataset["customers"][:1000], 250)
        _batch_insert_spark(client, "bank_inceptor.dwd_corp_cust_ext", dataset["corp_exts"], 250)
        _batch_insert_spark(client, "bank_inceptor.dwd_acct_deposit_info", dataset["accounts"][:600], 300)
        _batch_insert_spark(client, "bank_inceptor.dwd_loan_contract", dataset["contracts"][:500], 250)
        # 保留前 800 笔借据（100% 包含全部 7 笔负余额 + 12 笔隐匿不良 + 5 笔零利率大额 + 776 正常）
        _batch_insert_spark(client, "bank_inceptor.dwd_loan_receipt", dataset["receipts"][:800], 250)
        # 保留前 1,200 笔交易流水（100% 包含全部 48 笔 AML 拆单 + 1,152 正常流水）
        _batch_insert_spark(client, "bank_inceptor.dwd_txn_detail", dataset["txns"][:1200], 300)
        _batch_insert_spark(client, "bank_inceptor.dwd_collateral_info", dataset["collaterals"][:500], 250)
        _batch_insert_spark(client, "bank_inceptor.dwd_credit_limit", dataset["credit_limits"][:300], 300)
        _batch_insert_spark(client, "bank_inceptor.dws_corp_credit_exposure", dataset["dw_corp_credits"], 250)
        _batch_insert_spark(
            client,
            "bank_inceptor.dws_aml_large_txn_alert",
            [
                ("AML_CASE_2026_001", "ACCT_CORP_88001", "CUST_C_0088", 24, 1194600.00, "CRIT", "RULE_SPLIT_5W", datetime.date(2026, 10, 14)),
                ("AML_CASE_2026_002", "ACCT_CORP_88002", "CUST_C_0089", 24, 1194600.00, "CRIT", "RULE_SPLIT_5W", datetime.date(2026, 10, 14)),
            ],
            20,
        )
        _batch_insert_spark(
            client,
            "bank_inceptor.his_legacy_bill_2022",
            [("BILL2022001", "CUST_C_0001", 2500000.00, "2022-05-10"), ("BILL2022002", "CUST_C_0002", 1800000.00, "2022-08-21")],
            20,
        )
    progress_fn("[Inceptor/Spark] 数据初始化完成（31 对象、含全部暗雷样本）。")
    return {"receipts": 800, "txns": 1200}


def seed_all(
    *,
    targets: Optional[Sequence[str]] = None,
    progress_fn: Callable[[str], None] = print,
) -> Dict[str, Any]:
    """一键初始化并灌入指定或全部活库引擎."""
    dataset = generate_banking_dataset()
    tgt_set = {t.lower() for t in targets} if targets else {"dws", "mysql", "inceptor"}
    stats: Dict[str, Any] = {}
    if "dws" in tgt_set:
        stats["dws"] = seed_dws(dataset, progress_fn=progress_fn)
    if "mysql" in tgt_set:
        stats["mysql"] = seed_mysql(dataset, progress_fn=progress_fn)
    if "inceptor" in tgt_set:
        stats["inceptor"] = seed_inceptor(dataset, progress_fn=progress_fn)
    return stats


def verify_live_anomalies() -> Dict[str, Any]:
    """实测校验三引擎连通性及 5 大业务暗雷在活库中的精确数字."""
    report: Dict[str, Any] = {}
    with _open_dws_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COALESCE(SUM(loan_bal_amt), 0) FROM dwd_bank.dwd_loan_receipt WHERE five_class_cd IN ('30','40','50') AND loan_bal_amt < 0;")
        neg_cnt, neg_sum = cur.fetchone()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(loan_bal_amt), 0) FROM dwd_bank.dwd_loan_receipt WHERE overdue_days > 180 AND five_class_cd = '10';")
        hide_cnt, hide_sum = cur.fetchone()
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(txn_amt), 0), COUNT(DISTINCT opp_acct_name)
            FROM dwd_bank.dwd_txn_detail
            WHERE txn_dt BETWEEN DATE '2026-10-12' AND DATE '2026-10-14'
              AND txn_amt >= 49500 AND txn_amt < 50000;
            """
        )
        aml_cnt, aml_sum, aml_opps = cur.fetchone()
        cur.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(r.disburse_amt), 0)
            FROM dwd_bank.dwd_loan_receipt r
            LEFT JOIN dwd_bank.dwd_collateral_info c ON c.loan_contract_no = r.loan_contract_no
            WHERE r.disburse_amt >= 10000000 AND r.exec_int_rate <= 0.0005 AND c.collateral_id IS NULL;
            """
        )
        zero_cnt, zero_sum = cur.fetchone()
        cur.execute("SELECT COUNT(*), COALESCE(SUM(approved_limit_amt), 0) FROM dws_bank.dws_corp_credit_exposure;")
        dws_cred_cnt, dws_cred_sum = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM dwd_bank.dwd_loan_contract;")
        contract_cnt = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM dwd_bank.dwd_acct_swap_demo;")
        swap_cnt = cur.fetchone()[0]

    with _open_mysql_conn() as mconn, mconn.cursor() as mcur:
        mcur.execute("SELECT COUNT(*), COALESCE(SUM(approved_limit_amt), 0) FROM t_core_corp_credit;")
        mysql_cred_cnt, mysql_cred_sum = mcur.fetchone()

    with _open_inceptor_client(INCEPTOR_DB) as iclient:
        inc_tbls = iclient.execute_query(f"SHOW TABLES IN {INCEPTOR_DB}")
        inc_aml = iclient.execute_query(
            f"SELECT COUNT(*), SUM(txn_amt) FROM {INCEPTOR_DB}.dwd_txn_detail WHERE txn_amt >= 49500 AND txn_amt < 50000"
        )

    recon_gap = float(mysql_cred_sum) - float(dws_cred_sum)
    report = {
        "dws": {
            "neg_npl_count": int(neg_cnt),
            "neg_npl_sum": float(neg_sum),
            "concealed_npl_count": int(hide_cnt),
            "concealed_npl_sum": float(hide_sum),
            "aml_split_count": int(aml_cnt),
            "aml_split_sum": float(aml_sum),
            "aml_opp_count": int(aml_opps),
            "zero_rate_unsecured_count": int(zero_cnt),
            "zero_rate_unsecured_sum": float(zero_sum),
            "dws_credit_count": int(dws_cred_cnt),
            "contract_count": int(contract_cnt),
            "swap_demo_count": int(swap_cnt),
        },
        "mysql": {
            "core_credit_count": int(mysql_cred_cnt),
            "recon_gap_vs_dws": round(recon_gap, 2),
        },
        "inceptor": {
            "bank_inceptor_objects": len(inc_tbls),
            "aml_split_count": int(inc_aml[0][0]) if inc_aml else 0,
            "aml_split_sum": float(inc_aml[0][1] or 0) if inc_aml else 0.0,
        },
    }
    return report
