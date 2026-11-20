"""启明商业银行 (Qiming Commercial Bank) 金融数据标准字典资产生成器.

参考银保监 EAST 5.0 与人民银行金融基础数据标准体系，原创构建 164 条金融行业数据标准，
覆盖 8 大主题域（客户/账户/信贷/交易/担保/风险/财务/机构），完整支持 Data.O.G 46 列多库物理格式导入，
同时生成供现场演示「批量对照导入绑标」使用的预填 Excel 模板。
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any, Dict, List, Optional, Tuple

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from core.platform.standard_catalog import BOHAI_DICT_HEADERS


def _type_bundle(
    kind: str,
    length: int = 0,
    scale: int = 0,
    *,
    argodb_override: str = "",
    dws_override: str = "",
) -> Dict[str, str]:
    """根据标准数据类别自动推导 DWS / ArgoDB(星环) / MySQL / Oracle / 达梦等 10 种物理类型."""
    k = kind.upper()
    if k == "VARCHAR":
        n = max(1, int(length or 64))
        dws = dws_override or f"VARCHAR({n})"
        argo = argodb_override or (f"VARCHAR({n})" if n <= 64 else "STRING")
        return {
            "max_length": str(n),
            "min_scale": "",
            "data_format": f"c..{n}",
            "type_dws": dws,
            "type_dw_mart": f"VARCHAR2({n})",
            "type_argodb": argo,
            "type_mysql": f"VARCHAR({n})",
            "type_oracle": f"VARCHAR2({n})",
            "type_dameng": f"VARCHAR2({n})",
            "type_goldendb": f"VARCHAR({n})",
            "type_ob_oracle": f"VARCHAR2({n})",
            "type_ob_mysql": f"VARCHAR({n})",
            "type_oceanbase": f"VARCHAR({n})",
        }
    if k == "CHAR":
        n = max(1, int(length or 1))
        dws = dws_override or f"CHAR({n})"
        argo = argodb_override or f"CHAR({n})"
        return {
            "max_length": str(n),
            "min_scale": "",
            "data_format": f"c{n}",
            "type_dws": dws,
            "type_dw_mart": f"CHAR({n})",
            "type_argodb": argo,
            "type_mysql": f"CHAR({n})",
            "type_oracle": f"CHAR({n})",
            "type_dameng": f"CHAR({n})",
            "type_goldendb": f"CHAR({n})",
            "type_ob_oracle": f"CHAR({n})",
            "type_ob_mysql": f"CHAR({n})",
            "type_oceanbase": f"CHAR({n})",
        }
    if k == "DECIMAL":
        p = max(2, int(length or 18))
        s = max(0, int(scale))
        dws = dws_override or f"NUMERIC({p},{s})"
        argo = argodb_override or f"DECIMAL({p},{s})"
        return {
            "max_length": str(p),
            "min_scale": str(s),
            "data_format": f"n..{p},{s}",
            "type_dws": dws,
            "type_dw_mart": f"DECIMAL({p},{s})",
            "type_argodb": argo,
            "type_mysql": f"DECIMAL({p},{s})",
            "type_oracle": f"NUMBER({p},{s})",
            "type_dameng": f"DECIMAL({p},{s})",
            "type_goldendb": f"DECIMAL({p},{s})",
            "type_ob_oracle": f"NUMBER({p},{s})",
            "type_ob_mysql": f"DECIMAL({p},{s})",
            "type_oceanbase": f"DECIMAL({p},{s})",
        }
    if k == "INT":
        return {
            "max_length": "10",
            "min_scale": "0",
            "data_format": "n..10",
            "type_dws": dws_override or "INTEGER",
            "type_dw_mart": "INTEGER",
            "type_argodb": argodb_override or "INT",
            "type_mysql": "INT",
            "type_oracle": "NUMBER(10,0)",
            "type_dameng": "INTEGER",
            "type_goldendb": "INT",
            "type_ob_oracle": "NUMBER(10,0)",
            "type_ob_mysql": "INT",
            "type_oceanbase": "INT",
        }
    if k == "BIGINT":
        return {
            "max_length": "19",
            "min_scale": "0",
            "data_format": "n..19",
            "type_dws": dws_override or "BIGINT",
            "type_dw_mart": "BIGINT",
            "type_argodb": argodb_override or "BIGINT",
            "type_mysql": "BIGINT",
            "type_oracle": "NUMBER(19,0)",
            "type_dameng": "BIGINT",
            "type_goldendb": "BIGINT",
            "type_ob_oracle": "NUMBER(19,0)",
            "type_ob_mysql": "BIGINT",
            "type_oceanbase": "BIGINT",
        }
    if k == "DATE":
        return {
            "max_length": "10",
            "min_scale": "",
            "data_format": "YYYY-MM-DD",
            "type_dws": dws_override or "DATE",
            "type_dw_mart": "DATE",
            "type_argodb": argodb_override or "DATE",
            "type_mysql": "DATE",
            "type_oracle": "DATE",
            "type_dameng": "DATE",
            "type_goldendb": "DATE",
            "type_ob_oracle": "DATE",
            "type_ob_mysql": "DATE",
            "type_oceanbase": "DATE",
        }
    if k == "TIMESTAMP":
        return {
            "max_length": "19",
            "min_scale": "",
            "data_format": "YYYY-MM-DD HH:MM:SS",
            "type_dws": dws_override or "TIMESTAMP",
            "type_dw_mart": "TIMESTAMP",
            "type_argodb": argodb_override or "TIMESTAMP",
            "type_mysql": "DATETIME",
            "type_oracle": "TIMESTAMP",
            "type_dameng": "TIMESTAMP",
            "type_goldendb": "DATETIME",
            "type_ob_oracle": "TIMESTAMP",
            "type_ob_mysql": "DATETIME",
            "type_oceanbase": "DATETIME",
        }
    raise ValueError(f"Unsupported kind: {kind}")


# (domain_cn, cat_l1, source_sys, ref_std_no, ref_std_name)
_DOMAIN_META: Dict[str, Tuple[str, str, str, str, str]] = {
    "CUST": ("客户主体域", "客户域", "ECIF客户信息中心", "JR/T 0068-2020", "金融业客户基础信息标准"),
    "ACCT": ("存款账户域", "账户域", "核心存款核算系统", "EAST 5.0", "银行业监管标准化数据规范(账户)"),
    "LOAN": ("信贷业务域", "信贷域", "信贷综合管理系统", "EAST 5.0", "银行业监管标准化数据规范(信贷)"),
    "TXN": ("支付交易域", "交易域", "核心支付清算平台", "JR/T 0197-2020", "金融数据安全与交易流水规范"),
    "COLL": ("担保押品域", "担保域", "押品估值与风控平台", "1104监管", "银行业信用风险缓释工具规范"),
    "RISK": ("风险监管域", "风控域", "全面风险管理平台", "1104监管", "商业银行资本与风险监管指标"),
    "FIN": ("财务核算域", "财务域", "总账财务核算系统", "CAS-FIN", "金融企业会计核算数据标准"),
    "ORG": ("公共机构域", "公共域", "全行公共主数据平台", "GB/T 2260", "全行组织机构与数仓治理规范"),
}

# (std_code, domain, cat_l2, cn_name, en_abbr, en_name, data_category, reg_category, sec_lvl,
#  kind, length, scale, biz_def, ref_code_detail)
_RAW_STANDARDS: List[Tuple[Any, ...]] = [
    # ================= 1. CUST 客户域 (25条) =================
    ("QM_CUST_001", "CUST", "客户标识", "客户统一编号", "CUST_ID", "Customer Unified ID", "编码类", "EAST 5.0", "L3", "VARCHAR", 32, 0, "全行ECIF统一分配的对公/零售客户唯一识别号", ""),
    ("QM_CUST_002", "CUST", "客户基础", "客户中文全称", "CUST_NAME", "Customer Full Name", "名称类", "EAST 5.0", "L3", "VARCHAR", 120, 0, "客户在工商或公安机关登记的法定全称", ""),
    ("QM_CUST_003", "CUST", "客户基础", "客户类型代码", "CUST_TYPE_CD", "Customer Type Code", "代码类", "EAST 5.0", "L2", "CHAR", 2, 0, "区分对公、零售、同业客户的分类代码", "01-对公 02-零售 03-同业"),
    ("QM_CUST_004", "CUST", "证件信息", "证件类型代码", "CERT_TYPE_CD", "Certificate Type Code", "代码类", "EAST 5.0", "L2", "VARCHAR", 8, 0, "客户身份或机构登记有效证件分类代码", "101-身份证 201-统一社会信用代码 102-护照"),
    ("QM_CUST_005", "CUST", "证件信息", "证件号码", "CERT_NO", "Certificate Number", "编码类", "EAST 5.0", "L4", "VARCHAR", 32, 0, "身份证号、统一社会信用代码或法定登记证件号", ""),
    ("QM_CUST_006", "CUST", "证件信息", "统一社会信用代码", "USCC_CD", "Unified Social Credit Code", "编码类", "EAST 5.0", "L3", "VARCHAR", 18, 0, "18位法人和其他组织统一社会信用代码", ""),
    ("QM_CUST_007", "CUST", "证件信息", "证件到期日期", "CERT_EXPIRE_DT", "Certificate Expire Date", "日期类", "反洗钱", "L2", "DATE", 10, 0, "客户主要身份证明文件有效期截止日期", ""),
    ("QM_CUST_008", "CUST", "客户属性", "国别地区代码", "COUNTRY_CD", "Country Region Code", "代码类", "EAST 5.0", "L1", "CHAR", 3, 0, "ISO 3166-1三位字母国别代码", "CHN-中国 HKG-中国香港 USA-美国"),
    ("QM_CUST_009", "CUST", "客户属性", "国民经济行业代码", "INDUSTRY_CD", "Industry Category Code", "代码类", "1104监管", "L2", "VARCHAR", 10, 0, "GB/T 4754国民经济行业门类及大类代码", "C39-电子制造 F51-批发业 K70-房地产业"),
    ("QM_CUST_010", "CUST", "客户属性", "企业规模代码", "ENT_SCALE_CD", "Enterprise Scale Code", "代码类", "1104监管", "L2", "CHAR", 2, 0, "工信部四部委划型标准企业规模代码", "01-大型 02-中型 03-小型 04-微型"),
    ("QM_CUST_011", "CUST", "客户属性", "客户风险评级代码", "CUST_RISK_LVL_CD", "Customer Risk Level Code", "代码类", "反洗钱", "L3", "VARCHAR", 4, 0, "反洗钱及信用综合评估客户风险等级", "R1-低 R2-中低 R3-中 R4-中高 R5-高"),
    ("QM_CUST_012", "CUST", "联系信息", "手机号码", "MOBILE_NO", "Mobile Phone Number", "编码类", "反洗钱", "L4", "VARCHAR", 20, 0, "客户预留用于身份核验与短信通知的手机号", ""),
    ("QM_CUST_013", "CUST", "联系信息", "电子邮箱", "EMAIL_ADDR", "Email Address", "文本类", "业务数据", "L3", "VARCHAR", 100, 0, "客户有效电子邮箱地址", ""),
    ("QM_CUST_014", "CUST", "联系信息", "注册或联系地址", "REG_ADDR", "Registered Address", "文本类", "EAST 5.0", "L3", "VARCHAR", 200, 0, "企业工商注册地址或个人常住通讯地址", ""),
    ("QM_CUST_015", "CUST", "客户分层", "客户价值分层代码", "VIP_TIER_CD", "Customer VIP Tier Code", "代码类", "业务数据", "L2", "VARCHAR", 8, 0, "按AUM与综合贡献评定的客户分层等级", "MASS-大众 GOLD-黄金 PLAT-白金 PRIV-私行"),
    ("QM_CUST_016", "CUST", "对公信息", "企业成立日期", "ESTAB_DT", "Establishment Date", "日期类", "EAST 5.0", "L2", "DATE", 10, 0, "企业法人营业执照记载的成立日期", ""),
    ("QM_CUST_017", "CUST", "对公信息", "注册资本金额", "REG_CAPITAL_AMT", "Registered Capital Amount", "金额类", "EAST 5.0", "L2", "DECIMAL", 18, 2, "企业在工商登记机关登记的注册资本折人民币金额", ""),
    ("QM_CUST_018", "CUST", "对公信息", "法定代表人姓名", "LEGAL_REP_NAME", "Legal Representative Name", "名称类", "EAST 5.0", "L3", "VARCHAR", 64, 0, "对公客户法定代表人或主要负责人姓名", ""),
    ("QM_CUST_019", "CUST", "客户属性", "客户状态代码", "CUST_STATUS_CD", "Customer Status Code", "代码类", "EAST 5.0", "L2", "CHAR", 2, 0, "标识客户在行内的存续活跃状态", "01-正常 02-休眠 03-冻结 09-注销"),
    ("QM_CUST_020", "CUST", "对公信息", "集团客户标识", "GROUP_CUST_IND", "Group Customer Indicator", "指示器类", "客户风险", "L2", "CHAR", 1, 0, "标识客户是否隶属单一集团客户授信体系", "0-否 1-是"),
    ("QM_CUST_021", "CUST", "对公信息", "所属集团编号", "GROUP_ID", "Corporate Group ID", "编码类", "客户风险", "L2", "VARCHAR", 32, 0, "对公集团客户统一归口编号", ""),
    ("QM_CUST_022", "CUST", "零售信息", "性别代码", "GENDER_CD", "Gender Code", "代码类", "EAST 5.0", "L2", "CHAR", 1, 0, "自然人性别代码", "1-男 2-女 9-未知"),
    ("QM_CUST_023", "CUST", "零售信息", "出生日期", "BIRTH_DT", "Birth Date", "日期类", "EAST 5.0", "L3", "DATE", 10, 0, "自然人身份证载明的出生日期", ""),
    ("QM_CUST_024", "CUST", "零售信息", "职业分类代码", "OCCUPATION_CD", "Occupation Code", "代码类", "反洗钱", "L2", "VARCHAR", 8, 0, "零售客户职业类别代码", ""),
    ("QM_CUST_025", "CUST", "客户关系", "开户建档日期", "CUST_OPEN_DT", "Customer Onboard Date", "日期类", "EAST 5.0", "L2", "DATE", 10, 0, "客户首次在启明银行建立主档的日期", ""),

    # ================= 2. ACCT 账户域 (20条) =================
    ("QM_ACCT_001", "ACCT", "账户标识", "银行账号", "ACCT_NO", "Bank Account Number", "编码类", "EAST 5.0", "L4", "VARCHAR", 32, 0, "客户在核心系统开立的结算或内部核算账号", ""),
    ("QM_ACCT_002", "ACCT", "账户标识", "原旧系统账号", "ACCT_LEGACY_NO", "Legacy Account Number", "编码类", "业务数据", "L3", "VARCHAR", 32, 0, "历史核心迁移保留的原旧账号对照字段", ""),
    ("QM_ACCT_003", "ACCT", "账户基础", "账户户名", "ACCT_NAME", "Account Name", "名称类", "EAST 5.0", "L3", "VARCHAR", 120, 0, "银行账户对外展示和清算使用的法定户名", ""),
    ("QM_ACCT_004", "ACCT", "账户属性", "账户类型代码", "ACCT_TYPE_CD", "Account Type Code", "代码类", "EAST 5.0", "L2", "VARCHAR", 8, 0, "活期、定期、基本户、一般户、专用户等分类", "BASIC-基本户 GEN-一般户 SPEC-专用户 SAV-个人活期"),
    ("QM_ACCT_005", "ACCT", "账户属性", "账户状态代码", "ACCT_STATUS_CD", "Account Status Code", "代码类", "EAST 5.0", "L2", "CHAR", 2, 0, "标识当前银行账户的核算及管控状态", "01-正常 02-久悬 03-冻结 04-止付 09-销户"),
    ("QM_ACCT_006", "ACCT", "账户基础", "开户日期", "OPEN_DT", "Account Open Date", "日期类", "EAST 5.0", "L2", "DATE", 10, 0, "账户正式设立并启用核算的日期", ""),
    ("QM_ACCT_007", "ACCT", "账户基础", "销户日期", "CLOSE_DT", "Account Close Date", "日期类", "EAST 5.0", "L2", "DATE", 10, 0, "账户结清并注销的日期", ""),
    ("QM_ACCT_008", "ACCT", "账户余额", "账户活期余额", "ACCT_BAL_AMT", "Account Current Balance", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "截至统计日账户账面可用及不可用总余额", ""),
    ("QM_ACCT_009", "ACCT", "账户余额", "账户可用余额", "AVAIL_BAL_AMT", "Available Balance Amount", "金额类", "业务数据", "L3", "DECIMAL", 18, 2, "扣除司法冻结和质押止付后实际可支取金额", ""),
    ("QM_ACCT_010", "ACCT", "账户余额", "冻结金额", "FROZEN_AMT", "Frozen Amount", "金额类", "反洗钱", "L3", "DECIMAL", 18, 2, "处于司法或业务止付冻结状态的金额", ""),
    ("QM_ACCT_011", "ACCT", "账户余额", "日均存款余额", "AVG_DAILY_BAL_AMT", "Average Daily Balance", "金额类", "业务数据", "L3", "DECIMAL", 18, 2, "统计周期内账户每日日终余额的算术平均值", ""),
    ("QM_ACCT_012", "ACCT", "账户属性", "币种代码", "CURRENCY_CD", "Currency Code", "代码类", "EAST 5.0", "L1", "CHAR", 3, 0, "ISO 4217三位标准币种代码", "CNY-人民币 USD-美元 EUR-欧元 HKD-港元"),
    ("QM_ACCT_013", "ACCT", "账户属性", "存款利率", "DEP_INT_RATE", "Deposit Interest Rate", "比率类", "EAST 5.0", "L2", "DECIMAL", 10, 6, "存款账户适用的执行年化利率(小数形式)", ""),
    ("QM_ACCT_014", "ACCT", "账户属性", "存款期限月数", "DEP_TERM_MTH", "Deposit Term Months", "数值类", "EAST 5.0", "L2", "INT", 10, 0, "定期存款约定存期(月)", ""),
    ("QM_ACCT_015", "ACCT", "银行卡", "银行卡号", "CARD_NO", "Bank Card Number", "编码类", "EAST 5.0", "L4", "VARCHAR", 24, 0, "脱敏或加密存储的借记卡/信用卡主卡号", ""),
    ("QM_ACCT_016", "ACCT", "银行卡", "信用卡授信额度", "CARD_LIMIT_AMT", "Credit Card Limit Amount", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "发卡行核定的信用卡循环信用总额度", ""),
    ("QM_ACCT_017", "ACCT", "银行卡", "信用卡已用额度", "CARD_USED_AMT", "Credit Card Used Amount", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "信用卡当前未出账与已出账累计已占额度", ""),
    ("QM_ACCT_018", "ACCT", "理财持仓", "理财产品代码", "WEALTH_PROD_CD", "Wealth Product Code", "编码类", "EAST 5.0", "L2", "VARCHAR", 24, 0, "资管与理财登记系统统一编码", ""),
    ("QM_ACCT_019", "ACCT", "理财持仓", "理财持有份额", "WEALTH_SHARE_QTY", "Wealth Holding Shares", "数值类", "EAST 5.0", "L3", "DECIMAL", 18, 4, "客户当前持有的理财产品确认份额", ""),
    ("QM_ACCT_020", "ACCT", "理财持仓", "理财持有市值", "WEALTH_MKT_VAL_AMT", "Wealth Market Value", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "按最新净值计算的理财产品持仓市值", ""),

    # ================= 3. LOAN 信贷业务域 (30条) =================
    ("QM_LOAN_001", "LOAN", "信贷标识", "贷款合同编号", "LOAN_CONTRACT_NO", "Loan Contract Number", "编码类", "EAST 5.0", "L3", "VARCHAR", 36, 0, "借贷双方签署的借款主合同唯一编号", ""),
    ("QM_LOAN_002", "LOAN", "信贷标识", "贷款借据编号", "LOAN_RECEIPT_NO", "Loan Receipt Number", "编码类", "EAST 5.0", "L3", "VARCHAR", 36, 0, "单笔放款入账生成的贷款借据(分户)唯一编号", ""),
    ("QM_LOAN_003", "LOAN", "授信额度", "授信额度编号", "CREDIT_LIMIT_ID", "Credit Limit Identifier", "编码类", "客户风险", "L3", "VARCHAR", 32, 0, "授信审批系统核定的综合授信协议编号", ""),
    ("QM_LOAN_004", "LOAN", "信贷产品", "贷款产品代码", "LOAN_PROD_CD", "Loan Product Code", "代码类", "EAST 5.0", "L2", "VARCHAR", 16, 0, "全行信贷产品目录唯一代码", "CORP_WC-流动资金 CORP_FA-固定资产 RET_MTG-按揭 RET_CON-消费贷"),
    ("QM_LOAN_005", "LOAN", "信贷产品", "贷款产品名称", "LOAN_PROD_NAME", "Loan Product Name", "名称类", "业务数据", "L2", "VARCHAR", 80, 0, "贷款业务品种中文全称", ""),
    ("QM_LOAN_006", "LOAN", "信贷金额", "贷款合同金额", "LOAN_AMT", "Loan Contract Amount", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "借款合同约定的本金总额", ""),
    ("QM_LOAN_007", "LOAN", "信贷金额", "放款发放金额", "DISBURSE_AMT", "Loan Disbursement Amount", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "单笔借据实际发放给借款人的本金金额", ""),
    ("QM_LOAN_008", "LOAN", "信贷金额", "贷款本金余额", "LOAN_BAL_AMT", "Loan Principal Balance", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "截至统计日尚未偿还的贷款本金余额", ""),
    ("QM_LOAN_009", "LOAN", "授信额度", "核定授信总额", "APPROVED_LIMIT_AMT", "Approved Credit Limit", "金额类", "客户风险", "L3", "DECIMAL", 18, 2, "审批通过的对公客户最高综合授信额度", ""),
    ("QM_LOAN_010", "LOAN", "授信额度", "已用授信额度", "USED_LIMIT_AMT", "Used Credit Limit", "金额类", "客户风险", "L3", "DECIMAL", 18, 2, "当前各项表内外信贷业务累计占用的授信额度", ""),
    ("QM_LOAN_011", "LOAN", "利率定价", "执行年利率", "EXEC_INT_RATE", "Execution Interest Rate", "比率类", "EAST 5.0", "L2", "DECIMAL", 10, 6, "贷款当前实际执行的年化利率(小数形式，如0.043500)", ""),
    ("QM_LOAN_012", "LOAN", "利率定价", "LPR基准利率", "LPR_BASE_RATE", "LPR Benchmark Rate", "比率类", "EAST 5.0", "L1", "DECIMAL", 10, 6, "放款定价锚定的贷款市场报价利率(LPR)", ""),
    ("QM_LOAN_013", "LOAN", "利率定价", "利率加减点BP", "RATE_SPREAD_BP", "Interest Rate Spread BP", "数值类", "EAST 5.0", "L2", "DECIMAL", 10, 2, "相对LPR基准利率的浮动基点数(BP)", ""),
    ("QM_LOAN_014", "LOAN", "信贷期限", "贷款期限月数", "LOAN_TERM_MTH", "Loan Term Months", "数值类", "EAST 5.0", "L2", "INT", 10, 0, "合同约定的借款期限总月数", ""),
    ("QM_LOAN_015", "LOAN", "信贷日期", "合同签订日期", "SIGN_DT", "Contract Sign Date", "日期类", "EAST 5.0", "L2", "DATE", 10, 0, "借款主合同正式签署生效的日期", ""),
    ("QM_LOAN_016", "LOAN", "信贷日期", "放款起息日期", "VALUE_DT", "Loan Value Date", "日期类", "EAST 5.0", "L2", "DATE", 10, 0, "贷款借据实际发放并开始计息的日期", ""),
    ("QM_LOAN_017", "LOAN", "信贷日期", "贷款到期日期", "MATURITY_DT", "Loan Maturity Date", "日期类", "EAST 5.0", "L2", "DATE", 10, 0, "合同/借据约定的最终本息清偿到期日", ""),
    ("QM_LOAN_018", "LOAN", "资产质量", "五级分类代码", "FIVE_CLASS_CD", "Five-Tier Classification Code", "代码类", "1104监管", "L3", "CHAR", 2, 0, "银保监贷款风险五级分类结果代码", "10-正常 20-关注 30-次级 40-可疑 50-损失"),
    ("QM_LOAN_019", "LOAN", "资产质量", "逾期天数", "OVERDUE_DAYS", "Overdue Days", "数值类", "1104监管", "L3", "INT", 10, 0, "自最早一期未清偿本金或利息到期日起算的逾期自然天数", ""),
    ("QM_LOAN_020", "LOAN", "资产质量", "逾期本金金额", "OVERDUE_PRIN_AMT", "Overdue Principal Amount", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "已超过约定还款日仍未偿还的本金金额", ""),
    ("QM_LOAN_021", "LOAN", "资产质量", "表内欠息金额", "ON_BS_INT_AMT", "On-Balance-Sheet Interest", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "逾期90天以内计入表内应收利息的欠息金额", ""),
    ("QM_LOAN_022", "LOAN", "资产质量", "表外欠息金额", "OFF_BS_INT_AMT", "Off-Balance-Sheet Interest", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "逾期超90天转入表外核算的应收未收利息", ""),
    ("QM_LOAN_023", "LOAN", "还款管理", "还款方式代码", "REPAY_METHOD_CD", "Repayment Method Code", "代码类", "EAST 5.0", "L2", "VARCHAR", 8, 0, "借款合同约定的本息偿还方式", "EPI-等额本息 EPP-等额本金 IO-先息后本 BULLET-到期还本"),
    ("QM_LOAN_024", "LOAN", "还款管理", "还款期次", "REPAY_TERM_NO", "Repayment Installment No", "数值类", "业务数据", "L2", "INT", 10, 0, "贷款分期还款计划顺序期号", ""),
    ("QM_LOAN_025", "LOAN", "还款管理", "应还日期", "SCHED_REPAY_DT", "Scheduled Repayment Date", "日期类", "业务数据", "L2", "DATE", 10, 0, "还款计划约定的本期本息扣款日期", ""),
    ("QM_LOAN_026", "LOAN", "还款管理", "本期应还本金", "SCHED_PRIN_AMT", "Scheduled Principal Amount", "金额类", "业务数据", "L3", "DECIMAL", 18, 2, "本期计划应偿还贷款本金", ""),
    ("QM_LOAN_027", "LOAN", "还款管理", "本期实还本金", "ACTUAL_PRIN_AMT", "Actual Paid Principal", "金额类", "业务数据", "L3", "DECIMAL", 18, 2, "本期实际已扣收归还的贷款本金", ""),
    ("QM_LOAN_028", "LOAN", "信贷属性", "贷款投向用途", "LOAN_PURPOSE_DESC", "Loan Purpose Description", "文本类", "EAST 5.0", "L2", "VARCHAR", 120, 0, "借款人申请并使用该笔信贷资金的具体用途说明", ""),
    ("QM_LOAN_029", "LOAN", "信贷属性", "主要担保方式代码", "MAIN_GUAR_TYPE_CD", "Main Guarantee Type Code", "代码类", "EAST 5.0", "L2", "CHAR", 2, 0, "贷款合同项下最核心的信用缓释担保方式", "01-信用 02-保证 03-抵押 04-质押"),
    ("QM_LOAN_030", "LOAN", "信贷属性", "借据核算状态代码", "RECEIPT_STATUS_CD", "Loan Receipt Status Code", "代码类", "EAST 5.0", "L2", "CHAR", 2, 0, "贷款借据当前生命周期核算状态", "01-正常 02-逾期 03-呆滞 04-核销 09-结清"),

    # ================= 4. TXN 支付交易域 (25条) =================
    ("QM_TXN_001", "TXN", "交易标识", "交易流水号", "TXN_SERIAL_NO", "Transaction Serial Number", "编码类", "EAST 5.0", "L3", "VARCHAR", 40, 0, "核心清算平台生成的全局唯一交易流水号", ""),
    ("QM_TXN_002", "TXN", "交易时间", "交易日期", "TXN_DT", "Transaction Date", "日期类", "EAST 5.0", "L2", "DATE", 10, 0, "金融交易会计记账发生日期", ""),
    ("QM_TXN_003", "TXN", "交易时间", "交易发生时间戳", "TXN_TS", "Transaction Timestamp", "时间类", "反洗钱", "L2", "TIMESTAMP", 19, 0, "交易到达核心系统的精确时间戳", ""),
    ("QM_TXN_004", "TXN", "交易要素", "借贷标志代码", "DC_FLAG_CD", "Debit Credit Flag", "代码类", "EAST 5.0", "L1", "CHAR", 1, 0, "标识资金流向为借方流出或贷方流入", "D-借方支出 C-贷方收入"),
    ("QM_TXN_005", "TXN", "交易金额", "交易发生金额", "TXN_AMT", "Transaction Amount", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "单笔金融交易实际发生金额", ""),
    ("QM_TXN_006", "TXN", "交易金额", "交易后账户余额", "AFTER_BAL_AMT", "Balance After Transaction", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "本笔交易入账后的账户实时余额", ""),
    ("QM_TXN_007", "TXN", "交易要素", "交易渠道代码", "TXN_CHANNEL_CD", "Transaction Channel Code", "代码类", "反洗钱", "L2", "VARCHAR", 12, 0, "发起交易的渠道类型编码", "EBANK-网银 MBANK-手机银行 COUNTER-柜面 ATM-自助机具 POS-消费"),
    ("QM_TXN_008", "TXN", "交易要素", "交易类型代码", "TXN_TYPE_CD", "Transaction Type Code", "代码类", "EAST 5.0", "L2", "VARCHAR", 12, 0, "转账、代发、汇兑、放款、扣款等业务类型", "TRF-转账 PAY-代发 LOAN-放款 REPAY-还款 FEE-手续费"),
    ("QM_TXN_009", "TXN", "交易要素", "交易摘要说明", "TXN_SUMMARY", "Transaction Summary", "文本类", "EAST 5.0", "L2", "VARCHAR", 200, 0, "客户填写或系统自动生成的交易附言摘要", ""),
    ("QM_TXN_010", "TXN", "交易对手", "对手方账号", "OPP_ACCT_NO", "Counterparty Account No", "编码类", "反洗钱", "L4", "VARCHAR", 36, 0, "资金划转对手方银行账号", ""),
    ("QM_TXN_011", "TXN", "交易对手", "对手方户名", "OPP_ACCT_NAME", "Counterparty Account Name", "名称类", "反洗钱", "L3", "VARCHAR", 120, 0, "资金划转对手方账户名称", ""),
    ("QM_TXN_012", "TXN", "交易对手", "对手方开户行号", "OPP_BANK_CD", "Counterparty Bank Code", "编码类", "EAST 5.0", "L2", "VARCHAR", 16, 0, "人行现代化支付系统12位联行号", ""),
    ("QM_TXN_013", "TXN", "交易对手", "对手方开户行名", "OPP_BANK_NAME", "Counterparty Bank Name", "名称类", "EAST 5.0", "L2", "VARCHAR", 120, 0, "对手方账户所属商业银行分支机构名称", ""),
    ("QM_TXN_014", "TXN", "交易要素", "现转标志代码", "CASH_TRF_IND", "Cash Transfer Indicator", "代码类", "反洗钱", "L1", "CHAR", 1, 0, "区分现金交易与非现金转账交易", "1-现金 2-转账"),
    ("QM_TXN_015", "TXN", "反洗钱监控", "大额交易上报标识", "LARGE_TXN_IND", "Large Transaction Flag", "指示器类", "反洗钱", "L2", "CHAR", 1, 0, "标识单笔或当日累计是否触达人行大额交易报告标准", "0-未触发 1-已触发"),
    ("QM_TXN_016", "TXN", "反洗钱监控", "可疑拆单预警标识", "AML_SUSPECT_IND", "AML Structuring Alert Flag", "指示器类", "反洗钱", "L3", "CHAR", 1, 0, "标识是否命中有意图规避5万元门槛的高频拆单规则", "0-正常 1-可疑"),
    ("QM_TXN_017", "TXN", "交易要素", "交易状态代码", "TXN_STATUS_CD", "Transaction Status Code", "代码类", "EAST 5.0", "L1", "CHAR", 2, 0, "交易处理结果状态", "00-成功 01-冲正 02-失败"),
    ("QM_TXN_018", "TXN", "交易要素", "柜员号", "TELLER_ID", "Teller ID", "编码类", "EAST 5.0", "L2", "VARCHAR", 16, 0, "办理柜面或复核交易的经办柜员工号", ""),
    ("QM_TXN_019", "TXN", "交易要素", "终端设备IP", "TERM_IP_ADDR", "Terminal IP Address", "文本类", "反洗钱", "L3", "VARCHAR", 48, 0, "电子渠道交易客户端源IP地址", ""),
    ("QM_TXN_020", "TXN", "交易金额", "手续费金额", "TXN_FEE_AMT", "Transaction Fee Amount", "金额类", "业务数据", "L2", "DECIMAL", 18, 2, "单笔支付结算向客户收取的手续费", ""),
    ("QM_TXN_021", "TXN", "交易统计", "日累计交易笔数", "DAILY_TXN_CNT", "Daily Transaction Count", "数值类", "反洗钱", "L2", "INT", 10, 0, "单账户单自然日累计发生的有效交易总笔数", ""),
    ("QM_TXN_022", "TXN", "交易统计", "日累计交易金额", "DAILY_TXN_AMT", "Daily Transaction Amount", "金额类", "反洗钱", "L3", "DECIMAL", 18, 2, "单账户单自然日累计发生的交易总金额", ""),
    ("QM_TXN_023", "TXN", "交易统计", "近3日拆单笔数", "SPLIT_3D_CNT", "3-Day Structuring Count", "数值类", "反洗钱", "L3", "INT", 10, 0, "近3日金额位于4.5万至5万元区间的疑似拆单笔数", ""),
    ("QM_TXN_024", "TXN", "交易要素", "跨境交易标识", "CROSS_BORDER_IND", "Cross-Border Indicator", "指示器类", "反洗钱", "L2", "CHAR", 1, 0, "标识资金划转是否涉及境外银行或离岸账户", "0-境内 1-跨境"),
    ("QM_TXN_025", "TXN", "交易要素", "核心凭证号", "VOUCHER_NO", "Core Voucher Number", "编码类", "EAST 5.0", "L2", "VARCHAR", 32, 0, "核心记账关联的电子或纸质凭证号码", ""),

    # ================= 5. COLL 担保押品域 (18条) =================
    ("QM_COLL_001", "COLL", "担保标识", "担保合同编号", "GUAR_CONTRACT_NO", "Guarantee Contract No", "编码类", "EAST 5.0", "L3", "VARCHAR", 36, 0, "抵质押或连带保证担保从合同唯一编号", ""),
    ("QM_COLL_002", "COLL", "押品标识", "押品统一编号", "COLLATERAL_ID", "Collateral Unified ID", "编码类", "1104监管", "L3", "VARCHAR", 32, 0, "押品管理系统赋予每项抵质押物的唯一编码", ""),
    ("QM_COLL_003", "COLL", "押品属性", "押品大类代码", "COLL_TYPE_CD", "Collateral Type Code", "代码类", "1104监管", "L2", "VARCHAR", 8, 0, "不动产、金融质押品、应收账款等押品分类", "REAL-房产 LAND-土地 FIN-存单证券 EQP-设备 GUAR-保证"),
    ("QM_COLL_004", "COLL", "押品属性", "押品名称描述", "COLL_NAME", "Collateral Description", "名称类", "EAST 5.0", "L2", "VARCHAR", 120, 0, "抵质押资产具体权属及名称描述", ""),
    ("QM_COLL_005", "COLL", "押品估值", "押品评估价值", "EVAL_VALUE_AMT", "Appraised Collateral Value", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "经认可评估机构或内部模型评定的押品公允价值", ""),
    ("QM_COLL_006", "COLL", "押品估值", "核定抵质押率", "PLEDGE_RATIO", "Loan-to-Value Pledge Ratio", "比率类", "1104监管", "L2", "DECIMAL", 8, 4, "担保债权本金与押品评估价值的比率上限", ""),
    ("QM_COLL_007", "COLL", "押品估值", "担保覆盖金额", "GUAR_COVER_AMT", "Guarantee Covered Amount", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "该项担保实际为对应贷款提供的风险缓释额度", ""),
    ("QM_COLL_008", "COLL", "保证人", "保证人客户编号", "GUARANTOR_CUST_ID", "Guarantor Customer ID", "编码类", "客户风险", "L3", "VARCHAR", 32, 0, "提供连带责任保证的法人或自然人客户编号", ""),
    ("QM_COLL_009", "COLL", "保证人", "保证人名称", "GUARANTOR_NAME", "Guarantor Full Name", "名称类", "客户风险", "L3", "VARCHAR", 120, 0, "保证人法定中文名称", ""),
    ("QM_COLL_010", "COLL", "押品状态", "押品登记状态代码", "COLL_STATUS_CD", "Collateral Status Code", "代码类", "1104监管", "L2", "CHAR", 2, 0, "押品权证入库及抵质押登记状态", "01-已入库 02-办理中 03-已出库 04-处置中"),
    ("QM_COLL_011", "COLL", "押品估值", "最近重估日期", "REVAL_DT", "Last Revaluation Date", "日期类", "1104监管", "L2", "DATE", 10, 0, "押品最近一次完成价值重估的日期", ""),
    ("QM_COLL_012", "COLL", "权证信息", "权证编号", "CERT_RIGHT_NO", "Ownership Certificate No", "编码类", "EAST 5.0", "L3", "VARCHAR", 48, 0, "不动产权证号、存单号或质押登记证明号", ""),
    ("QM_COLL_013", "COLL", "权证信息", "登记机关名称", "REG_AUTHORITY_NAME", "Registration Authority", "名称类", "业务数据", "L2", "VARCHAR", 100, 0, "办理抵质押登记的法定主管部门名称", ""),
    ("QM_COLL_014", "COLL", "押品属性", "合格缓释工具标识", "QUALIFIED_MITIGANT_IND", "Qualified Mitigant Flag", "指示器类", "1104监管", "L2", "CHAR", 1, 0, "标识该押品是否满足巴塞尔协议合格风险缓释认定", "0-不合格 1-合格"),
    ("QM_COLL_015", "COLL", "押品属性", "顺位编号", "MORTGAGE_RANK_NO", "Mortgage Priority Rank", "数值类", "1104监管", "L2", "INT", 10, 0, "抵押权顺位序号(1为第一顺位)", ""),
    ("QM_COLL_016", "COLL", "押品估值", "押品初始价值", "INIT_VALUE_AMT", "Initial Collateral Value", "金额类", "业务数据", "L3", "DECIMAL", 18, 2, "押品首次入库时确认的评估基准价值", ""),
    ("QM_COLL_017", "COLL", "押品属性", "押品所在地区代码", "COLL_REGION_CD", "Collateral Region Code", "代码类", "业务数据", "L1", "CHAR", 6, 0, "GB/T 2260六位行政区划代码", ""),
    ("QM_COLL_018", "COLL", "押品属性", "保险到期日期", "INSURANCE_EXPIRE_DT", "Insurance Expire Date", "日期类", "业务数据", "L2", "DATE", 10, 0, "押品财产保险单有效期截止日期", ""),

    # ================= 6. RISK 风险监管域 (18条) =================
    ("QM_RISK_001", "RISK", "信用风险", "风险暴露总金额", "RISK_EXPOSURE_AMT", "Total Risk Exposure Amount", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "表内外信贷与同业业务折算后的信用风险总敞口", ""),
    ("QM_RISK_002", "RISK", "信用风险", "不良贷款余额", "NPL_BAL_AMT", "Non-Performing Loan Balance", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "五级分类为次级、可疑、损失三类贷款本金余额之和", ""),
    ("QM_RISK_003", "RISK", "信用风险", "不良贷款率", "NPL_RATIO", "Non-Performing Loan Ratio", "比率类", "1104监管", "L3", "DECIMAL", 8, 4, "不良贷款余额占各项贷款总余额的比率", ""),
    ("QM_RISK_004", "RISK", "拨备管理", "贷款减值准备余额", "PROVISION_BAL_AMT", "Loan Loss Provision Balance", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "按预期信用损失法计提的贷款减值准备余额", ""),
    ("QM_RISK_005", "RISK", "拨备管理", "拨备覆盖率", "PROVISION_COV_RATIO", "Provision Coverage Ratio", "比率类", "1104监管", "L3", "DECIMAL", 10, 4, "贷款损失准备金与不良贷款余额之比", ""),
    ("QM_RISK_006", "RISK", "内评模型", "违约概率PD", "PD_RATE", "Probability of Default", "比率类", "客户风险", "L3", "DECIMAL", 10, 6, "内评法测算的客户未来12个月违约概率", ""),
    ("QM_RISK_007", "RISK", "内评模型", "违约损失率LGD", "LGD_RATE", "Loss Given Default", "比率类", "客户风险", "L3", "DECIMAL", 10, 6, "违约发生后预计损失占风险暴露的比例", ""),
    ("QM_RISK_008", "RISK", "内评模型", "客户内评信用评分", "RISK_SCORE", "Internal Credit Risk Score", "数值类", "客户风险", "L2", "DECIMAL", 10, 4, "客户信用评分卡综合得分(0~1000)", ""),
    ("QM_RISK_009", "RISK", "名单管理", "黑名单命中标识", "BLACKLIST_IND", "Blacklist Hit Indicator", "指示器类", "反洗钱", "L3", "CHAR", 1, 0, "是否命中人行/司法/行内高危黑名单", "0-未命中 1-命中"),
    ("QM_RISK_010", "RISK", "监管报送", "EAST报送批次号", "EAST_BATCH_NO", "EAST Submission Batch No", "编码类", "EAST 5.0", "L2", "VARCHAR", 24, 0, "银保监EAST 5.0定期报送数据批次编号", ""),
    ("QM_RISK_011", "RISK", "监管报送", "监管指标代码", "REG_IND_CD", "Regulatory Indicator Code", "编码类", "1104监管", "L2", "VARCHAR", 20, 0, "1104非现场监管报表唯一指标编码", ""),
    ("QM_RISK_012", "RISK", "风险预警", "风险预警等级代码", "ALERT_LEVEL_CD", "Risk Alert Level Code", "代码类", "反洗钱", "L2", "VARCHAR", 8, 0, "风险监控系统触发的预警严重级别", "LOW-低 MED-中 HIGH-高 CRIT-紧急"),
    ("QM_RISK_013", "RISK", "风险预警", "可疑案件编号", "AML_CASE_ID", "AML Suspicious Case ID", "编码类", "反洗钱", "L3", "VARCHAR", 32, 0, "反洗钱人工甄别立案生成的案件唯一编号", ""),
    ("QM_RISK_014", "RISK", "风险预警", "预警触发规则代码", "ALERT_RULE_CD", "Alert Trigger Rule Code", "编码类", "反洗钱", "L2", "VARCHAR", 24, 0, "命中反洗钱或信用预警模型的规则标识", ""),
    ("QM_RISK_015", "RISK", "信用风险", "逾期账龄分段代码", "OVERDUE_BUCKET_CD", "Overdue Aging Bucket Code", "代码类", "1104监管", "L2", "VARCHAR", 8, 0, "按逾期天数划分的M0~M6+账龄区间", "M0-未逾期 M1-1~30天 M2-31~60天 M3-61~90天 M6+-180天以上"),
    ("QM_RISK_016", "RISK", "流动性", "存贷比指标值", "LOAN_TO_DEP_RATIO", "Loan to Deposit Ratio", "比率类", "1104监管", "L2", "DECIMAL", 8, 4, "各项贷款余额与各项存款余额之比", ""),
    ("QM_RISK_017", "RISK", "合规审计", "数据质量异常标识", "DQ_ANOMALY_IND", "Data Quality Anomaly Flag", "指示器类", "EAST 5.0", "L2", "CHAR", 1, 0, "标识记录是否触发监管报送前逻辑校验异常", "0-正常 1-异常"),
    ("QM_RISK_018", "RISK", "合规审计", "异常原因说明", "ANOMALY_REASON", "Anomaly Reason Description", "文本类", "EAST 5.0", "L2", "VARCHAR", 200, 0, "触发风控或数据质量告警的具体规则描述", ""),

    # ================= 7. FIN 财务核算域 (14条) =================
    ("QM_FIN_001", "FIN", "会计科目", "会计科目代码", "GL_SUBJ_CD", "General Ledger Subject Code", "编码类", "EAST 5.0", "L2", "VARCHAR", 16, 0, "商业银行总账标准会计科目编号", ""),
    ("QM_FIN_002", "FIN", "会计科目", "会计科目名称", "GL_SUBJ_NAME", "General Ledger Subject Name", "名称类", "EAST 5.0", "L2", "VARCHAR", 80, 0, "总账会计科目中文标准名称", ""),
    ("QM_FIN_003", "FIN", "发生额", "借方发生额", "DEBIT_AMT", "Debit Transaction Amount", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "会计期间内总账科目借方累计发生金额", ""),
    ("QM_FIN_004", "FIN", "发生额", "贷方发生额", "CREDIT_AMT", "Credit Transaction Amount", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "会计期间内总账科目贷方累计发生金额", ""),
    ("QM_FIN_005", "FIN", "期末余额", "科目期末余额", "GL_END_BAL_AMT", "GL Ending Balance Amount", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "会计期末轧差后的总账科目余额", ""),
    ("QM_FIN_006", "FIN", "损益核算", "利息收入金额", "INT_INCOME_AMT", "Interest Income Amount", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "报告期内确认的贷款及同业利息收入", ""),
    ("QM_FIN_007", "FIN", "损益核算", "利息支出金额", "INT_EXPENSE_AMT", "Interest Expense Amount", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "报告期内确认的存款及拆借利息支出", ""),
    ("QM_FIN_008", "FIN", "损益核算", "中间业务收入", "FEE_INCOME_AMT", "Fee Commission Income", "金额类", "1104监管", "L3", "DECIMAL", 18, 2, "结算、托管、代销等手续费及佣金收入", ""),
    ("QM_FIN_009", "FIN", "税务核算", "增值税税率", "VAT_TAX_RATE", "VAT Tax Rate", "比率类", "业务数据", "L1", "DECIMAL", 8, 4, "金融服务适用的增值税税率", ""),
    ("QM_FIN_010", "FIN", "管理会计", "利润中心编码", "PROFIT_CENTER_CD", "Profit Center Code", "编码类", "业务数据", "L2", "VARCHAR", 16, 0, "内部资金转移定价(FTP)归属利润中心代码", ""),
    ("QM_FIN_011", "FIN", "管理会计", "FTP内部转移利率", "FTP_RATE", "Funds Transfer Pricing Rate", "比率类", "业务数据", "L2", "DECIMAL", 10, 6, "司库核定的内部资金转移定价年化利率", ""),
    ("QM_FIN_012", "FIN", "损益核算", "净息差NIM", "NET_INT_MARGIN", "Net Interest Margin", "比率类", "1104监管", "L2", "DECIMAL", 8, 4, "净利息收入占平均生息资产的年化比率", ""),
    ("QM_FIN_013", "FIN", "财务对账", "对账差异金额", "RECON_DIFF_AMT", "Reconciliation Diff Amount", "金额类", "EAST 5.0", "L3", "DECIMAL", 18, 2, "总分核对或核心与数仓对账产生的差异金额", ""),
    ("QM_FIN_014", "FIN", "财务对账", "会计记账日期", "ACCT_DT", "Accounting Posting Date", "日期类", "EAST 5.0", "L2", "DATE", 10, 0, "总账系统入账归属的会计营业日", ""),

    # ================= 8. ORG 公共机构与数仓治理域 (14条) =================
    ("QM_ORG_001", "ORG", "机构主数据", "机构编号", "BRANCH_ID", "Branch Organization ID", "编码类", "EAST 5.0", "L1", "VARCHAR", 16, 0, "全行总分支机构唯一编码", ""),
    ("QM_ORG_002", "ORG", "机构主数据", "机构中文名称", "BRANCH_NAME", "Branch Organization Name", "名称类", "EAST 5.0", "L1", "VARCHAR", 80, 0, "分支机构对外规范中文全称", ""),
    ("QM_ORG_003", "ORG", "机构主数据", "上级机构编号", "PARENT_BRANCH_ID", "Parent Branch ID", "编码类", "EAST 5.0", "L1", "VARCHAR", 16, 0, "当前机构直属管辖上级分行或总行编码", ""),
    ("QM_ORG_004", "ORG", "机构主数据", "机构层级代码", "BRANCH_LVL_CD", "Branch Hierarchy Level", "代码类", "业务数据", "L1", "CHAR", 1, 0, "机构行政管理层级", "1-总行 2-一级分行 3-二级分行 4-支行网点"),
    ("QM_ORG_005", "ORG", "机构主数据", "行政区划代码", "REGION_CD", "Administrative Region Code", "代码类", "EAST 5.0", "L1", "CHAR", 6, 0, "GB/T 2260六位行政区划代码", ""),
    ("QM_ORG_006", "ORG", "机构主数据", "金融许可证号", "FIN_LICENSE_NO", "Financial License Number", "编码类", "EAST 5.0", "L2", "VARCHAR", 24, 0, "银保监核发的金融机构许可证编码", ""),
    ("QM_ORG_007", "ORG", "人员主数据", "客户经理工号", "EMP_ID", "Employee Relationship Manager ID", "编码类", "业务数据", "L2", "VARCHAR", 16, 0, "全行人力资源系统唯一员工工号", ""),
    ("QM_ORG_008", "ORG", "人员主数据", "员工姓名", "EMP_NAME", "Employee Full Name", "名称类", "业务数据", "L2", "VARCHAR", 48, 0, "银行员工法定姓名", ""),
    ("QM_ORG_009", "ORG", "人员主数据", "所属条线部门代码", "DEPT_CD", "Department Code", "代码类", "业务数据", "L1", "VARCHAR", 16, 0, "公司金融、零售金融、普惠小微、风控合规等条线", "CORP-公司部 RET-零售部 SME-普惠部 RISK-风控部"),
    ("QM_ORG_010", "ORG", "数仓公共", "数据统计日期", "STAT_DT", "Statistics Snapshot Date", "日期类", "技术数据", "L1", "DATE", 10, 0, "数仓T+1批处理业务统计切片日期", ""),
    ("QM_ORG_011", "ORG", "数仓公共", "源系统代码", "SRC_SYS_CD", "Source System Code", "代码类", "技术数据", "L1", "VARCHAR", 16, 0, "数据来源上游应用系统简称", "CORE-核心 ECIF-客管 CMS-信贷 AML-反洗钱"),
    ("QM_ORG_012", "ORG", "数仓公共", "记录有效标识", "VALID_IND", "Record Valid Indicator", "指示器类", "技术数据", "L1", "CHAR", 1, 0, "标识当前主数据或事实记录是否有效", "0-无效 1-有效"),
    ("QM_ORG_013", "ORG", "数仓公共", "ETL加载时间戳", "ETL_LOAD_TS", "ETL Load Timestamp", "时间类", "技术数据", "L1", "TIMESTAMP", 19, 0, "数据仓库任务落盘写入时间", ""),
    ("QM_ORG_014", "ORG", "数仓公共", "数据分区日期", "PT_DT", "Partition Date String", "编码类", "技术数据", "L1", "VARCHAR", 10, 0, "大数据平台按日分区字符串(YYYY-MM-DD)", ""),
]


def build_standard_items() -> List[Dict[str, Any]]:
    """返回完整 164 条标准字典记录，字段结构严格对齐 Data.O.G `standard_dictionary` 表."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    items: List[Dict[str, Any]] = []
    for idx, row in enumerate(_RAW_STANDARDS, start=1):
        (
            std_code,
            domain_key,
            cat_l2,
            cn_name,
            en_abbr,
            en_name,
            data_cat,
            reg_cat,
            sec_lvl,
            kind,
            length,
            scale,
            biz_def,
            ref_code_detail,
        ) = row
        domain_cn, cat_l1, src_sys, ref_std_no, ref_std_name = _DOMAIN_META[domain_key]
        tb = _type_bundle(kind, length, scale)
        ref_code_no = f"CD_{domain_key}_{idx:03d}" if ref_code_detail else ""
        ref_code_name = f"{cn_name}值域代码表" if ref_code_detail else ""
        biz_rule = (
            f"须在标准代码值域内取值：{ref_code_detail}"
            if ref_code_detail
            else f"须符合{data_cat}格式要求（{tb['data_format']}），核心主键不可为空"
        )
        extra = {
            "sync_ddm": "1",
            "sync_lunaweb": "1",
            "dict_type": "基础标准",
            "dict_level": "全行级",
        }
        items.append(
            {
                "std_code": std_code,
                "reg_category": reg_cat,
                "cat_l1": cat_l1,
                "cat_l2": cat_l2,
                "cat_l3": domain_cn,
                "cn_name": cn_name,
                "cn_alias": "",
                "en_name": en_name,
                "en_abbr": en_abbr,
                "biz_definition": biz_def,
                "scope_desc": "启明商业银行全行数据中台及监管报送",
                "biz_rule": biz_rule,
                "max_length": tb["max_length"],
                "min_scale": tb["min_scale"],
                "ref_code_no": ref_code_no,
                "ref_code_name": ref_code_name,
                "ref_code_detail": ref_code_detail,
                "security_level": sec_lvl,
                "ref_std_no": ref_std_no,
                "ref_std_name": ref_std_name,
                "domain_name": f"{data_cat}({tb['type_dws']})",
                "domain_code": f"DM_{domain_key}_{idx:03d}",
                "data_category": data_cat,
                "data_format": tb["data_format"],
                "value_range": ref_code_detail or "/",
                "source_system": src_sys,
                "ddm_catalog": f"启明银行企业标准/{cat_l1}/{cat_l2}",
                "is_standard_dict": "1",
                "version_no": "V2026.1",
                "remark": "启明商业银行信创数仓示范数据标准",
                "type_dws": tb["type_dws"],
                "type_dw_mart": tb["type_dw_mart"],
                "type_mysql": tb["type_mysql"],
                "type_oracle": tb["type_oracle"],
                "type_dameng": tb["type_dameng"],
                "type_goldendb": tb["type_goldendb"],
                "type_ob_oracle": tb["type_ob_oracle"],
                "type_ob_mysql": tb["type_ob_mysql"],
                "type_argodb": tb["type_argodb"],
                "type_oceanbase": tb["type_oceanbase"],
                "extra_attrs": extra,
                "extra_attrs_json": json.dumps(extra, ensure_ascii=False),
                "updated_at": now_str,
            }
        )
    return items


def get_standard_by_code() -> Dict[str, Dict[str, Any]]:
    return {it["std_code"]: it for it in build_standard_items()}


def get_standard_by_abbr() -> Dict[str, Dict[str, Any]]:
    return {it["en_abbr"].lower(): it for it in build_standard_items()}


def _style_demo_sheet(ws: Any, col_count: int, required_indices: Optional[set[int]] = None) -> None:
    req_fill = PatternFill(start_color="1E3A8A", end_color="1E3A8A", fill_type="solid")
    opt_fill = PatternFill(start_color="334155", end_color="334155", fill_type="solid")
    header_font = Font(name="Microsoft YaHei", size=10, bold=True, color="FFFFFF")
    body_font = Font(name="Microsoft YaHei", size=10)
    thin_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="thin", color="CBD5E1"),
        bottom=Side(style="thin", color="CBD5E1"),
    )
    req = required_indices or set()
    for c_idx in range(1, col_count + 1):
        cell = ws.cell(row=1, column=c_idx)
        cell.fill = req_fill if c_idx in req else opt_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        col_letter = get_column_letter(c_idx)
        title_len = len(str(cell.value or ""))
        ws.column_dimensions[col_letter].width = max(14, min(32, title_len * 2 + 4))
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, min_col=1, max_col=col_count):
        for cell in row:
            cell.font = body_font
            cell.border = thin_border
            cell.alignment = Alignment(vertical="center")


def export_standard_dictionary_xlsx(output_path: str) -> Tuple[str, int]:
    """按 Data.O.G 46 列标准模板导出《启明商业银行金融数据标准字典》Excel."""
    items = build_standard_items()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "启明银行金融数据标准字典"
    ws.append(list(BOHAI_DICT_HEADERS))

    for it in items:
        ws.append(
            [
                it["std_code"],
                it["reg_category"],
                it["cat_l1"],
                it["cat_l2"],
                it["cat_l3"],
                it["cn_name"],
                it["cn_alias"],
                it["en_name"],
                it["en_abbr"],
                it["biz_definition"],
                it["scope_desc"],
                it["biz_rule"],
                it["max_length"],
                it["min_scale"],
                it["ref_code_no"],
                it["ref_code_name"],
                it["security_level"],
                it["ref_std_no"],
                it["ref_std_name"],
                it["domain_name"],
                it["domain_code"],
                it["type_mysql"],
                it["type_oracle"],
                it["type_dws"],
                it["type_dameng"],
                it["type_goldendb"],
                it["source_system"],
                "1",
                "1",
                it["is_standard_dict"],
                "",
                it["ddm_catalog"],
                it["ref_code_detail"],
                "基础标准",
                "全行级",
                it["version_no"],
                it["data_category"],
                it["data_format"],
                it["value_range"],
                it["type_ob_oracle"],
                it["type_ob_mysql"],
                it["type_argodb"],
                it["type_oceanbase"],
                it["type_dw_mart"],
                it["type_dws"],
                it["remark"],
            ]
        )

    _style_demo_sheet(ws, len(BOHAI_DICT_HEADERS), required_indices={1, 6, 9, 45})
    wb.save(output_path)
    return output_path, len(items)


def export_binding_import_template_xlsx(output_path: str) -> Tuple[str, int]:
    """导出用于现场演示「批量对照导入绑标」的示范 Excel（覆盖 3 张典型 DWS 治理靶表）."""
    by_code = get_standard_by_code()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "字段与数据标准对照"
    headers = [
        "模式名",
        "表名",
        "物理字段名",
        "库内当前类型(参考)",
        "库内当前注释(参考)",
        "数据项编号",
        "标准英文简称(选填)",
        "标准中文名称(选填)",
    ]
    ws.append(headers)

    sample_rows = [
        # 1. dwd_cust_base_info：涵盖老旧缩写、长度不足、仅缺注释、完全达标
        ("dwd_bank", "dwd_cust_base_info", "khbh", "character varying(18)", "老客户编号", "QM_CUST_001"),
        ("dwd_bank", "dwd_cust_base_info", "cust_nm", "character varying(60)", "客户名称", "QM_CUST_002"),
        ("dwd_bank", "dwd_cust_base_info", "cust_type_cd", "character(2)", "客户类型代码", "QM_CUST_003"),
        ("dwd_bank", "dwd_cust_base_info", "cert_type_cd", "character varying(8)", "", "QM_CUST_004"),
        ("dwd_bank", "dwd_cust_base_info", "zjhm", "character varying(18)", "证件号", "QM_CUST_005"),
        ("dwd_bank", "dwd_cust_base_info", "mobile_no", "character varying(20)", "", "QM_CUST_012"),
        ("dwd_bank", "dwd_cust_base_info", "cust_risk_lvl_cd", "character varying(4)", "客户风险评级代码", "QM_CUST_011"),
        ("dwd_bank", "dwd_cust_base_info", "branch_id", "character varying(16)", "机构编号", "QM_ORG_001"),
        # 2. dwd_loan_contract：DIRECT_ALTER 现场真改表靶表
        ("dwd_bank", "dwd_loan_contract", "htbh", "character varying(24)", "老合同号", "QM_LOAN_001"),
        ("dwd_bank", "dwd_loan_contract", "khbh", "character varying(18)", "客户号", "QM_CUST_001"),
        ("dwd_bank", "dwd_loan_contract", "loan_prod_cd", "character varying(16)", "", "QM_LOAN_004"),
        ("dwd_bank", "dwd_loan_contract", "dkje", "numeric(14,2)", "合同金额", "QM_LOAN_006"),
        ("dwd_bank", "dwd_loan_contract", "zxlv", "numeric(8,4)", "利率", "QM_LOAN_011"),
        ("dwd_bank", "dwd_loan_contract", "dkqx", "smallint", "期限", "QM_LOAN_014"),
        ("dwd_bank", "dwd_loan_contract", "sign_dt_str", "character varying(10)", "签约日期文本", "QM_LOAN_015"),
        ("dwd_bank", "dwd_loan_contract", "branch_id", "character varying(16)", "机构编号", "QM_ORG_001"),
        # 3. dwd_acct_swap_demo：TABLE_REBUILD / 链式改名保序重建靶表
        ("dwd_bank", "dwd_acct_swap_demo", "acct_code", "character varying(24)", "新主账号候选", "QM_ACCT_001"),
        ("dwd_bank", "dwd_acct_swap_demo", "acct_no", "character varying(24)", "历史老账号", "QM_ACCT_002"),
        ("dwd_bank", "dwd_acct_swap_demo", "cust_id", "character varying(32)", "客户统一编号", "QM_CUST_001"),
        ("dwd_bank", "dwd_acct_swap_demo", "acct_bal_amt", "numeric(14,2)", "余额", "QM_ACCT_008"),
        ("dwd_bank", "dwd_acct_swap_demo", "open_dt_txt", "character varying(10)", "开户日期字符", "QM_ACCT_006"),
    ]

    for sch, tbl, col, cur_t, cur_c, code in sample_rows:
        std = by_code[code]
        ws.append([sch, tbl, col, cur_t, cur_c, code, std["en_abbr"], std["cn_name"]])

    _style_demo_sheet(ws, len(headers), required_indices={1, 2, 3, 6})
    wb.save(output_path)
    return output_path, len(sample_rows)
