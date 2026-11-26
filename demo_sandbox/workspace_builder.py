"""启明商业银行双模式 `.dataog.db` 零密钥工作区生成器.

严格复用 `core.platform.storage` / `StandardCatalog` / `WorkspaceManager.export_workspace_bytes` 官方机制构建：
1. `qiming_live_sandbox.dataog.db`：对接本地 3 个活库容器，预置 164 条金融标准字典、真实或等价元数据缓存、
   字段级绑定（涵盖 `COMPLIANT`/`COMMENT_ONLY`/`NON_COMPLIANT`/`UNBOUND`）、15 对跨环境映射与待贯标靶表；
2. `qiming_enterprise_150tables.dataog.db`：合成覆盖 8 大主题域、ODS/DWD/DWS/ADS 四层的 156 张星环表 +
   162 张华为 DWS 表（5,890 个双端字段、5,222 条跨端绑标）、120 对跨环境表映射、20 张推荐同名未保存表及全谱系兼容告警，
   用于百表级大中台压测演示与离线兜底。

所有输出文件均经由 `WorkspaceManager.export_workspace_bytes` 净化导出（`DELETE` 日志模式 + `VACUUM` +
零私有表 + 零账号密码/API Key）。
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, List, Tuple

from core.platform.context import PlatformContext
from core.platform.dialects import normalize_cross_type
from core.platform.models import ColumnMeta, ConnectionProfile, SchemaMeta, TableMeta
from core.platform.standard_catalog import StandardCatalog
from demo_sandbox.banking_catalog import (
    build_standard_items,
    export_binding_import_template_xlsx,
    export_standard_dictionary_xlsx,
    get_standard_by_code,
)
from demo_sandbox.live_seeder import (
    DWS_DB,
    DWS_HOST,
    DWS_PASSWORD,
    DWS_PORT,
    DWS_USER,
    INCEPTOR_DB,
    INCEPTOR_HOST,
    INCEPTOR_PASSWORD,
    INCEPTOR_PORT,
    INCEPTOR_USER,
    MYSQL_DB,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
)

CONN_DWS_ID = "conn-qiming-dws"
CONN_INC_ID = "conn-qiming-inceptor"
CONN_MY_ID = "conn-qiming-mysql"


def build_demo_connection_profiles(*, include_secrets: bool = True) -> List[ConnectionProfile]:
    """返回启明银行三引擎标准连接档案（导出至 .dataog.db 时自动剥离凭据）."""
    return [
        ConnectionProfile(
            connection_id=CONN_DWS_ID,
            name="华为 GaussDB(DWS) · 启明银行信创生产数仓",
            driver_track="NATIVE",
            db_type="DWS",
            host=DWS_HOST,
            port=DWS_PORT,
            dbname=DWS_DB,
            user=DWS_USER if include_secrets else "",
            password=DWS_PASSWORD if include_secrets else "",
            default_schema="dwd_bank",
            sslmode="disable",
            auth_mode="LDAP",
            standard_format="type_dws",
        ),
        ConnectionProfile(
            connection_id=CONN_INC_ID,
            name="星环 Inceptor · 启明银行历史大数据平台",
            driver_track="NATIVE",
            db_type="INCEPTOR",
            host=INCEPTOR_HOST,
            port=INCEPTOR_PORT,
            dbname=INCEPTOR_DB,
            user=INCEPTOR_USER if include_secrets else "",
            password=INCEPTOR_PASSWORD if include_secrets else "",
            default_schema=INCEPTOR_DB,
            sslmode="prefer",
            auth_mode="NOSASL",
            standard_format="type_argodb",
        ),
        ConnectionProfile(
            connection_id=CONN_MY_ID,
            name="核心交易系统 MySQL · 启明银行联机源库",
            driver_track="NATIVE",
            db_type="MYSQL",
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            dbname=MYSQL_DB,
            user=MYSQL_USER if include_secrets else "",
            password=MYSQL_PASSWORD if include_secrets else "",
            default_schema=MYSQL_DB,
            sslmode="prefer",
            auth_mode="LDAP",
            standard_format="type_mysql",
        ),
    ]


# =============================================================================
# 离线等价元数据定义（供免 Docker `generate-assets` 与 CI 单元测试使用）
# =============================================================================


def _col(pos: int, name: str, dtype: str, comment: str = "", pk: bool = False) -> ColumnMeta:
    return ColumnMeta(
        ordinal_position=pos,
        column_name=name,
        data_type=dtype,
        normalized_type=normalize_cross_type(dtype),
        is_not_null=pk,
        column_comment=comment,
        is_primary_key=pk,
    )


def _offline_live_table_specs() -> Dict[str, List[Tuple[str, str, str, str, List[Tuple[str, str, str]]]]]:
    """与 `live_seeder.py` 完全一致的离线元数据定义."""
    dws_tables: List[Tuple[str, str, str, str, List[Tuple[str, str, str]]]] = [
        ("dwd_bank", "dwd_org_branch_info", "TABLE", "支行机构信息表（全表100%落标样板）", [
            ("branch_id", "character varying(16)", "机构编号"),
            ("branch_name", "character varying(80)", "机构中文名称"),
            ("parent_branch_id", "character varying(16)", "上级机构编号"),
            ("branch_lvl_cd", "character(1)", "机构层级代码"),
            ("region_cd", "character(6)", "行政区划代码"),
            ("fin_license_no", "character varying(24)", "金融许可证号"),
            ("valid_ind", "character(1)", "记录有效标识"),
        ]),
        ("dwd_bank", "dwd_emp_manager_info", "TABLE", "客户经理主数据表", [
            ("emp_id", "character varying(16)", "客户经理工号"),
            ("emp_name", "character varying(48)", "员工姓名"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("dept_cd", "character varying(16)", "所属条线部门代码"),
            ("valid_ind", "character(1)", "记录有效标识"),
            ("etl_load_ts", "timestamp without time zone", "ETL加载时间戳"),
        ]),
        ("dwd_bank", "dwd_cust_base_info", "TABLE", "全行客户基础信息明细表（含待治理历史字段）", [
            ("khbh", "character varying(18)", "老客户编号"),
            ("cust_nm", "character varying(60)", "客户中文简称"),
            ("cust_type_cd", "character(2)", "客户类型代码"),
            ("cert_type_cd", "character varying(8)", ""),
            ("zjhm", "character varying(18)", "证件号"),
            ("country_cd", "character(3)", "国别地区代码"),
            ("industry_cd", "character varying(10)", "国民经济行业代码"),
            ("cust_risk_lvl_cd", "character varying(4)", "客户风险评级代码"),
            ("mobile_no", "character varying(20)", ""),
            ("reg_addr", "character varying(120)", "注册或联系地址"),
            ("vip_tier_cd", "character varying(8)", "客户价值分层代码"),
            ("cust_status_cd", "character(2)", "客户状态代码"),
            ("cust_open_dt", "date", "开户建档日期"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("emp_id", "character varying(16)", "客户经理工号"),
            ("etl_load_ts", "timestamp without time zone", "ETL加载时间戳"),
        ]),
        ("dwd_bank", "dwd_corp_cust_ext", "TABLE", "对公客户扩展信息明细表", [
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("uscc_cd", "character varying(18)", "统一社会信用代码"),
            ("ent_scale_cd", "character(2)", "企业规模代码"),
            ("estab_dt", "date", "企业成立日期"),
            ("reg_capital_amt", "numeric(18,2)", "注册资本金额"),
            ("legal_rep_name", "character varying(64)", "法定代表人姓名"),
            ("group_cust_ind", "character(1)", "集团客户标识"),
            ("group_id", "character varying(32)", "所属集团编号"),
            ("industry_cd", "character varying(10)", "国民经济行业代码"),
            ("branch_id", "character varying(16)", "机构编号"),
        ]),
        ("dwd_bank", "dwd_acct_deposit_info", "TABLE", "存款账户主档明细表", [
            ("acct_no", "character varying(32)", "银行账号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("acct_name", "character varying(120)", "账户户名"),
            ("acct_type_cd", "character varying(8)", "账户类型代码"),
            ("acct_status_cd", "character(2)", "账户状态代码"),
            ("open_dt", "date", "开户日期"),
            ("acct_bal_amt", "numeric(18,2)", "账户活期余额"),
            ("avail_bal_amt", "numeric(18,2)", "账户可用余额"),
            ("frozen_amt", "numeric(18,2)", "冻结金额"),
            ("currency_cd", "character(3)", "币种代码"),
            ("dep_int_rate", "numeric(10,6)", "存款利率"),
            ("branch_id", "character varying(16)", "机构编号"),
        ]),
        ("dwd_bank", "dwd_loan_contract", "TABLE", "贷款合同明细表（DIRECT_ALTER 贯标改表演示靶表）", [
            ("htbh", "character varying(24)", "老合同号"),
            ("khbh", "character varying(18)", "客户号"),
            ("loan_prod_cd", "character varying(16)", ""),
            ("dkje", "numeric(14,2)", "合同金额"),
            ("zxlv", "numeric(8,4)", "利率"),
            ("dkqx", "smallint", "期限"),
            ("sign_dt_str", "character varying(10)", "签约日期文本"),
            ("main_guar_type_cd", "character(2)", "主要担保方式代码"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("emp_id", "character varying(16)", "客户经理工号"),
        ]),
        ("dwd_bank", "dwd_loan_receipt", "TABLE", "贷款借据明细表", [
            ("loan_receipt_no", "character varying(36)", "贷款借据编号"),
            ("loan_contract_no", "character varying(36)", "贷款合同编号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("emp_id", "character varying(16)", "客户经理工号"),
            ("loan_prod_cd", "character varying(16)", "贷款产品代码"),
            ("currency_cd", "character(3)", "币种代码"),
            ("disburse_amt", "numeric(18,2)", "放款发放金额"),
            ("loan_bal_amt", "numeric(18,2)", "贷款本金余额"),
            ("exec_int_rate", "numeric(10,6)", "执行年利率"),
            ("value_dt", "date", "放款起息日期"),
            ("maturity_dt", "date", "贷款到期日期"),
            ("five_class_cd", "character(2)", "五级分类代码"),
            ("overdue_days", "integer", "逾期天数"),
            ("overdue_prin_amt", "numeric(18,2)", "逾期本金金额"),
            ("on_bs_int_amt", "numeric(18,2)", "表内欠息金额"),
            ("off_bs_int_amt", "numeric(18,2)", "表外欠息金额"),
            ("main_guar_type_cd", "character(2)", "主要担保方式代码"),
            ("receipt_status_cd", "character(2)", "借据核算状态代码"),
        ]),
        ("dwd_bank", "dwd_loan_repay_plan", "TABLE", "贷款还款计划明细表", [
            ("loan_receipt_no", "character varying(36)", "贷款借据编号"),
            ("repay_term_no", "integer", "还款期次"),
            ("sched_repay_dt", "date", "应还日期"),
            ("sched_prin_amt", "numeric(18,2)", "本期应还本金"),
            ("actual_prin_amt", "numeric(18,2)", "本期实还本金"),
            ("int_income_amt", "numeric(18,2)", "利息收入金额"),
            ("repay_method_cd", "character varying(8)", "还款方式代码"),
        ]),
        ("dwd_bank", "dwd_txn_detail", "TABLE", "核心交易流水明细表（含反洗钱拆分暗雷样本）", [
            ("txn_serial_no", "character varying(40)", "交易流水号"),
            ("acct_no", "character varying(32)", "银行账号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("txn_dt", "date", "交易日期"),
            ("txn_ts", "timestamp without time zone", "交易发生时间戳"),
            ("dc_flag_cd", "character(1)", "借贷标志代码"),
            ("txn_amt", "numeric(18,2)", "交易发生金额"),
            ("after_bal_amt", "numeric(18,2)", "交易后账户余额"),
            ("txn_channel_cd", "character varying(12)", "交易渠道代码"),
            ("txn_type_cd", "character varying(12)", "交易类型代码"),
            ("txn_summary", "character varying(200)", "交易摘要说明"),
            ("opp_acct_no", "character varying(36)", "对手方账号"),
            ("opp_acct_name", "character varying(120)", "对手方户名"),
            ("opp_bank_cd", "character varying(16)", "对手方开户行号"),
            ("cash_trf_ind", "character(1)", "现转标志代码"),
            ("large_txn_ind", "character(1)", "大额交易上报标识"),
            ("aml_suspect_ind", "character(1)", "可疑拆单预警标识"),
            ("branch_id", "character varying(16)", "机构编号"),
        ]),
        ("dwd_bank", "dwd_collateral_info", "TABLE", "担保押品信息明细表", [
            ("guar_contract_no", "character varying(36)", "担保合同编号"),
            ("collateral_id", "character varying(32)", "押品统一编号"),
            ("loan_contract_no", "character varying(36)", "贷款合同编号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("coll_type_cd", "character varying(8)", "押品大类代码"),
            ("coll_name", "character varying(120)", "押品名称描述"),
            ("eval_value_amt", "numeric(18,2)", "押品评估价值"),
            ("pledge_ratio", "numeric(8,4)", "核定抵质押率"),
            ("guar_cover_amt", "numeric(18,2)", "担保覆盖金额"),
            ("coll_status_cd", "character(2)", "押品登记状态代码"),
            ("reval_dt", "date", "最近重估日期"),
            ("qualified_mitigant_ind", "character(1)", "合格缓释工具标识"),
        ]),
        ("dwd_bank", "dwd_credit_limit", "TABLE", "对公综合授信额度表", [
            ("credit_limit_id", "character varying(32)", "授信额度编号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("approved_limit_amt", "numeric(18,2)", "核定授信总额"),
            ("used_limit_amt", "numeric(18,2)", "已用授信额度"),
            ("value_dt", "date", "放款起息日期"),
            ("maturity_dt", "date", "贷款到期日期"),
            ("valid_ind", "character(1)", "记录有效标识"),
        ]),
        ("dwd_bank", "dwd_credit_card_acct", "TABLE", "信用卡账户表", [
            ("card_no", "character varying(24)", "银行卡号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("card_limit_amt", "numeric(18,2)", "信用卡授信额度"),
            ("card_used_amt", "numeric(18,2)", "信用卡已用额度"),
            ("acct_status_cd", "character(2)", "账户状态代码"),
            ("branch_id", "character varying(16)", "机构编号"),
        ]),
        ("dwd_bank", "dwd_wealth_holding", "TABLE", "个人与机构理财持仓明细表", [
            ("wealth_prod_cd", "character varying(24)", "理财产品代码"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("wealth_share_qty", "numeric(18,4)", "理财持有份额"),
            ("wealth_mkt_val_amt", "numeric(18,2)", "理财持有市值"),
            ("stat_dt", "date", "数据统计日期"),
            ("branch_id", "character varying(16)", "机构编号"),
        ]),
        ("dwd_bank", "dwd_acct_swap_demo", "TABLE", "账户链式改名与保序重建演示靶表（TABLE_REBUILD）", [
            ("acct_code", "character varying(24)", "新主账号候选"),
            ("acct_no", "character varying(24)", "历史老账号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("acct_bal_amt", "numeric(14,2)", "余额"),
            ("open_dt_txt", "character varying(10)", "开户日期字符"),
            ("branch_id", "character varying(16)", "机构编号"),
        ]),
        # DWS 汇总层 8 表
        ("dws_bank", "dws_cust_panoramic_360", "TABLE", "全行客户资产负债全景宽表", [
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("cust_name", "character varying(120)", "客户中文全称"),
            ("cust_type_cd", "character(2)", "客户类型代码"),
            ("vip_tier_cd", "character varying(8)", "客户价值分层代码"),
            ("acct_bal_amt", "numeric(18,2)", "账户活期余额"),
            ("loan_bal_amt", "numeric(18,2)", "贷款本金余额"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("stat_dt", "date", "数据统计日期"),
        ]),
        ("dws_bank", "dws_corp_credit_exposure", "TABLE", "对公授信风险敞口汇总表", [
            ("credit_limit_id", "character varying(32)", "授信额度编号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("approved_limit_amt", "numeric(18,2)", "核定授信总额"),
            ("used_limit_amt", "numeric(18,2)", "已用授信额度"),
            ("stat_dt", "date", "数据统计日期"),
            ("acct_status_cd", "character(2)", "账户状态代码"),
            ("sync_tag", "character varying(24)", "同步标记"),
        ]),
        ("dws_bank", "dws_retail_loan_risk_m", "TABLE", "零售贷款资产质量月度汇总表", [
            ("stat_dt", "date", "数据统计日期"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("loan_bal_amt", "numeric(18,2)", "贷款本金余额"),
            ("npl_bal_amt", "numeric(18,2)", "不良贷款余额"),
            ("npl_ratio", "numeric(8,4)", "不良贷款率"),
        ]),
        ("dws_bank", "dws_branch_deposit_loan_d", "TABLE", "分支机构存贷比日汇总表", [
            ("stat_dt", "date", "数据统计日期"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("branch_name", "character varying(80)", "机构中文名称"),
            ("acct_bal_amt", "numeric(18,2)", "账户活期余额"),
            ("loan_bal_amt", "numeric(18,2)", "贷款本金余额"),
            ("loan_to_dep_ratio", "numeric(8,4)", "存贷比指标值"),
        ]),
        ("dws_bank", "dws_overdue_five_class_d", "TABLE", "逾期账龄与五级分类交叉汇总表", [
            ("stat_dt", "date", "数据统计日期"),
            ("five_class_cd", "character(2)", "五级分类代码"),
            ("overdue_bucket_cd", "character varying(8)", "逾期账龄分段代码"),
            ("loan_bal_amt", "numeric(18,2)", "贷款本金余额"),
            ("overdue_prin_amt", "numeric(18,2)", "逾期本金金额"),
        ]),
        ("dws_bank", "dws_aml_large_txn_alert", "TABLE", "反洗钱大额与拆单可疑交易监控表", [
            ("aml_case_id", "character varying(32)", "可疑案件编号"),
            ("acct_no", "character varying(32)", "银行账号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("split_3d_cnt", "integer", "近3日拆单笔数"),
            ("daily_txn_amt", "numeric(18,2)", "日累计交易金额"),
            ("alert_level_cd", "character varying(8)", "风险预警等级代码"),
            ("alert_rule_cd", "character varying(24)", "预警触发规则代码"),
            ("stat_dt", "date", "数据统计日期"),
        ]),
        ("dws_bank", "dws_cust_value_tier", "TABLE", "全行客户价值分层统计表", [
            ("vip_tier_cd", "character varying(8)", "客户价值分层代码"),
            ("cust_cnt", "integer", "客户数"),
            ("acct_bal_amt", "numeric(18,2)", "账户活期余额"),
            ("wealth_mkt_val_amt", "numeric(18,2)", "理财持有市值"),
        ]),
        ("dws_bank", "dws_interest_pricing_anly", "TABLE", "贷款利率定价偏离分析表", [
            ("branch_id", "character varying(16)", "机构编号"),
            ("loan_prod_cd", "character varying(16)", "贷款产品代码"),
            ("avg_exec_rate", "numeric(10,6)", "平均执行利率"),
            ("lpr_base_rate", "numeric(10,6)", "LPR基准利率"),
            ("rate_spread_bp", "numeric(10,2)", "利率加减点BP"),
            ("disburse_amt", "numeric(18,2)", "放款发放金额"),
        ]),
        # ADS 监管报送层 5 表 + 4 视图
        ("ads_reg", "ads_east_cust_info", "TABLE", "EAST 5.0 客户信息报送表", [
            ("east_batch_no", "character varying(24)", "EAST报送批次号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("cust_name", "character varying(120)", "客户中文全称"),
            ("cert_type_cd", "character varying(8)", "证件类型代码"),
            ("cert_no", "character varying(32)", "证件号码"),
            ("branch_id", "character varying(16)", "机构编号"),
        ]),
        ("ads_reg", "ads_east_loan_detail", "TABLE", "EAST 5.0 贷款借据报送表", [
            ("east_batch_no", "character varying(24)", "EAST报送批次号"),
            ("loan_receipt_no", "character varying(36)", "贷款借据编号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("loan_bal_amt", "numeric(18,2)", "贷款本金余额"),
            ("five_class_cd", "character(2)", "五级分类代码"),
            ("overdue_days", "integer", "逾期天数"),
            ("dq_anomaly_ind", "character(1)", "数据质量异常标识"),
        ]),
        ("ads_reg", "ads_east_large_txn", "TABLE", "EAST 5.0 大额及可疑交易报送表", [
            ("east_batch_no", "character varying(24)", "EAST报送批次号"),
            ("txn_serial_no", "character varying(40)", "交易流水号"),
            ("acct_no", "character varying(32)", "银行账号"),
            ("txn_amt", "numeric(18,2)", "交易发生金额"),
            ("opp_acct_name", "character varying(120)", "对手方户名"),
            ("large_txn_ind", "character(1)", "大额交易上报标识"),
        ]),
        ("ads_reg", "ads_1104_risk_exposure", "TABLE", "1104 非现场监管信用风险敞口报表", [
            ("reg_ind_cd", "character varying(20)", "监管指标代码"),
            ("branch_id", "character varying(16)", "机构编号"),
            ("risk_exposure_amt", "numeric(18,2)", "风险暴露总金额"),
            ("npl_bal_amt", "numeric(18,2)", "不良贷款余额"),
            ("provision_cov_ratio", "numeric(10,4)", "拨备覆盖率"),
        ]),
        ("ads_reg", "ads_aml_suspect_case", "TABLE", "反洗钱可疑案件甄别报送表", [
            ("aml_case_id", "character varying(32)", "可疑案件编号"),
            ("cust_id", "character varying(32)", "客户统一编号"),
            ("acct_no", "character varying(32)", "银行账号"),
            ("anomaly_reason", "character varying(200)", "异常原因说明"),
            ("alert_level_cd", "character varying(8)", "风险预警等级代码"),
        ]),
        ("ads_reg", "v_l1_loan_base", "VIEW", "一级监管宽视图", [
            ("loan_receipt_no", "character varying(36)", ""),
            ("cust_id", "character varying(32)", ""),
            ("branch_id", "character varying(16)", ""),
            ("emp_id", "character varying(16)", ""),
            ("loan_bal_amt", "numeric(18,2)", ""),
            ("exec_int_rate", "numeric(10,6)", ""),
            ("five_class_cd", "character(2)", ""),
            ("overdue_days", "integer", ""),
            ("main_guar_type_cd", "character(2)", ""),
        ]),
        ("ads_reg", "v_l2_loan_overdue", "VIEW", "二级逾期甄别视图", [
            ("loan_receipt_no", "character varying(36)", ""),
            ("cust_id", "character varying(32)", ""),
            ("branch_id", "character varying(16)", ""),
            ("emp_id", "character varying(16)", ""),
            ("loan_bal_amt", "numeric(18,2)", ""),
            ("five_class_cd", "character(2)", ""),
            ("overdue_days", "integer", ""),
            ("dq_anomaly_ind", "text", ""),
        ]),
        ("ads_reg", "v_l3_branch_risk", "VIEW", "三级支行汇总视图", [
            ("branch_id", "character varying(16)", ""),
            ("anomaly_loan_cnt", "bigint", ""),
            ("risk_exposure_amt", "numeric", ""),
            ("concealed_npl_amt", "numeric", ""),
        ]),
        ("ads_reg", "v_l4_east_credit_summary", "VIEW", "四级EAST信用汇总视图", [
            ("branch_id", "character varying(16)", ""),
            ("branch_name", "character varying(80)", ""),
            ("anomaly_loan_cnt", "bigint", ""),
            ("risk_exposure_amt", "numeric", ""),
            ("concealed_npl_amt", "numeric", ""),
        ]),
    ]

    # 星环 Inceptor 表结构（刻意采用 STRING / DOUBLE / DECIMAL(20,4) / VARCHAR(200)）
    inceptor_type_overrides: Dict[Tuple[str, str], str] = {
        ("dwd_cust_base_info", "khbh"): "VARCHAR(32)",
        ("dwd_cust_base_info", "cust_nm"): "VARCHAR(200)",
        ("dwd_cust_base_info", "zjhm"): "VARCHAR(32)",
        ("dwd_cust_base_info", "reg_addr"): "STRING",
        ("dwd_acct_deposit_info", "acct_name"): "VARCHAR(200)",
        ("dwd_acct_deposit_info", "acct_bal_amt"): "DOUBLE",
        ("dwd_acct_deposit_info", "avail_bal_amt"): "DECIMAL(20,4)",
        ("dwd_acct_deposit_info", "frozen_amt"): "DECIMAL(20,4)",
        ("dwd_acct_deposit_info", "dep_int_rate"): "DOUBLE",
        ("dwd_loan_contract", "dkje"): "DOUBLE",
        ("dwd_loan_contract", "zxlv"): "DOUBLE",
        ("dwd_loan_contract", "dkqx"): "BIGINT",
        ("dwd_loan_contract", "sign_dt_str"): "STRING",
        ("dwd_loan_receipt", "disburse_amt"): "DECIMAL(20,4)",
        ("dwd_loan_receipt", "loan_bal_amt"): "DECIMAL(20,4)",
        ("dwd_loan_receipt", "exec_int_rate"): "DOUBLE",
        ("dwd_loan_receipt", "overdue_days"): "BIGINT",
        ("dwd_loan_receipt", "overdue_prin_amt"): "DECIMAL(20,4)",
        ("dwd_txn_detail", "txn_amt"): "DECIMAL(20,4)",
        ("dwd_txn_detail", "after_bal_amt"): "DECIMAL(20,4)",
        ("dwd_txn_detail", "opp_acct_name"): "VARCHAR(200)",
        ("dwd_txn_detail", "txn_summary"): "STRING",
        ("dwd_corp_cust_ext", "reg_capital_amt"): "DECIMAL(20,4)",
        ("dwd_collateral_info", "pledge_ratio"): "DOUBLE",
        ("dws_cust_panoramic_360", "cust_name"): "VARCHAR(200)",
        ("dws_cust_panoramic_360", "acct_bal_amt"): "DECIMAL(20,4)",
        ("dws_cust_panoramic_360", "loan_bal_amt"): "DECIMAL(20,4)",
        ("dws_branch_deposit_loan_d", "loan_to_dep_ratio"): "DOUBLE",
        ("ads_east_cust_info", "cust_name"): "VARCHAR(200)",
        ("ads_1104_risk_exposure", "provision_cov_ratio"): "DOUBLE",
    }

    inc_keep_names = {
        "dwd_org_branch_info",
        "dwd_emp_manager_info",
        "dwd_cust_base_info",
        "dwd_corp_cust_ext",
        "dwd_acct_deposit_info",
        "dwd_loan_contract",
        "dwd_loan_receipt",
        "dwd_loan_repay_plan",
        "dwd_txn_detail",
        "dwd_collateral_info",
        "dwd_credit_limit",
        "dwd_credit_card_acct",
        "dwd_wealth_holding",
        "dws_cust_panoramic_360",
        "dws_corp_credit_exposure",
        "dws_branch_deposit_loan_d",
        "dws_aml_large_txn_alert",
        "ads_east_cust_info",
        "ads_east_loan_detail",
        "ads_1104_risk_exposure",
        "v_l1_loan_base",
        "v_l2_loan_overdue",
        "v_l3_branch_risk",
        "v_l4_east_credit_summary",
    }

    inc_tables: List[Tuple[str, str, str, str, List[Tuple[str, str, str]]]] = []
    for _sch, tbl, otype, tcomm, cols in dws_tables:
        if tbl not in inc_keep_names:
            continue
        inc_cols: List[Tuple[str, str, str]] = []
        for cname, ctype, ccomm in cols:
            ov = inceptor_type_overrides.get((tbl, cname))
            if ov:
                it = ov
            elif "character varying" in ctype:
                it = ctype.replace("character varying", "VARCHAR").upper()
            elif "character(" in ctype:
                it = ctype.replace("character(", "CHAR(").upper()
            elif "numeric" in ctype:
                it = ctype.replace("numeric", "DECIMAL").upper()
            elif ctype == "smallint":
                it = "BIGINT"
            elif ctype == "integer":
                it = "INT"
            elif "timestamp" in ctype:
                it = "TIMESTAMP"
            else:
                it = ctype.upper()
            inc_cols.append((cname, it, ccomm))
        inc_tables.append(("bank_inceptor", tbl, otype, f"星环{tcomm}", inc_cols))

    # 2 张星环独有历史归档表
    inc_tables.extend(
        [
            ("bank_inceptor", "his_legacy_bill_2022", "TABLE", "星环2022旧票据系统归档表（单侧独有）", [
                ("bill_no", "STRING", "历史票据编号"),
                ("cust_id", "STRING", "客户统一编号"),
                ("bill_amt", "DECIMAL(18,2)", "票面金额"),
                ("issue_dt", "STRING", "出票日期"),
            ]),
            ("bank_inceptor", "his_legacy_guar_2022", "TABLE", "星环2022旧保函归档表（单侧独有）", [
                ("guar_no", "STRING", "旧保函编号"),
                ("cust_id", "STRING", "客户统一编号"),
                ("guar_amt", "DOUBLE", "担保金额"),
                ("expire_dt", "STRING", "到期日"),
            ]),
        ]
    )

    mysql_tables: List[Tuple[str, str, str, str, List[Tuple[str, str, str]]]] = [
        ("core_bank_ods", "t_core_branch", "TABLE", "核心系统机构表", dws_tables[0][4]),
        ("core_bank_ods", "t_core_emp", "TABLE", "核心系统员工表", dws_tables[1][4]),
        ("core_bank_ods", "t_core_customer", "TABLE", "核心系统客户主档表", dws_tables[3][4]),
        ("core_bank_ods", "t_core_corp_credit", "TABLE", "核心对公授信主表（含3户未入仓差额3650万）", dws_tables[15][4]),
        ("core_bank_ods", "t_core_loan_contract", "TABLE", "核心贷款合同主表", dws_tables[5][4]),
        ("core_bank_ods", "t_core_loan_receipt", "TABLE", "核心贷款借据分户账", dws_tables[6][4]),
        ("core_bank_ods", "t_core_deposit_acct", "TABLE", "核心存款账户主表", dws_tables[4][4]),
        ("core_bank_ods", "t_core_txn_journal", "TABLE", "核心交易联机流水表", dws_tables[8][4]),
        ("core_bank_ods", "t_core_collateral", "TABLE", "核心押品登记表", dws_tables[9][4]),
        ("core_bank_ods", "t_core_repay_log", "TABLE", "核心还款计划流水表", dws_tables[7][4]),
    ]

    return {
        CONN_DWS_ID: dws_tables,
        CONN_INC_ID: inc_tables,
        CONN_MY_ID: mysql_tables,
    }


def _persist_table_specs(
    ctx: PlatformContext,
    conn_id: str,
    specs: List[Tuple[str, str, str, str, List[Tuple[str, str, str]]]],
) -> None:
    by_schema: Dict[str, List[int]] = {}
    snap_tables: List[Dict[str, Any]] = []
    for sch, tbl, otype, tcomm, _cols in specs:
        counts = by_schema.setdefault(sch, [0, 0])
        counts[1 if otype == "VIEW" else 0] += 1
        snap_tables.append(
            {
                "schema_name": sch,
                "table_name": tbl,
                "object_type": otype,
                "table_comment": tcomm,
            }
        )
    schemas = [
        SchemaMeta(schema_name=s, table_count=tc, view_count=vc)
        for s, (tc, vc) in by_schema.items()
    ]
    ctx.store.save_catalog_snapshot(conn_id, schemas=schemas, tables=snap_tables)
    for sch, tbl, otype, tcomm, cols in specs:
        col_objs = [_col(idx, cn, ct, cc, pk=(idx == 1)) for idx, (cn, ct, cc) in enumerate(cols, start=1)]
        pks = [cols[0][0]] if (cols and otype == "TABLE") else []
        t_meta = TableMeta(
            schema_name=sch,
            table_name=tbl,
            exists=True,
            object_type=otype,
            table_comment=tcomm,
            primary_keys=pks,
            columns=col_objs,
        )
        ctx.store.save_table_meta(conn_id, t_meta)


def _probe_live_metadata(ctx: PlatformContext) -> None:
    """当本地 Docker 三引擎在线时，直接用 Data.O.G MetadataHub 真实采集目录与列元数据."""
    schema_targets = {
        CONN_DWS_ID: ["dwd_bank", "dws_bank", "ads_reg"],
        CONN_INC_ID: [INCEPTOR_DB],
        CONN_MY_ID: [MYSQL_DB],
    }
    for cid, sch_list in schema_targets.items():
        cat = ctx.metadata_hub.refresh_catalog(cid)
        pairs: List[Tuple[str, str]] = []
        for t in cat.get("tables") or []:
            s = str(t.get("schema_name") or "")
            n = str(t.get("table_name") or "")
            if s in sch_list and n:
                pairs.append((s, n))
        if pairs:
            ctx.metadata_hub.get_tables_metadata_batch(
                cid, pairs, force_refresh=True, tolerate_errors=True
            )


def _evaluate_and_save_bindings(
    ctx: PlatformContext,
    conn_id: str,
    db_type: str,
    format_profile: str,
    rows: List[Tuple[str, str, str, str]],
) -> None:
    """写入 field_standard_bindings 并基于缓存表结构计算三维分级落标状态."""
    std_map = get_standard_by_code()
    to_save: List[Dict[str, Any]] = []
    table_cache: Dict[Tuple[str, str], Dict[str, ColumnMeta]] = {}
    for sch, tbl, col, std_code in rows:
        tkey = (sch.lower(), tbl.lower())
        if tkey not in table_cache:
            meta = ctx.store.get_cached_table_meta(conn_id, sch, tbl)
            cmap = {c.column_name.lower(): c for c in meta.columns} if meta else {}
            table_cache[tkey] = cmap
        cobj = table_cache[tkey].get(col.lower())
        std_item = std_map.get(std_code)
        if cobj and std_item:
            ev = StandardCatalog.evaluate_column_compliance(
                col_name=cobj.column_name,
                col_type=cobj.data_type,
                col_comment=cobj.column_comment,
                std_item=std_item,
                std_code=std_code,
                db_type=db_type,
                format_profile=format_profile,
            )
            status = ev["status"]
            detail = ev["detail"]
        else:
            status = "UNCHECKED"
            detail = ""
        to_save.append(
            {
                "schema_name": sch,
                "table_name": tbl,
                "column_name": col,
                "std_code": std_code,
                "bind_source": "MANUAL",
                "compliance_status": status,
                "compliance_detail": detail,
            }
        )
    ctx.standard_catalog.bulk_save_field_bindings(
        conn_id, to_save, mode="merge", bind_source="MANUAL"
    )


def build_live_sandbox_workspace(
    output_dir: str,
    *,
    live_probe: bool = False,
) -> Tuple[str, Dict[str, Any]]:
    """生成 `qiming_live_sandbox.dataog.db`（启明银行 · 活库现场演练工作区）."""
    os.makedirs(output_dir, exist_ok=True)
    dict_xlsx = os.path.join(output_dir, "qiming_bank_data_standards_v2026.xlsx")
    export_standard_dictionary_xlsx(dict_xlsx)
    export_binding_import_template_xlsx(
        os.path.join(output_dir, "qiming_dws_binding_import_demo.xlsx")
    )

    with tempfile.TemporaryDirectory(prefix="dataog_demo_live_ws_") as tmp_priv:
        ctx = PlatformContext(private_root=tmp_priv, legacy_data_dir="")
        for prof in build_demo_connection_profiles(include_secrets=True):
            ctx.store.upsert_connection_profile(prof)

        with open(dict_xlsx, "rb") as fh:
            ctx.standard_catalog.import_dictionary_excel(
                "qiming_bank_data_standards_v2026.xlsx",
                fh.read(),
                mode="replace",
            )

        if live_probe:
            _probe_live_metadata(ctx)
        else:
            specs = _offline_live_table_specs()
            for cid, tbl_list in specs.items():
                _persist_table_specs(ctx, cid, tbl_list)

        # 为 dwd_acct_swap_demo 补齐 DWS 列存与分布键元数据提示（PG16 无原生 DWS 系统表，保留空 with/distribute 子句以兼容真库重建 DDL）
        swap_meta = ctx.store.get_cached_table_meta(CONN_DWS_ID, "dwd_bank", "dwd_acct_swap_demo")
        if swap_meta is not None:
            swap_meta.orientation = "COLUMN"
            swap_meta.distribute_keys = ["acct_code"]
            swap_meta.with_clause = ""
            swap_meta.distribute_clause = ""
            ctx.store.save_table_meta(CONN_DWS_ID, swap_meta)

        # DWS 核心字段数标绑定（刻意保留 vip_tier_cd/etl_load_ts 及 2 张表末绑定，供现场演示）
        dws_bindings: List[Tuple[str, str, str, str]] = [
            # dwd_org_branch_info (100% COMPLIANT 样板表)
            ("dwd_bank", "dwd_org_branch_info", "branch_id", "QM_ORG_001"),
            ("dwd_bank", "dwd_org_branch_info", "branch_name", "QM_ORG_002"),
            ("dwd_bank", "dwd_org_branch_info", "parent_branch_id", "QM_ORG_003"),
            ("dwd_bank", "dwd_org_branch_info", "branch_lvl_cd", "QM_ORG_004"),
            ("dwd_bank", "dwd_org_branch_info", "region_cd", "QM_ORG_005"),
            ("dwd_bank", "dwd_org_branch_info", "fin_license_no", "QM_ORG_006"),
            ("dwd_bank", "dwd_org_branch_info", "valid_ind", "QM_ORG_012"),
            # dwd_emp_manager_info (100% COMPLIANT)
            ("dwd_bank", "dwd_emp_manager_info", "emp_id", "QM_ORG_007"),
            ("dwd_bank", "dwd_emp_manager_info", "emp_name", "QM_ORG_008"),
            ("dwd_bank", "dwd_emp_manager_info", "branch_id", "QM_ORG_001"),
            ("dwd_bank", "dwd_emp_manager_info", "dept_cd", "QM_ORG_009"),
            ("dwd_bank", "dwd_emp_manager_info", "valid_ind", "QM_ORG_012"),
            ("dwd_bank", "dwd_emp_manager_info", "etl_load_ts", "QM_ORG_013"),
            # dwd_cust_base_info (NON_COMPLIANT + COMMENT_ONLY + 2列留白UNBOUND)
            ("dwd_bank", "dwd_cust_base_info", "khbh", "QM_CUST_001"),
            ("dwd_bank", "dwd_cust_base_info", "cust_nm", "QM_CUST_002"),
            ("dwd_bank", "dwd_cust_base_info", "cust_type_cd", "QM_CUST_003"),
            ("dwd_bank", "dwd_cust_base_info", "cert_type_cd", "QM_CUST_004"),
            ("dwd_bank", "dwd_cust_base_info", "zjhm", "QM_CUST_005"),
            ("dwd_bank", "dwd_cust_base_info", "country_cd", "QM_CUST_008"),
            ("dwd_bank", "dwd_cust_base_info", "industry_cd", "QM_CUST_009"),
            ("dwd_bank", "dwd_cust_base_info", "cust_risk_lvl_cd", "QM_CUST_011"),
            ("dwd_bank", "dwd_cust_base_info", "mobile_no", "QM_CUST_012"),
            ("dwd_bank", "dwd_cust_base_info", "reg_addr", "QM_CUST_014"),
            ("dwd_bank", "dwd_cust_base_info", "cust_status_cd", "QM_CUST_019"),
            ("dwd_bank", "dwd_cust_base_info", "cust_open_dt", "QM_CUST_025"),
            ("dwd_bank", "dwd_cust_base_info", "branch_id", "QM_ORG_001"),
            ("dwd_bank", "dwd_cust_base_info", "emp_id", "QM_ORG_007"),
            # dwd_loan_contract (DIRECT_ALTER 靶表)
            ("dwd_bank", "dwd_loan_contract", "htbh", "QM_LOAN_001"),
            ("dwd_bank", "dwd_loan_contract", "khbh", "QM_CUST_001"),
            ("dwd_bank", "dwd_loan_contract", "loan_prod_cd", "QM_LOAN_004"),
            ("dwd_bank", "dwd_loan_contract", "dkje", "QM_LOAN_006"),
            ("dwd_bank", "dwd_loan_contract", "zxlv", "QM_LOAN_011"),
            ("dwd_bank", "dwd_loan_contract", "dkqx", "QM_LOAN_014"),
            ("dwd_bank", "dwd_loan_contract", "sign_dt_str", "QM_LOAN_015"),
            ("dwd_bank", "dwd_loan_contract", "main_guar_type_cd", "QM_LOAN_029"),
            ("dwd_bank", "dwd_loan_contract", "branch_id", "QM_ORG_001"),
            ("dwd_bank", "dwd_loan_contract", "emp_id", "QM_ORG_007"),
            # dwd_acct_swap_demo (TABLE_REBUILD 链式改名靶表)
            ("dwd_bank", "dwd_acct_swap_demo", "acct_code", "QM_ACCT_001"),
            ("dwd_bank", "dwd_acct_swap_demo", "acct_no", "QM_ACCT_002"),
            ("dwd_bank", "dwd_acct_swap_demo", "cust_id", "QM_CUST_001"),
            ("dwd_bank", "dwd_acct_swap_demo", "acct_bal_amt", "QM_ACCT_008"),
            ("dwd_bank", "dwd_acct_swap_demo", "open_dt_txt", "QM_ACCT_006"),
            ("dwd_bank", "dwd_acct_swap_demo", "branch_id", "QM_ORG_001"),
            # dwd_loan_receipt
            ("dwd_bank", "dwd_loan_receipt", "loan_receipt_no", "QM_LOAN_002"),
            ("dwd_bank", "dwd_loan_receipt", "loan_contract_no", "QM_LOAN_001"),
            ("dwd_bank", "dwd_loan_receipt", "cust_id", "QM_CUST_001"),
            ("dwd_bank", "dwd_loan_receipt", "branch_id", "QM_ORG_001"),
            ("dwd_bank", "dwd_loan_receipt", "emp_id", "QM_ORG_007"),
            ("dwd_bank", "dwd_loan_receipt", "loan_prod_cd", "QM_LOAN_004"),
            ("dwd_bank", "dwd_loan_receipt", "currency_cd", "QM_ACCT_012"),
            ("dwd_bank", "dwd_loan_receipt", "disburse_amt", "QM_LOAN_007"),
            ("dwd_bank", "dwd_loan_receipt", "loan_bal_amt", "QM_LOAN_008"),
            ("dwd_bank", "dwd_loan_receipt", "exec_int_rate", "QM_LOAN_011"),
            ("dwd_bank", "dwd_loan_receipt", "value_dt", "QM_LOAN_016"),
            ("dwd_bank", "dwd_loan_receipt", "maturity_dt", "QM_LOAN_017"),
            ("dwd_bank", "dwd_loan_receipt", "five_class_cd", "QM_LOAN_018"),
            ("dwd_bank", "dwd_loan_receipt", "overdue_days", "QM_LOAN_019"),
            ("dwd_bank", "dwd_loan_receipt", "overdue_prin_amt", "QM_LOAN_020"),
            ("dwd_bank", "dwd_loan_receipt", "on_bs_int_amt", "QM_LOAN_021"),
            ("dwd_bank", "dwd_loan_receipt", "off_bs_int_amt", "QM_LOAN_022"),
            ("dwd_bank", "dwd_loan_receipt", "main_guar_type_cd", "QM_LOAN_029"),
            ("dwd_bank", "dwd_loan_receipt", "receipt_status_cd", "QM_LOAN_030"),
            # dwd_txn_detail
            ("dwd_bank", "dwd_txn_detail", "txn_serial_no", "QM_TXN_001"),
            ("dwd_bank", "dwd_txn_detail", "acct_no", "QM_ACCT_001"),
            ("dwd_bank", "dwd_txn_detail", "cust_id", "QM_CUST_001"),
            ("dwd_bank", "dwd_txn_detail", "txn_dt", "QM_TXN_002"),
            ("dwd_bank", "dwd_txn_detail", "txn_ts", "QM_TXN_003"),
            ("dwd_bank", "dwd_txn_detail", "dc_flag_cd", "QM_TXN_004"),
            ("dwd_bank", "dwd_txn_detail", "txn_amt", "QM_TXN_005"),
            ("dwd_bank", "dwd_txn_detail", "after_bal_amt", "QM_TXN_006"),
            ("dwd_bank", "dwd_txn_detail", "txn_channel_cd", "QM_TXN_007"),
            ("dwd_bank", "dwd_txn_detail", "txn_type_cd", "QM_TXN_008"),
            ("dwd_bank", "dwd_txn_detail", "txn_summary", "QM_TXN_009"),
            ("dwd_bank", "dwd_txn_detail", "opp_acct_no", "QM_TXN_010"),
            ("dwd_bank", "dwd_txn_detail", "opp_acct_name", "QM_TXN_011"),
            ("dwd_bank", "dwd_txn_detail", "opp_bank_cd", "QM_TXN_012"),
            ("dwd_bank", "dwd_txn_detail", "cash_trf_ind", "QM_TXN_014"),
            ("dwd_bank", "dwd_txn_detail", "large_txn_ind", "QM_TXN_015"),
            ("dwd_bank", "dwd_txn_detail", "aml_suspect_ind", "QM_TXN_016"),
            ("dwd_bank", "dwd_txn_detail", "branch_id", "QM_ORG_001"),
            # dwd_corp_cust_ext
            ("dwd_bank", "dwd_corp_cust_ext", "cust_id", "QM_CUST_001"),
            ("dwd_bank", "dwd_corp_cust_ext", "uscc_cd", "QM_CUST_006"),
            ("dwd_bank", "dwd_corp_cust_ext", "ent_scale_cd", "QM_CUST_010"),
            ("dwd_bank", "dwd_corp_cust_ext", "estab_dt", "QM_CUST_016"),
            ("dwd_bank", "dwd_corp_cust_ext", "reg_capital_amt", "QM_CUST_017"),
            ("dwd_bank", "dwd_corp_cust_ext", "legal_rep_name", "QM_CUST_018"),
            ("dwd_bank", "dwd_corp_cust_ext", "group_cust_ind", "QM_CUST_020"),
            ("dwd_bank", "dwd_corp_cust_ext", "group_id", "QM_CUST_021"),
            ("dwd_bank", "dwd_corp_cust_ext", "industry_cd", "QM_CUST_009"),
            ("dwd_bank", "dwd_corp_cust_ext", "branch_id", "QM_ORG_001"),
            # dwd_acct_deposit_info
            ("dwd_bank", "dwd_acct_deposit_info", "acct_no", "QM_ACCT_001"),
            ("dwd_bank", "dwd_acct_deposit_info", "cust_id", "QM_CUST_001"),
            ("dwd_bank", "dwd_acct_deposit_info", "acct_name", "QM_ACCT_003"),
            ("dwd_bank", "dwd_acct_deposit_info", "acct_type_cd", "QM_ACCT_004"),
            ("dwd_bank", "dwd_acct_deposit_info", "acct_status_cd", "QM_ACCT_005"),
            ("dwd_bank", "dwd_acct_deposit_info", "open_dt", "QM_ACCT_006"),
            ("dwd_bank", "dwd_acct_deposit_info", "acct_bal_amt", "QM_ACCT_008"),
            ("dwd_bank", "dwd_acct_deposit_info", "avail_bal_amt", "QM_ACCT_009"),
            ("dwd_bank", "dwd_acct_deposit_info", "frozen_amt", "QM_ACCT_010"),
            ("dwd_bank", "dwd_acct_deposit_info", "currency_cd", "QM_ACCT_012"),
            ("dwd_bank", "dwd_acct_deposit_info", "dep_int_rate", "QM_ACCT_013"),
            ("dwd_bank", "dwd_acct_deposit_info", "branch_id", "QM_ORG_001"),
            # dwd_collateral_info
            ("dwd_bank", "dwd_collateral_info", "guar_contract_no", "QM_COLL_001"),
            ("dwd_bank", "dwd_collateral_info", "collateral_id", "QM_COLL_002"),
            ("dwd_bank", "dwd_collateral_info", "loan_contract_no", "QM_LOAN_001"),
            ("dwd_bank", "dwd_collateral_info", "cust_id", "QM_CUST_001"),
            ("dwd_bank", "dwd_collateral_info", "coll_type_cd", "QM_COLL_003"),
            ("dwd_bank", "dwd_collateral_info", "coll_name", "QM_COLL_004"),
            ("dwd_bank", "dwd_collateral_info", "eval_value_amt", "QM_COLL_005"),
            ("dwd_bank", "dwd_collateral_info", "pledge_ratio", "QM_COLL_006"),
            ("dwd_bank", "dwd_collateral_info", "guar_cover_amt", "QM_COLL_007"),
            ("dwd_bank", "dwd_collateral_info", "coll_status_cd", "QM_COLL_010"),
            ("dwd_bank", "dwd_collateral_info", "reval_dt", "QM_COLL_011"),
            ("dwd_bank", "dwd_collateral_info", "qualified_mitigant_ind", "QM_COLL_014"),
            # dwd_credit_limit
            ("dwd_bank", "dwd_credit_limit", "credit_limit_id", "QM_LOAN_003"),
            ("dwd_bank", "dwd_credit_limit", "cust_id", "QM_CUST_001"),
            ("dwd_bank", "dwd_credit_limit", "branch_id", "QM_ORG_001"),
            ("dwd_bank", "dwd_credit_limit", "approved_limit_amt", "QM_LOAN_009"),
            ("dwd_bank", "dwd_credit_limit", "used_limit_amt", "QM_LOAN_010"),
            ("dwd_bank", "dwd_credit_limit", "value_dt", "QM_LOAN_016"),
            ("dwd_bank", "dwd_credit_limit", "maturity_dt", "QM_LOAN_017"),
            ("dwd_bank", "dwd_credit_limit", "valid_ind", "QM_ORG_012"),
            # DWS 汇总与 ADS 监管
            ("dws_bank", "dws_cust_panoramic_360", "cust_id", "QM_CUST_001"),
            ("dws_bank", "dws_cust_panoramic_360", "cust_name", "QM_CUST_002"),
            ("dws_bank", "dws_cust_panoramic_360", "cust_type_cd", "QM_CUST_003"),
            ("dws_bank", "dws_cust_panoramic_360", "vip_tier_cd", "QM_CUST_015"),
            ("dws_bank", "dws_cust_panoramic_360", "acct_bal_amt", "QM_ACCT_008"),
            ("dws_bank", "dws_cust_panoramic_360", "loan_bal_amt", "QM_LOAN_008"),
            ("dws_bank", "dws_cust_panoramic_360", "branch_id", "QM_ORG_001"),
            ("dws_bank", "dws_cust_panoramic_360", "stat_dt", "QM_ORG_010"),
            ("dws_bank", "dws_corp_credit_exposure", "credit_limit_id", "QM_LOAN_003"),
            ("dws_bank", "dws_corp_credit_exposure", "cust_id", "QM_CUST_001"),
            ("dws_bank", "dws_corp_credit_exposure", "branch_id", "QM_ORG_001"),
            ("dws_bank", "dws_corp_credit_exposure", "approved_limit_amt", "QM_LOAN_009"),
            ("dws_bank", "dws_corp_credit_exposure", "used_limit_amt", "QM_LOAN_010"),
            ("dws_bank", "dws_corp_credit_exposure", "stat_dt", "QM_ORG_010"),
            ("dws_bank", "dws_corp_credit_exposure", "acct_status_cd", "QM_ACCT_005"),
            ("dws_bank", "dws_aml_large_txn_alert", "aml_case_id", "QM_RISK_013"),
            ("dws_bank", "dws_aml_large_txn_alert", "acct_no", "QM_ACCT_001"),
            ("dws_bank", "dws_aml_large_txn_alert", "cust_id", "QM_CUST_001"),
            ("dws_bank", "dws_aml_large_txn_alert", "split_3d_cnt", "QM_TXN_023"),
            ("dws_bank", "dws_aml_large_txn_alert", "daily_txn_amt", "QM_TXN_022"),
            ("dws_bank", "dws_aml_large_txn_alert", "alert_level_cd", "QM_RISK_012"),
            ("dws_bank", "dws_aml_large_txn_alert", "alert_rule_cd", "QM_RISK_014"),
            ("dws_bank", "dws_aml_large_txn_alert", "stat_dt", "QM_ORG_010"),
            ("ads_reg", "ads_east_cust_info", "east_batch_no", "QM_RISK_010"),
            ("ads_reg", "ads_east_cust_info", "cust_id", "QM_CUST_001"),
            ("ads_reg", "ads_east_cust_info", "cust_name", "QM_CUST_002"),
            ("ads_reg", "ads_east_cust_info", "cert_type_cd", "QM_CUST_004"),
            ("ads_reg", "ads_east_cust_info", "cert_no", "QM_CUST_005"),
            ("ads_reg", "ads_east_cust_info", "branch_id", "QM_ORG_001"),
            ("ads_reg", "ads_east_loan_detail", "east_batch_no", "QM_RISK_010"),
            ("ads_reg", "ads_east_loan_detail", "loan_receipt_no", "QM_LOAN_002"),
            ("ads_reg", "ads_east_loan_detail", "cust_id", "QM_CUST_001"),
            ("ads_reg", "ads_east_loan_detail", "loan_bal_amt", "QM_LOAN_008"),
            ("ads_reg", "ads_east_loan_detail", "five_class_cd", "QM_LOAN_018"),
            ("ads_reg", "ads_east_loan_detail", "overdue_days", "QM_LOAN_019"),
            ("ads_reg", "ads_east_loan_detail", "dq_anomaly_ind", "QM_RISK_017"),
        ]
        _evaluate_and_save_bindings(ctx, CONN_DWS_ID, "DWS", "type_dws", dws_bindings)

        # 星环端字段数标绑定（刻意构造 SPLIT_DIFF 异标双胶囊与 SPLIT_PARTIAL 单侧绑定）
        inc_overrides = {
            ("dwd_cust_base_info", "zjhm"): "QM_CUST_006",  # 星环绑统一社会信用代码 vs DWS绑证件号码 -> SPLIT_DIFF
            ("dwd_cust_base_info", "vip_tier_cd"): "QM_CUST_015",  # DWS侧刻意未绑 -> SPLIT_PARTIAL
            ("dwd_cust_base_info", "etl_load_ts"): "QM_ORG_013",  # DWS侧刻意未绑 -> SPLIT_PARTIAL
            ("dwd_loan_receipt", "loan_bal_amt"): "QM_RISK_001",  # 星环绑风险暴露 vs DWS绑贷款本金余额 -> SPLIT_DIFF
            ("dws_corp_credit_exposure", "sync_tag"): "QM_ORG_011",  # DWS侧未绑 -> SPLIT_PARTIAL
        }
        inc_bindings: List[Tuple[str, str, str, str]] = []
        seen_inc: set[Tuple[str, str]] = set()
        for _sch, tbl, col, std_code in dws_bindings:
            if tbl in ("dwd_acct_swap_demo",):
                continue
            eff_code = inc_overrides.get((tbl, col), std_code)
            inc_bindings.append((INCEPTOR_DB, tbl, col, eff_code))
            seen_inc.add((tbl.lower(), col.lower()))
        for (tbl, col), code in inc_overrides.items():
            if (tbl.lower(), col.lower()) not in seen_inc:
                inc_bindings.append((INCEPTOR_DB, tbl, col, code))
        _evaluate_and_save_bindings(ctx, CONN_INC_ID, "INCEPTOR", "type_argodb", inc_bindings)

        # 构造 15 对跨环境映射（保留 5 张同名未保存表供推荐列表展示）
        cross_pair_defs = [
            ("dwd_bank", "dwd_cust_base_info"),
            ("dwd_bank", "dwd_loan_contract"),
            ("dwd_bank", "dwd_loan_receipt"),
            ("dwd_bank", "dwd_txn_detail"),
            ("dwd_bank", "dwd_acct_deposit_info"),
            ("dwd_bank", "dwd_corp_cust_ext"),
            ("dwd_bank", "dwd_collateral_info"),
            ("dwd_bank", "dwd_credit_limit"),
            ("dwd_bank", "dwd_org_branch_info"),
            ("dwd_bank", "dwd_emp_manager_info"),
            ("dws_bank", "dws_cust_panoramic_360"),
            ("dws_bank", "dws_corp_credit_exposure"),
            ("dws_bank", "dws_aml_large_txn_alert"),
            ("ads_reg", "ads_east_cust_info"),
            ("ads_reg", "ads_east_loan_detail"),
        ]
        std_by_code = get_standard_by_code()
        dws_bind_lookup = {(t.lower(), c.lower()): sc for _s, t, c, sc in dws_bindings}
        inc_bind_lookup = {(t.lower(), c.lower()): sc for _s, t, c, sc in inc_bindings}

        cross_items: List[Dict[str, Any]] = []
        for tgt_sch, tbl in cross_pair_defs:
            tgt_meta = ctx.store.get_cached_table_meta(CONN_DWS_ID, tgt_sch, tbl)
            src_meta = ctx.store.get_cached_table_meta(CONN_INC_ID, INCEPTOR_DB, tbl)
            if not tgt_meta or not src_meta:
                continue
            src_col_names = {c.column_name.lower(): c for c in src_meta.columns}
            for r_idx, t_col in enumerate(tgt_meta.columns, start=1):
                c_low = t_col.column_name.lower()
                s_col = src_col_names.get(c_low)
                t_code = dws_bind_lookup.get((tbl.lower(), c_low), "")
                s_code = inc_bind_lookup.get((tbl.lower(), c_low), "")
                t_std = std_by_code.get(t_code or s_code)
                std_name = t_std["en_abbr"].lower() if t_std else t_col.column_name
                std_type = t_std["type_dws"] if t_std else t_col.data_type
                std_comm = t_std["cn_name"] if t_std else t_col.column_comment
                cross_items.append(
                    {
                        "row_index": r_idx,
                        "std_schema_name": tgt_sch,
                        "std_table_name": tbl,
                        "std_table_comment": tgt_meta.table_comment,
                        "source_schema": INCEPTOR_DB,
                        "source_table": tbl,
                        "target_schema": tgt_sch,
                        "target_table": tbl,
                        "orig_col_name": s_col.column_name if s_col else t_col.column_name,
                        "orig_col_type": s_col.data_type if s_col else t_col.data_type,
                        "orig_col_comment": s_col.column_comment if s_col else t_col.column_comment,
                        "std_col_name": std_name,
                        "std_col_type": std_type,
                        "std_col_comment": std_comm,
                        "bound_source_col": s_col.column_name if s_col else "",
                        "bound_target_col": t_col.column_name,
                        "source_std_code": s_code,
                        "target_std_code": t_code,
                    }
                )
        ctx.store.replace_cross_standards(cross_items, "启明银行星环→DWS跨环境映射清单")

        # 预置待贯标任务清单（dwd_loan_contract DIRECT_ALTER + dwd_acct_swap_demo TABLE_REBUILD）
        mig_items: List[Dict[str, Any]] = [
            {
                "row_index": 1,
                "schema_name": "dwd_bank",
                "table_name": "dwd_loan_contract",
                "old_col_name": "htbh",
                "old_col_type": "character varying(24)",
                "old_col_comment": "老合同号",
                "new_col_name": "loan_contract_no",
                "new_col_type": "VARCHAR(36)",
                "new_col_comment": "贷款合同编号",
                "ordinal_position": 1,
                "total_columns": 10,
                "db_current_col_name": "htbh",
                "db_current_type": "character varying(24)",
                "db_current_comment": "老合同号",
            },
            {
                "row_index": 2,
                "schema_name": "dwd_bank",
                "table_name": "dwd_loan_contract",
                "old_col_name": "khbh",
                "old_col_type": "character varying(18)",
                "old_col_comment": "客户号",
                "new_col_name": "cust_id",
                "new_col_type": "VARCHAR(32)",
                "new_col_comment": "客户统一编号",
                "ordinal_position": 2,
                "total_columns": 10,
                "db_current_col_name": "khbh",
                "db_current_type": "character varying(18)",
                "db_current_comment": "客户号",
            },
            {
                "row_index": 3,
                "schema_name": "dwd_bank",
                "table_name": "dwd_loan_contract",
                "old_col_name": "loan_prod_cd",
                "old_col_type": "character varying(16)",
                "old_col_comment": "",
                "new_col_name": "loan_prod_cd",
                "new_col_type": "VARCHAR(16)",
                "new_col_comment": "贷款产品代码",
                "ordinal_position": 3,
                "total_columns": 10,
                "db_current_col_name": "loan_prod_cd",
                "db_current_type": "character varying(16)",
                "db_current_comment": "",
            },
            {
                "row_index": 4,
                "schema_name": "dwd_bank",
                "table_name": "dwd_loan_contract",
                "old_col_name": "dkje",
                "old_col_type": "numeric(14,2)",
                "old_col_comment": "合同金额",
                "new_col_name": "loan_amt",
                "new_col_type": "NUMERIC(18,2)",
                "new_col_comment": "贷款合同金额",
                "ordinal_position": 4,
                "total_columns": 10,
                "db_current_col_name": "dkje",
                "db_current_type": "numeric(14,2)",
                "db_current_comment": "合同金额",
            },
            {
                "row_index": 5,
                "schema_name": "dwd_bank",
                "table_name": "dwd_loan_contract",
                "old_col_name": "zxlv",
                "old_col_type": "numeric(8,4)",
                "old_col_comment": "利率",
                "new_col_name": "exec_int_rate",
                "new_col_type": "NUMERIC(10,6)",
                "new_col_comment": "执行年利率",
                "ordinal_position": 5,
                "total_columns": 10,
                "db_current_col_name": "zxlv",
                "db_current_type": "numeric(8,4)",
                "db_current_comment": "利率",
            },
            {
                "row_index": 6,
                "schema_name": "dwd_bank",
                "table_name": "dwd_loan_contract",
                "old_col_name": "dkqx",
                "old_col_type": "smallint",
                "old_col_comment": "期限",
                "new_col_name": "loan_term_mth",
                "new_col_type": "INTEGER",
                "new_col_comment": "贷款期限月数",
                "ordinal_position": 6,
                "total_columns": 10,
                "db_current_col_name": "dkqx",
                "db_current_type": "smallint",
                "db_current_comment": "期限",
            },
            {
                "row_index": 7,
                "schema_name": "dwd_bank",
                "table_name": "dwd_loan_contract",
                "old_col_name": "sign_dt_str",
                "old_col_type": "character varying(10)",
                "old_col_comment": "签约日期文本",
                "new_col_name": "sign_dt",
                "new_col_type": "DATE",
                "new_col_comment": "合同签订日期",
                "ordinal_position": 7,
                "total_columns": 10,
                "db_current_col_name": "sign_dt_str",
                "db_current_type": "character varying(10)",
                "db_current_comment": "签约日期文本",
            },
            # dwd_acct_swap_demo（链式改名 + TABLE_REBUILD 演示）
            {
                "row_index": 8,
                "schema_name": "dwd_bank",
                "table_name": "dwd_acct_swap_demo",
                "old_col_name": "acct_code",
                "old_col_type": "character varying(24)",
                "old_col_comment": "新主账号候选",
                "new_col_name": "acct_no",
                "new_col_type": "VARCHAR(32)",
                "new_col_comment": "银行账号",
                "strategy": "TABLE_REBUILD",
                "is_distribute_key": 1,
                "table_orientation": "COLUMN",
                "ordinal_position": 1,
                "total_columns": 6,
                "db_current_col_name": "acct_code",
                "db_current_type": "character varying(24)",
                "db_current_comment": "新主账号候选",
            },
            {
                "row_index": 9,
                "schema_name": "dwd_bank",
                "table_name": "dwd_acct_swap_demo",
                "old_col_name": "acct_no",
                "old_col_type": "character varying(24)",
                "old_col_comment": "历史老账号",
                "new_col_name": "acct_legacy_no",
                "new_col_type": "VARCHAR(32)",
                "new_col_comment": "原旧系统账号",
                "strategy": "TABLE_REBUILD",
                "table_orientation": "COLUMN",
                "ordinal_position": 2,
                "total_columns": 6,
                "db_current_col_name": "acct_no",
                "db_current_type": "character varying(24)",
                "db_current_comment": "历史老账号",
            },
            {
                "row_index": 10,
                "schema_name": "dwd_bank",
                "table_name": "dwd_acct_swap_demo",
                "old_col_name": "acct_bal_amt",
                "old_col_type": "numeric(14,2)",
                "old_col_comment": "余额",
                "new_col_name": "acct_bal_amt",
                "new_col_type": "NUMERIC(18,2)",
                "new_col_comment": "账户活期余额",
                "strategy": "TABLE_REBUILD",
                "table_orientation": "COLUMN",
                "ordinal_position": 4,
                "total_columns": 6,
                "db_current_col_name": "acct_bal_amt",
                "db_current_type": "numeric(14,2)",
                "db_current_comment": "余额",
            },
            {
                "row_index": 11,
                "schema_name": "dwd_bank",
                "table_name": "dwd_acct_swap_demo",
                "old_col_name": "open_dt_txt",
                "old_col_type": "character varying(10)",
                "old_col_comment": "开户日期字符",
                "new_col_name": "open_dt",
                "new_col_type": "DATE",
                "new_col_comment": "开户日期",
                "strategy": "TABLE_REBUILD",
                "table_orientation": "COLUMN",
                "ordinal_position": 5,
                "total_columns": 6,
                "db_current_col_name": "open_dt_txt",
                "db_current_type": "character varying(10)",
                "db_current_comment": "开户日期字符",
            },
        ]
        ctx.store.replace_all_items(
            mig_items,
            "待贯标核心靶表 [dwd_loan_contract / dwd_acct_swap_demo]",
            preserve_history=False,
        )
        ctx.store.save_meta("migrator_connection_id", CONN_DWS_ID)
        ctx.store.save_meta("cross_source_connection_id", CONN_INC_ID)
        ctx.store.save_meta("cross_target_connection_id", CONN_DWS_ID)
        active_ws_id = ctx.workspace_hub.get_active_workspace_summary()["workspace_id"]
        ctx.workspace_hub.update_workspace(
            active_ws_id,
            name="启明银行 · 活库现场演练工作区",
            description="对接本地 Docker 华为DWS/星环Inceptor/MySQL 活库，含164条金融标准、5大业务暗雷与现场真改表靶表",
        )

        ws_bytes, _ = ctx.workspace_hub.export_workspace_bytes(include_metadata_cache=True)
        out_path = os.path.join(output_dir, "qiming_live_sandbox.dataog.db")
        with open(out_path, "wb") as fh:
            fh.write(ws_bytes)

        return out_path, {
            "standards": len(build_standard_items()),
            "dws_bindings": len(dws_bindings),
            "inceptor_bindings": len(inc_bindings),
            "cross_pairs": len(cross_pair_defs),
            "migration_items": len(mig_items),
        }


# =============================================================================
# 大体量 150 表信创中台工作区生成器 (`qiming_enterprise_150tables.dataog.db`)
# =============================================================================

_ENTERPRISE_DOMAIN_TEMPLATES: Tuple[Tuple[str, str, str, str], ...] = (
    ("cust", "客户域", "dwd_bank", "QM_CUST_"),
    ("acct", "账户域", "dwd_bank", "QM_ACCT_"),
    ("loan", "信贷域", "dwd_bank", "QM_LOAN_"),
    ("txn", "交易域", "dwd_bank", "QM_TXN_"),
    ("coll", "担保域", "dwd_bank", "QM_COLL_"),
    ("risk", "风控域", "dws_bank", "QM_RISK_"),
    ("fin", "财务域", "dws_bank", "QM_FIN_"),
    ("reg", "监管报送域", "ads_reg", "QM_RISK_"),
)


def build_enterprise_150_workspace(output_dir: str) -> Tuple[str, Dict[str, Any]]:
    """生成 156 星环表 × 162 DWS 表（5,890 双端字段、5,222 条跨端绑标）的超大规模压测与离线兜底 `.dataog.db`."""
    os.makedirs(output_dir, exist_ok=True)
    all_stds = build_standard_items()
    by_domain: Dict[str, List[Dict[str, Any]]] = {}
    for st in all_stds:
        dom = st["std_code"].split("_")[1]
        by_domain.setdefault(dom, []).append(st)
    org_stds = by_domain.get("ORG", [])

    # 生成 140 张同名核心业务表（前 120 张保存映射，后 20 张作为推荐同名未保存表）+
    # 16 张星环独有表 + 22 张 DWS 独有表 = 156 星环表 + 162 DWS 表
    dws_specs: List[Tuple[str, str, str, str, List[Tuple[str, str, str]]]] = []
    inc_specs: List[Tuple[str, str, str, str, List[Tuple[str, str, str]]]] = []
    dws_bindings: List[Tuple[str, str, str, str]] = []
    inc_bindings: List[Tuple[str, str, str, str]] = []
    cross_items: List[Dict[str, Any]] = []

    def _pick_stds_for_table(dom_prefix: str, idx: int) -> List[Dict[str, Any]]:
        dom_key = dom_prefix.split("_")[1]
        pool = by_domain.get(dom_key, all_stds)
        chosen: List[Dict[str, Any]] = []
        seen_c: set[str] = set()
        for k in range(14):
            st = pool[(idx * 3 + k) % len(pool)]
            if st["std_code"] not in seen_c:
                seen_c.add(st["std_code"])
                chosen.append(st)
        for k in range(6):
            st = all_stds[(idx * 11 + k * 7) % len(all_stds)]
            if st["std_code"] not in seen_c and len(chosen) < 16:
                seen_c.add(st["std_code"])
                chosen.append(st)
        for o in org_stds[:4]:
            if o["std_code"] not in seen_c:
                seen_c.add(o["std_code"])
                chosen.append(o)
        return chosen

    for t_idx in range(1, 141):
        dom_code, dom_cn, dws_sch, std_pref = _ENTERPRISE_DOMAIN_TEMPLATES[(t_idx - 1) % len(_ENTERPRISE_DOMAIN_TEMPLATES)]
        layer = "ads" if dws_sch == "ads_reg" else ("dws" if dws_sch == "dws_bank" else "dwd")
        tbl_name = f"{layer}_{dom_code}_biz_{t_idx:03d}"
        tbl_comm = f"启明银行{dom_cn}业务专题表#{t_idx:03d}"
        stds = _pick_stds_for_table(std_pref, t_idx)

        dws_cols: List[Tuple[str, str, str]] = []
        inc_cols: List[Tuple[str, str, str]] = []

        for c_idx, st in enumerate(stds, start=1):
            en_low = st["en_abbr"].lower()
            dws_t = st["type_dws"]
            inc_t = st["type_argodb"]
            dws_name = en_low
            dws_comm = st["cn_name"]
            inc_name = en_low
            inc_comm = st["cn_name"]

            # 刻意注入丰富、真实比例的落标差异与跨环境包络/转换冲突
            mode_flag = (t_idx + c_idx) % 10
            if mode_flag == 1 and "VARCHAR(" in dws_t:
                # 长度不足（星环 VARCHAR(200) / STRING -> DWS VARCHAR(60)）
                dws_t = "VARCHAR(40)"
                inc_t = "VARCHAR(200)"
                dws_name = f"old_{en_low[:18]}"
            elif mode_flag == 2 and "NUMERIC(" in dws_t:
                # 类型转换与精度差异（星环 DOUBLE -> DWS NUMERIC(14,2)）
                dws_t = "NUMERIC(14,2)"
                inc_t = "DOUBLE"
            elif mode_flag == 3:
                # 仅缺注释
                dws_comm = ""
            elif mode_flag == 4 and c_idx >= 3:
                # 星环端多出无界 STRING 与老精度
                inc_t = "STRING"

            dws_cols.append((dws_name, dws_t, dws_comm))
            inc_cols.append((inc_name, inc_t, inc_comm))

            # 绑标策略：约 8% 未绑标；约 10% 双侧 SPLIT_DIFF；约 8% 单侧 SPLIT_PARTIAL
            col_s_code = ""
            col_t_code = ""
            if (t_idx + c_idx) % 13 == 0:
                # 仅星环绑，DWS 未绑 -> SPLIT_PARTIAL
                col_s_code = st["std_code"]
                inc_bindings.append((INCEPTOR_DB, tbl_name, inc_name, col_s_code))
            elif (t_idx + c_idx) % 11 == 0:
                # 双侧绑不同标准 -> SPLIT_DIFF
                alt_st = all_stds[(t_idx * 7 + c_idx) % len(all_stds)]
                col_t_code = st["std_code"]
                col_s_code = alt_st["std_code"] if alt_st["std_code"] != col_t_code else all_stds[(t_idx * 7 + c_idx + 1) % len(all_stds)]["std_code"]
                dws_bindings.append((dws_sch, tbl_name, dws_name, col_t_code))
                inc_bindings.append((INCEPTOR_DB, tbl_name, inc_name, col_s_code))
            elif (t_idx + c_idx) % 17 != 0:
                col_t_code = st["std_code"]
                col_s_code = st["std_code"]
                dws_bindings.append((dws_sch, tbl_name, dws_name, col_t_code))
                inc_bindings.append((INCEPTOR_DB, tbl_name, inc_name, col_s_code))

            if t_idx <= 120:
                cross_items.append(
                    {
                        "row_index": c_idx,
                        "std_schema_name": dws_sch,
                        "std_table_name": tbl_name,
                        "std_table_comment": tbl_comm,
                        "source_schema": INCEPTOR_DB,
                        "source_table": tbl_name,
                        "target_schema": dws_sch,
                        "target_table": tbl_name,
                        "orig_col_name": inc_name,
                        "orig_col_type": inc_t,
                        "orig_col_comment": inc_comm,
                        "std_col_name": en_low,
                        "std_col_type": st["type_dws"],
                        "std_col_comment": st["cn_name"],
                        "bound_source_col": inc_name,
                        "bound_target_col": dws_name,
                        "source_std_code": col_s_code,
                        "target_std_code": col_t_code,
                    }
                )

        dws_specs.append((dws_sch, tbl_name, "TABLE", tbl_comm, dws_cols))
        inc_specs.append((INCEPTOR_DB, tbl_name, "TABLE", f"星环{tbl_comm}", inc_cols))

    # 16 张星环独有表
    for j in range(1, 17):
        t_name = f"his_inceptor_only_{j:03d}"
        stds = _pick_stds_for_table("QM_TXN_", j + 50)[:8]
        inc_specs.append(
            (INCEPTOR_DB, t_name, "TABLE", f"星环历史独有归档表#{j:03d}", [(s["en_abbr"].lower(), "STRING", s["cn_name"]) for s in stds])
        )

    # 22 张 DWS 独有表
    for k in range(1, 23):
        sch = "ads_reg" if k > 12 else "dws_bank"
        t_name = f"dws_native_only_{k:03d}"
        stds = _pick_stds_for_table("QM_RISK_", k + 70)[:8]
        cols = [(s["en_abbr"].lower(), s["type_dws"], s["cn_name"]) for s in stds]
        dws_specs.append((sch, t_name, "TABLE", f"DWS信创独有集市表#{k:03d}", cols))
        for cn, _ct, _cc in cols:
            st = next(s for s in stds if s["en_abbr"].lower() == cn)
            dws_bindings.append((sch, t_name, cn, st["std_code"]))

    with tempfile.TemporaryDirectory(prefix="dataog_demo_150_ws_") as tmp_priv:
        ctx = PlatformContext(private_root=tmp_priv, legacy_data_dir="")
        for prof in build_demo_connection_profiles(include_secrets=False):
            ctx.store.upsert_connection_profile(prof)

        dict_xlsx = os.path.join(output_dir, "qiming_bank_data_standards_v2026.xlsx")
        if not os.path.isfile(dict_xlsx):
            export_standard_dictionary_xlsx(dict_xlsx)
        with open(dict_xlsx, "rb") as fh:
            ctx.standard_catalog.import_dictionary_excel(
                "qiming_bank_data_standards_v2026.xlsx",
                fh.read(),
                mode="replace",
            )

        _persist_table_specs(ctx, CONN_DWS_ID, dws_specs)
        _persist_table_specs(ctx, CONN_INC_ID, inc_specs)
        _persist_table_specs(ctx, CONN_MY_ID, _offline_live_table_specs()[CONN_MY_ID])

        _evaluate_and_save_bindings(ctx, CONN_DWS_ID, "DWS", "type_dws", dws_bindings)
        _evaluate_and_save_bindings(ctx, CONN_INC_ID, "INCEPTOR", "type_argodb", inc_bindings)
        ctx.store.replace_cross_standards(cross_items, "启明银行全行150表跨环境映射基线")

        ctx.store.save_meta("migrator_connection_id", CONN_DWS_ID)
        ctx.store.save_meta("cross_source_connection_id", CONN_INC_ID)
        ctx.store.save_meta("cross_target_connection_id", CONN_DWS_ID)
        active_ws_id = ctx.workspace_hub.get_active_workspace_summary()["workspace_id"]
        ctx.workspace_hub.update_workspace(
            active_ws_id,
            name="启明银行 · 全行150表信创中台工作区",
            description="156 张星环表 × 162 张华为 DWS 表（5,890 双端字段、5,222 条跨端绑标）百表级压测与离线报告演示工作区",
        )

        ws_bytes, _ = ctx.workspace_hub.export_workspace_bytes(include_metadata_cache=True)
        out_path = os.path.join(output_dir, "qiming_enterprise_150tables.dataog.db")
        with open(out_path, "wb") as fh:
            fh.write(ws_bytes)

        return out_path, {
            "inceptor_tables": len(inc_specs),
            "dws_tables": len(dws_specs),
            "dws_bindings": len(dws_bindings),
            "inceptor_bindings": len(inc_bindings),
            "cross_pairs": 120,
            "recommended_unpaired": 20,
        }


def build_all_workspaces(output_dir: str, *, live_probe: bool = False) -> Dict[str, Any]:
    """一键生成 46 列标准字典 Excel、绑标演示模板与两份 `.dataog.db` 工作区."""
    live_path, live_stats = build_live_sandbox_workspace(output_dir, live_probe=live_probe)
    ent_path, ent_stats = build_enterprise_150_workspace(output_dir)
    return {
        "output_dir": os.path.abspath(output_dir),
        "live_workspace": live_path,
        "live_stats": live_stats,
        "enterprise_workspace": ent_path,
        "enterprise_stats": ent_stats,
    }
