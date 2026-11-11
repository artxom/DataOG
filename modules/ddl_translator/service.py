"""多邻国模块核心引擎：智能数仓词根命名 + 跨方言建表 DDL 翻译与四层视图穿透 (DdlTranslatorService).

严格遵循插件隔离规范：仅依赖 `core.platform`，不与任何其他业务模块私有互引。
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional, Tuple

from core.platform.context import PlatformContext
from core.platform.dialects import (
    build_dialect_ddl,
    clean_identifier_input,
    enrich_view_columns_by_lineage,
    format_readable_sql_ddl,
    parse_view_sql_lineage,
)
from core.platform.llm_hub import extract_json_payload
from core.platform.models import ColumnMeta, TableMeta

# 内置金融与通用数仓高频标准词根字典：(中文词条, 缩写词根, 全拼英文词根, 词根说明)
DW_STANDARD_ROOTS: Tuple[Tuple[str, str, str, str], ...] = (
    ("统一社会信用代码", "uscc_no", "unified_social_credit_code", "证照标识词根"),
    ("身份证号码", "id_card_no", "id_card_number", "证件号码词根"),
    ("身份证号", "id_card_no", "id_card_number", "证件号码词根"),
    ("证件号码", "cert_no", "certificate_number", "证件号码词根"),
    ("证件类型", "cert_type_cd", "certificate_type_code", "类型代码词根"),
    ("手机号码", "mobile_no", "mobile_number", "联系方式词根"),
    ("手机号", "mobile_no", "mobile_number", "联系方式词根"),
    ("联系电话", "contact_tel_no", "contact_phone_number", "联系方式词根"),
    ("电子邮箱", "email_addr", "email_address", "联系方式词根"),
    ("创建时间", "create_tm", "created_time", "审计时间戳词根"),
    ("更新时间", "update_tm", "updated_time", "审计时间戳词根"),
    ("修改时间", "modify_tm", "modified_time", "审计时间戳词根"),
    ("生效日期", "eff_dt", "effective_date", "生命周期日期"),
    ("失效日期", "exp_dt", "expiry_date", "生命周期日期"),
    ("到期日期", "mat_dt", "maturity_date", "金融到期日期"),
    ("开户日期", "open_dt", "open_date", "账户日期词根"),
    ("销户日期", "close_dt", "close_date", "账户日期词根"),
    ("出生日期", "birth_dt", "birth_date", "自然人属性"),
    ("交易日期", "txn_dt", "transaction_date", "交易日期词根"),
    ("交易时间", "txn_tm", "transaction_time", "交易时间词根"),
    ("统计日期", "stat_dt", "stat_date", "数仓统计周期"),
    ("数据日期", "data_dt", "data_date", "数仓切片日期"),
    ("分区日期", "pt_dt", "partition_date", "数仓分区键"),
    ("会计日期", "acct_dt", "accounting_date", "账务日期词根"),
    ("创建人", "created_by", "created_by", "审计操作人"),
    ("更新人", "updated_by", "updated_by", "审计操作人"),
    ("手续费", "comm_fee_amt", "commission_fee_amount", "金额费用词根"),
    ("唯一标识", "uniq_id", "unique_id", "标识符词根"),
    ("主键标识", "pk_id", "primary_id", "主键词根"),
    ("明细表", "dtl", "detail", "明细粒度表后缀"),
    ("汇总表", "sum", "summary", "汇总粒度表后缀"),
    ("配置表", "cfg", "config", "配置表后缀"),
    ("映射表", "map", "mapping", "映射表后缀"),
    ("字典表", "dict", "dictionary", "字典维表后缀"),
    ("流水表", "jnl", "journal", "流水表后缀"),
    ("日志表", "log", "log", "日志表后缀"),
    ("信息表", "info", "info", "基础信息表后缀"),
    ("宽表", "wide", "wide", "主题宽表后缀"),
    ("维表", "dim", "dim", "维度表后缀"),
    ("事实表", "fact", "fact", "事实表后缀"),
    ("客户号", "cust_no", "customer_number", "客户编号词根"),
    ("客户", "cust", "customer", "核心主题词根"),
    ("用户", "user", "user", "用户主题词根"),
    ("账户", "acct", "account", "账户主题词根"),
    ("账号", "acct_no", "account_number", "账号词根"),
    ("卡号", "card_no", "card_number", "卡号词根"),
    ("订单", "order", "order", "订单主题词根"),
    ("合同", "ctrct", "contract", "合同协议词根"),
    ("协议", "agmt", "agreement", "协议词根"),
    ("产品", "prod", "product", "产品主题词根"),
    ("商品", "sku", "merchandise", "商品主题词根"),
    ("商户", "mcht", "merchant", "商户主题词根"),
    ("机构", "org", "organization", "组织机构词根"),
    ("分行", "branch", "branch", "分支机构词根"),
    ("网点", "outlet", "outlet", "营业网点词根"),
    ("部门", "dept", "department", "部门词根"),
    ("员工", "emp", "employee", "员工词根"),
    ("渠道", "chnl", "channel", "渠道词根"),
    ("交易", "txn", "transaction", "交易动作词根"),
    ("流水", "seq", "serial", "流水词根"),
    ("凭证", "vchr", "voucher", "财务凭证词根"),
    ("贷款", "loan", "loan", "信贷主题词根"),
    ("借据", "due_bill", "due_bill", "信贷借据词根"),
    ("存款", "dep", "deposit", "存款主题词根"),
    ("授信", "crdt", "credit", "授信额度词根"),
    ("额度", "lmt_amt", "limit_amount", "额度金额词根"),
    ("本金", "prin_amt", "principal_amount", "本金词根"),
    ("利息", "int_amt", "interest_amount", "利息金额词根"),
    ("利率", "int_rate", "interest_rate", "利率比例词根"),
    ("汇率", "exch_rate", "exchange_rate", "汇率词根"),
    ("税率", "tax_rate", "tax_rate", "税率词根"),
    ("余额", "bal", "balance", "时点余额词根"),
    ("总额", "tot_amt", "total_amount", "汇总金额词根"),
    ("净额", "net_amt", "net_amount", "净额词根"),
    ("金额", "amt", "amount", "标准度量词根 amt"),
    ("单价", "unit_prc", "unit_price", "价格词根"),
    ("价格", "prc", "price", "价格词根"),
    ("成本", "cost_amt", "cost_amount", "成本词根"),
    ("费用", "fee_amt", "fee_amount", "费用词根"),
    ("收入", "incm_amt", "income_amount", "收入词根"),
    ("支出", "exp_amt", "expense_amount", "支出词根"),
    ("利润", "prft_amt", "profit_amount", "利润词根"),
    ("累计", "cum", "cumulative", "累计修饰词"),
    ("历史", "hist", "history", "历史修饰词"),
    ("当前", "curr", "current", "当前修饰词"),
    ("昨日", "ystd", "yesterday", "周期修饰词"),
    ("本月", "mtd", "month_to_date", "月累计修饰词"),
    ("本年", "ytd", "year_to_date", "年累计修饰词"),
    ("平均", "avg", "average", "统计聚合词根"),
    ("最大", "max", "max", "统计聚合词根"),
    ("最小", "min", "min", "统计聚合词根"),
    ("合计", "tot", "total", "汇总修饰词"),
    ("占比", "ratio", "ratio", "比例词根"),
    ("比例", "ratio", "ratio", "比例词根"),
    ("比率", "rate", "rate", "比率词根"),
    ("笔数", "cnt", "count", "计数度量词根 cnt"),
    ("数量", "cnt", "count", "计数度量词根 cnt"),
    ("人数", "head_cnt", "head_count", "人数计数词根"),
    ("次数", "cnt", "count", "频次计数词根"),
    ("天数", "days", "days", "时长计数字段"),
    ("年龄", "age", "age", "年龄数值词根"),
    ("等级", "lvl", "level", "等级分类词根"),
    ("评分", "score", "score", "分值词根"),
    ("币种", "ccy_cd", "currency_code", "币种代码词根"),
    ("状态", "stat_cd", "status", "状态词根"),
    ("类型", "type_cd", "type", "类型词根"),
    ("类别", "cat_cd", "category", "分类词根"),
    ("编码", "cd", "code", "标准代码词根 cd"),
    ("代码", "cd", "code", "标准代码词根 cd"),
    ("编号", "no", "number", "标准编号词根 no"),
    ("序号", "seq_no", "seq_number", "序号词根"),
    ("标识", "id", "id", "标准标识词根 id"),
    ("标志", "flg", "flag", "布尔标志词根"),
    ("是否", "is", "is", "布尔前缀 is_"),
    ("有效", "valid", "valid", "有效性修饰词"),
    ("启用", "enabled", "enabled", "启用状态词根"),
    ("删除", "del", "deleted", "逻辑删除词根"),
    ("实名", "real_nm", "real_name", "实名认证词根"),
    ("逾期", "ovd", "overdue", "风险逾期词根"),
    ("冻结", "frz", "frozen", "账户状态词根"),
    ("风险", "risk", "risk", "风控主题词根"),
    ("资产", "asset", "asset", "资产主题词根"),
    ("负债", "liab", "liability", "负债主题词根"),
    ("发票", "inv", "invoice", "票据词根"),
    ("供应商", "vndr", "vendor", "供应链词根"),
    ("仓库", "wh", "warehouse", "仓储词根"),
    ("省份", "prov", "province", "地域维度词根"),
    ("城市", "city", "city", "地域维度词根"),
    ("地区", "rgn", "region", "地域维度词根"),
    ("地址", "addr", "address", "地址文本词根"),
    ("简称", "short_nm", "short_name", "名称词根"),
    ("姓名", "full_nm", "full_name", "人员姓名词根"),
    ("名称", "nm", "name", "标准名称词根 nm"),
    ("描述", "desc", "description", "长文本描述词根"),
    ("说明", "desc", "description", "说明描述词根"),
    ("备注", "remark", "remark", "备注文本词根"),
    ("日期", "dt", "date", "标准日期词根 dt"),
    ("时间戳", "ts", "timestamp", "时间戳词根"),
    ("时间", "tm", "time", "标准时间词根 tm"),
    ("年份", "yr", "year", "年份词根"),
    ("月份", "mon", "month", "月份词根"),
    ("季度", "qtr", "quarter", "季度词根"),
    ("明细", "dtl", "detail", "明细词根"),
    ("汇总", "sum", "summary", "汇总词根"),
    ("信息", "info", "info", "信息词根"),
    ("记录", "rec", "record", "记录词根"),
    ("清单", "list", "list", "清单词根"),
)

_SORTED_DW_ROOTS = tuple(sorted(DW_STANDARD_ROOTS, key=lambda item: len(item[0]), reverse=True))


def _format_identifier_case(snake_name: str, naming_style: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_]+", "_", (snake_name or "").strip())
    clean = re.sub(r"_+", "_", clean).strip("_").lower()
    if not clean:
        clean = "field_col"
    if clean[0].isdigit():
        clean = f"col_{clean}"

    style = (naming_style or "snake_case").strip()
    if style == "UPPER_SNAKE":
        return clean.upper()
    if style == "camelCase":
        parts = [p for p in clean.split("_") if p]
        if not parts:
            return clean
        return parts[0] + "".join(p.capitalize() for p in parts[1:])
    return clean


def _recommend_sql_type(snake_name: str, cn_text: str, target_dialect: str) -> str:
    low = (snake_name or "").lower()
    dialect = (target_dialect or "DWS").strip().upper()
    num_18_2 = "NUMERIC(18,2)" if dialect in ("PGSQL", "DWS") else "DECIMAL(18,2)"
    num_10_6 = "NUMERIC(10,6)" if dialect in ("PGSQL", "DWS") else "DECIMAL(10,6)"
    ts_type = "DATETIME" if dialect == "MYSQL" else "TIMESTAMP"
    bool_int = "TINYINT" if dialect in ("MYSQL", "INCEPTOR") else "SMALLINT"

    if low.startswith("is_") or low.endswith("_flg") or low.endswith("_flag") or "是否" in cn_text:
        return bool_int
    if any(
        low.endswith(sfx)
        for sfx in ("_amt", "_amount", "_bal", "_balance", "_prc", "_price", "_fee", "_cost")
    ) or any(k in cn_text for k in ("金额", "余额", "总额", "净额", "本金", "利息", "费用", "单价", "成本")):
        return num_18_2
    if any(low.endswith(sfx) for sfx in ("_rate", "_ratio")) or any(
        k in cn_text for k in ("利率", "汇率", "税率", "占比", "比率", "比例")
    ):
        return num_10_6
    if any(low.endswith(sfx) for sfx in ("_cnt", "_count")) or any(
        k in cn_text for k in ("笔数", "数量", "人数", "次数")
    ):
        return "BIGINT"
    if any(low.endswith(sfx) for sfx in ("_days", "_age", "_yr", "_mon")) or any(
        k in cn_text for k in ("天数", "年龄", "年份", "月份")
    ):
        return "INTEGER"
    if low.endswith("_dt") or low.endswith("_date") or "日期" in cn_text:
        return "DATE"
    if any(low.endswith(sfx) for sfx in ("_tm", "_time", "_ts", "_at")) or "时间" in cn_text:
        return ts_type
    if any(low.endswith(sfx) for sfx in ("_desc", "_description", "_remark", "_addr", "_address")) or any(
        k in cn_text for k in ("描述", "说明", "备注", "地址")
    ):
        return "VARCHAR(512)"
    if any(low.endswith(sfx) for sfx in ("_cd", "_code", "_stat", "_status", "_type")):
        return "VARCHAR(32)"
    if any(low.endswith(sfx) for sfx in ("_id", "_no", "_number")):
        return "VARCHAR(64)"
    return "VARCHAR(128)"


def _translate_single_term_locally(
    raw_cn: str,
    *,
    granularity: str,
    naming_style: str,
    table_prefix: str,
    use_abbrev: bool,
    target_dialect: str,
) -> Dict[str, str]:
    cn = re.sub(r"^\s*(?:\d+[\.\、\)\s]+|[-*•]\s*)", "", (raw_cn or "").strip()).strip()
    # 若本身已是纯英文标识符，仅做规范化
    if re.match(r"^[a-zA-Z][a-zA-Z0-9_\s-]*$", cn):
        base_snake = re.sub(r"[\s-]+", "_", cn).lower()
        notes_str = "英文标识符规范化"
    else:
        working = cn
        if granularity == "table" and working.endswith("表") and len(working) > 2:
            # 保留特定复合后缀如 明细表/汇总表/配置表，仅剥离孤立尾字「表」
            if not any(working.endswith(w) for w in ("明细表", "汇总表", "配置表", "映射表", "字典表", "流水表", "日志表", "信息表", "宽表", "维表", "事实表")):
                working = working[:-1]

        tokens: List[str] = []
        matched_notes: List[str] = []
        idx = 0
        while idx < len(working):
            ch = working[idx]
            if ch in (" ", "\t", "_", "-", "/", "（", "）", "(", ")", "：", ":", "、", "，", ","):
                idx += 1
                continue
            ascii_m = re.match(r"^[a-zA-Z0-9]+", working[idx:])
            if ascii_m:
                seg = ascii_m.group(0).lower()
                tokens.append(seg)
                idx += len(seg)
                continue

            hit = None
            for cn_key, abbr_val, full_val, rule_desc in _SORTED_DW_ROOTS:
                if working.startswith(cn_key, idx):
                    hit = (cn_key, abbr_val if use_abbrev else full_val, rule_desc)
                    break
            if hit is not None:
                cn_key, eng_tok, rule_desc = hit
                if eng_tok:
                    tokens.append(eng_tok)
                if len(matched_notes) < 3:
                    matched_notes.append(f"{cn_key}→{eng_tok}")
                idx += len(cn_key)
            else:
                # 未收录单字跳过连接虚词（的/与/及/按/中），其余保留可读哈希后缀防止重名
                if ch not in ("的", "与", "及", "和", "之", "按", "中", "内", "表", "列", "字段"):
                    tokens.append(f"c{ord(ch) % 97:02d}")
                idx += 1

        # 去重相邻重复词根（如 客户号 + 编号 -> cust_no）
        deduped: List[str] = []
        for tok in "_".join(tokens).split("_"):
            if not tok:
                continue
            if deduped and deduped[-1] == tok:
                continue
            deduped.append(tok)

        base_snake = "_".join(deduped) or "std_col"
        notes_str = " · ".join(matched_notes) if matched_notes else "通用命名映射"

    if granularity == "table" and table_prefix:
        pfx = clean_identifier_input(table_prefix).lower().rstrip("_")
        if pfx and not base_snake.startswith(f"{pfx}_"):
            base_snake = f"{pfx}_{base_snake}"

    formatted_en = _format_identifier_case(base_snake, naming_style)
    rec_type = (
        "TABLE"
        if granularity == "table"
        else _recommend_sql_type(base_snake, cn, target_dialect)
    )
    return {
        "cn": cn,
        "en": formatted_en,
        "type": rec_type,
        "note": notes_str,
    }


def _parse_simple_create_table_sql(sql_text: str, default_schema: str = "public") -> Optional[TableMeta]:
    """轻量解析用户直接粘贴的 CREATE TABLE 语句，提取表名、列名、类型与内联 COMMENT."""
    raw = (sql_text or "").strip()
    head_m = re.search(
        r"CREATE\s+(?:EXTERNAL\s+|TEMPORARY\s+|TEMP\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s\(]+)\s*\(",
        raw,
        flags=re.IGNORECASE,
    )
    if not head_m:
        return None

    raw_tbl = clean_identifier_input(head_m.group(1))
    if "." in raw_tbl:
        sch, tbl = raw_tbl.split(".", 1)
    else:
        sch, tbl = default_schema or "public", raw_tbl

    open_paren = head_m.end() - 1
    depth = 0
    close_paren = -1
    in_quote = False
    for pos in range(open_paren, len(raw)):
        ch = raw[pos]
        if ch == "'" and (pos == 0 or raw[pos - 1] != "\\"):
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close_paren = pos
                break
    if close_paren == -1:
        return None

    body = raw[open_paren + 1 : close_paren]
    # 按顶层逗号切分列定义
    parts: List[str] = []
    buf: List[str] = []
    p_depth = 0
    in_str = False
    for ch in body:
        if ch == "'":
            in_str = not in_str
            buf.append(ch)
        elif not in_str and ch == "(":
            p_depth += 1
            buf.append(ch)
        elif not in_str and ch == ")":
            p_depth = max(0, p_depth - 1)
            buf.append(ch)
        elif not in_str and p_depth == 0 and ch == ",":
            seg = "".join(buf).strip()
            if seg:
                parts.append(seg)
            buf = []
        else:
            buf.append(ch)
    tail_seg = "".join(buf).strip()
    if tail_seg:
        parts.append(tail_seg)

    cols: List[ColumnMeta] = []
    constraint_prefixes = (
        "PRIMARY ",
        "CONSTRAINT ",
        "UNIQUE ",
        "KEY ",
        "INDEX ",
        "FOREIGN ",
        "CHECK ",
    )
    for seg in parts:
        line = re.sub(r"--.*$", "", seg, flags=re.MULTILINE).strip()
        if not line:
            continue
        up = line.upper()
        if up.startswith(constraint_prefixes):
            continue
        m_col = re.match(r'^([`"\[]?[a-zA-Z0-9_\u4e00-\u9fa5]+[`"\]]?)\s+(.+)$', line, flags=re.DOTALL)
        if not m_col:
            continue
        col_name = clean_identifier_input(m_col.group(1))
        rest = m_col.group(2).strip()
        comment_val = ""
        c_m = re.search(r"COMMENT\s+'([^']*)'", rest, flags=re.IGNORECASE)
        if c_m:
            comment_val = c_m.group(1).strip()
        # 提取最前面的数据类型（含括号精度）
        t_m = re.match(
            r"^([a-zA-Z0-9_]+(?:\s+PRECISION|\s+VARYING)?(?:\s*\([^\)]+\))?)",
            rest,
            flags=re.IGNORECASE,
        )
        col_type = t_m.group(1).strip().upper() if t_m else "VARCHAR(255)"
        is_pk = "PRIMARY KEY" in up
        not_null = "NOT NULL" in up or is_pk
        cols.append(
            ColumnMeta(
                ordinal_position=len(cols) + 1,
                column_name=col_name,
                data_type=col_type,
                is_not_null=not_null,
                is_primary_key=is_pk,
                column_comment=comment_val,
            )
        )

    if not cols:
        return None

    # 补充解析句尾 COMMENT ON COLUMN
    for cm in re.finditer(
        r"COMMENT\s+ON\s+COLUMN\s+([^\s]+)\s+IS\s+'([^']*)'",
        raw[close_paren + 1 :],
        flags=re.IGNORECASE,
    ):
        target_c = clean_identifier_input(cm.group(1)).split(".")[-1].lower()
        for col in cols:
            if col.column_name.lower() == target_c and not col.column_comment:
                col.column_comment = cm.group(2).strip()

    return TableMeta(
        schema_name=sch,
        table_name=tbl,
        exists=True,
        columns=cols,
        primary_keys=[c.column_name for c in cols if c.is_primary_key],
        raw_ddl=raw,
        object_type="TABLE",
    )


class DdlTranslatorService:
    """支持智能中文词根命名、跨库建表 DDL 翻译、四层视图血缘穿透与 AI 异构 SQL 方言深度适配."""

    def __init__(self, ctx: PlatformContext) -> None:
        self.ctx = ctx

    def _resolve_connection_id(self, mode: str, explicit_conn_id: str) -> str:
        if explicit_conn_id and explicit_conn_id.strip():
            return explicit_conn_id.strip()
        m_low = (mode or "").strip().lower()
        if m_low == "source":
            return str(self.ctx.store.get_meta("cross_source_connection_id", "") or "")
        if m_low == "target":
            return str(
                self.ctx.store.get_meta("cross_target_connection_id", "")
                or self.ctx.store.get_meta("migrator_connection_id", "")
                or ""
            )
        profiles = self.ctx.connection_hub.list_profiles()
        return profiles[0]["connection_id"] if profiles else ""

    def _build_meta_from_standard(
        self, std_table_key: str, fallback_schema: str, fallback_table: str
    ) -> TableMeta:
        t_key = clean_identifier_input(std_table_key or fallback_table).lower()
        tb_bind = self.ctx.store.get_cross_table_binding(t_key) if t_key else None
        col_binds = self.ctx.store.list_cross_columns(t_key) if t_key else []

        if col_binds:
            cols = [
                ColumnMeta(
                    ordinal_position=idx,
                    column_name=str(cb.get("std_col_name") or cb.get("orig_col_name") or f"col_{idx}"),
                    data_type=str(cb.get("std_col_type") or cb.get("orig_col_type") or "VARCHAR(255)"),
                    column_comment=str(cb.get("std_col_comment") or cb.get("orig_col_comment") or ""),
                )
                for idx, cb in enumerate(col_binds, start=1)
            ]
            return TableMeta(
                schema_name=str((tb_bind or {}).get("std_schema_name") or fallback_schema or "public"),
                table_name=str((tb_bind or {}).get("std_table_name") or fallback_table or t_key),
                exists=True,
                table_comment=str((tb_bind or {}).get("std_table_comment") or ""),
                columns=cols,
                object_type="TABLE",
            )

        mig_items = [
            it
            for it in self.ctx.store.list_items()
            if (not t_key or str(it.get("table_name", "")).lower() == t_key)
        ]
        if mig_items:
            first_tbl = str(mig_items[0].get("table_name") or fallback_table or "std_table")
            same_tbl = [
                it for it in mig_items if str(it.get("table_name", "")).lower() == first_tbl.lower()
            ]
            cols = [
                ColumnMeta(
                    ordinal_position=idx,
                    column_name=str(it.get("new_col_name") or it.get("old_col_name") or f"col_{idx}"),
                    data_type=str(it.get("new_col_type") or it.get("old_col_type") or "VARCHAR(255)"),
                    column_comment=str(it.get("new_col_comment") or it.get("old_col_comment") or ""),
                )
                for idx, it in enumerate(same_tbl, start=1)
            ]
            return TableMeta(
                schema_name=str(same_tbl[0].get("schema_name") or fallback_schema or "public"),
                table_name=first_tbl,
                exists=True,
                columns=cols,
                object_type="TABLE",
            )

        raise ValueError("当前尚未导入数据标准清单或未找到目标标准表结构，请先导入数标清单或切换为从数据源连接读取")

    def _apply_standard_overlay(
        self, meta: TableMeta, std_table_key: str, mode: str
    ) -> TableMeta:
        _ = mode
        t_key = clean_identifier_input(std_table_key or meta.table_name).lower()
        col_binds = self.ctx.store.list_cross_columns(t_key)
        if not col_binds:
            return meta

        out = copy.deepcopy(meta)
        tb_bind = self.ctx.store.get_cross_table_binding(t_key)
        if tb_bind and tb_bind.get("std_table_comment") and not out.table_comment:
            out.table_comment = str(tb_bind["std_table_comment"])

        by_src: Dict[str, Dict[str, Any]] = {}
        for cb in col_binds:
            for k in (
                cb.get("bound_source_col"),
                cb.get("orig_col_name"),
                cb.get("bound_target_col"),
                cb.get("std_col_name"),
            ):
                ck = clean_identifier_input(str(k or "")).lower()
                if ck and ck not in by_src:
                    by_src[ck] = cb

        rename_map: Dict[str, str] = {}
        for col in out.columns:
            cb = by_src.get(col.column_name.lower())
            if not cb:
                continue
            std_nm = clean_identifier_input(str(cb.get("std_col_name") or ""))
            std_tp = str(cb.get("std_col_type") or "").strip()
            std_cm = str(cb.get("std_col_comment") or "").strip()
            if std_nm and std_nm.lower() != col.column_name.lower():
                rename_map[col.column_name.lower()] = std_nm
                col.column_name = std_nm
            if std_tp:
                col.data_type = std_tp
            if std_cm:
                col.column_comment = std_cm

        if rename_map:
            out.distribute_keys = [
                rename_map.get(k.lower(), k) for k in (out.distribute_keys or [])
            ]
            out.partition_keys = [
                rename_map.get(k.lower(), k) for k in (out.partition_keys or [])
            ]
        return out

    # =========================================================================
    # 功能一：多邻国 · 智能数仓标准命名 (中文表名/字段名 -> 标准英文标识符 + 推荐类型)
    # =========================================================================

    def smart_naming(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_lines: List[str] = []
        if isinstance(payload.get("items"), list):
            raw_lines.extend(str(x) for x in payload["items"])
        raw_text = str(payload.get("text") or "")
        if raw_text.strip():
            for line in re.split(r"[\r\n,;，；]+", raw_text):
                if line.strip():
                    raw_lines.append(line.strip())

        terms: List[str] = []
        for item in raw_lines:
            cleaned = item.strip()
            if cleaned and cleaned not in terms:
                terms.append(cleaned)
            if len(terms) >= 200:
                break

        if not terms:
            raise ValueError("请输入至少一个需要翻译的中文表名或字段名称")

        granularity = str(payload.get("granularity") or "column").strip().lower()
        if granularity not in ("column", "table"):
            granularity = "column"
        naming_style = str(payload.get("naming_style") or "snake_case").strip()
        if naming_style not in ("snake_case", "UPPER_SNAKE", "camelCase"):
            naming_style = "snake_case"
        table_prefix = str(payload.get("table_prefix") or "").strip()
        use_abbrev = bool(payload.get("use_abbrev", True))
        target_dialect = str(payload.get("target_dialect") or "DWS").strip().upper()
        use_ai = bool(payload.get("use_ai", True))
        provider_id = str(payload.get("provider_id") or "").strip()
        model = str(payload.get("model") or "").strip()

        results: List[Dict[str, str]] = []
        ai_used = False
        engine_label = "内置数仓词根引擎"
        fallback_reason = ""
        elapsed_ms = 0.0

        resolved_llm = (
            self.ctx.llm_hub.resolve_provider_and_model(
                provider_id=provider_id, model=model, require_ready=False
            )
            if use_ai
            else None
        )

        if use_ai and resolved_llm is not None:
            prof, eff_model = resolved_llm
            abbrev_rule = (
                "优先采用金融与企业数仓标准词根缩写（如 金额->amt, 余额->bal, 数量/笔数->cnt, 日期->dt, 时间->tm, 编号->no, 标识->id, 代码/编码->cd, 客户->cust, 账户->acct, 机构->org, 交易->txn, 状态->stat_cd, 是否->is_）"
                if use_abbrev
                else "采用完整清晰的标准英文单词，不缩写核心名词"
            )
            prefix_rule = (
                f"所有表名统一添加分层前缀 `{table_prefix.rstrip('_')}_`。"
                if (granularity == "table" and table_prefix)
                else ""
            )
            sys_prompt = (
                "你是资深企业级数据仓库建模专家。请将用户提供的中文元数据名称翻译为符合规范的数据库英文标识符。"
                f"\n粒度：{'数据库表名 (TABLE)' if granularity == 'table' else '数据库字段名 (COLUMN)'}。"
                f"\n命名风格：{naming_style}。{abbrev_rule}。{prefix_rule}"
                f"\n目标数据库方言：{target_dialect}。"
                "\n严格只输出 JSON 数组，不要输出任何额外解释文字。每个元素格式为："
                '{"cn":"原中文名","en":"英文标识符","type":"推荐SQL数据类型(若为表名则填TABLE)","note":"命中的核心词根简述(12字以内)"}'
            )
            user_prompt = "待翻译清单：\n" + "\n".join(f"- {t}" for t in terms)
            chat_res = self.ctx.llm_hub.chat_completion(
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                provider_id=prof.provider_id,
                model=eff_model,
                temperature=0.1,
            )
            parsed = extract_json_payload(chat_res["content"])
            if isinstance(parsed, dict) and isinstance(parsed.get("items"), list):
                parsed = parsed["items"]
            if not isinstance(parsed, list):
                raise ValueError("AI 模型返回的命名结果不是合法的 JSON 数组")

            by_cn: Dict[str, Dict[str, Any]] = {}
            for idx, entry in enumerate(parsed):
                if not isinstance(entry, dict):
                    continue
                cn_k = str(entry.get("cn") or (terms[idx] if idx < len(terms) else "")).strip()
                if cn_k:
                    by_cn[cn_k] = entry

            for idx, orig_term in enumerate(terms):
                matched = by_cn.get(orig_term) or (
                    parsed[idx] if idx < len(parsed) and isinstance(parsed[idx], dict) else None
                )
                if matched and matched.get("en"):
                    raw_en = str(matched.get("en") or "").strip()
                    if granularity == "table" and table_prefix:
                        pfx = clean_identifier_input(table_prefix).lower().rstrip("_")
                        if pfx and not raw_en.lower().startswith(f"{pfx}_"):
                            raw_en = f"{pfx}_{raw_en}"
                    norm_en = _format_identifier_case(raw_en, naming_style)
                    rec_t = str(matched.get("type") or "").strip().upper()
                    if not rec_t:
                        rec_t = (
                            "TABLE"
                            if granularity == "table"
                            else _recommend_sql_type(norm_en, orig_term, target_dialect)
                        )
                    results.append(
                        {
                            "cn": orig_term,
                            "en": norm_en,
                            "type": rec_t,
                            "note": str(matched.get("note") or "AI 语义词根").strip(),
                        }
                    )
                else:
                    results.append(
                        _translate_single_term_locally(
                            orig_term,
                            granularity=granularity,
                            naming_style=naming_style,
                            table_prefix=table_prefix,
                            use_abbrev=use_abbrev,
                            target_dialect=target_dialect,
                        )
                    )

            ai_used = True
            elapsed_ms = float(chat_res.get("elapsed_ms") or 0.0)
            engine_label = f"{prof.name} · {eff_model}"
        else:
            if use_ai and resolved_llm is None:
                fallback_reason = "未配置 AI 模型密钥，已由内置数仓标准词根引擎即时生成"
            for orig_term in terms:
                results.append(
                    _translate_single_term_locally(
                        orig_term,
                        granularity=granularity,
                        naming_style=naming_style,
                        table_prefix=table_prefix,
                        use_abbrev=use_abbrev,
                        target_dialect=target_dialect,
                    )
                )

        ddl_preview = ""
        if granularity == "column" and results:
            draft_meta = TableMeta(
                schema_name="public",
                table_name=_format_identifier_case(
                    f"{table_prefix.rstrip('_') or 'dwd'}_auto_named_table", "snake_case"
                ),
                exists=True,
                table_comment="智能命名生成表草稿",
                columns=[
                    ColumnMeta(
                        ordinal_position=i,
                        column_name=r["en"],
                        data_type=r["type"] if r["type"] != "TABLE" else "VARCHAR(128)",
                        column_comment=r["cn"],
                    )
                    for i, r in enumerate(results, start=1)
                ],
                object_type="TABLE",
            )
            ddl_preview = build_dialect_ddl(
                draft_meta,
                source_dialect="DATA_STANDARD",
                target_dialect=target_dialect,
                target_schema="public",
                target_table=draft_meta.table_name,
            )["ddl"]

        self.ctx.store.add_log(
            "INFO",
            "多邻国命名",
            f"已完成 {len(results)} 项{'表名' if granularity == 'table' else '字段'}标准命名转换 ({engine_label})。",
        )

        return {
            "ok": True,
            "granularity": granularity,
            "naming_style": naming_style,
            "target_dialect": target_dialect,
            "ai_used": ai_used,
            "engine_label": engine_label,
            "fallback_reason": fallback_reason,
            "elapsed_ms": elapsed_ms,
            "items": results,
            "ddl_preview": ddl_preview,
        }

    # =========================================================================
    # 功能二：多邻国 · 跨方言建表 DDL / 视图穿透 / AI 异构 SQL 深度适配
    # =========================================================================

    def _refine_sql_with_llm(
        self,
        *,
        source_sql: str,
        draft_target_ddl: str,
        source_dialect: str,
        target_dialect: str,
        storage_mode: str,
        provider_id: str,
        model: str,
        is_freeform_sql: bool,
    ) -> Optional[Dict[str, Any]]:
        resolved = self.ctx.llm_hub.resolve_provider_and_model(
            provider_id=provider_id, model=model, require_ready=False
        )
        if resolved is None:
            return None
        prof, eff_model = resolved

        task_desc = (
            "将来源 SQL 语句（含异构函数、分析函数、日期处理、CTE、VIEW 或 DML/DDL 语法）精准转译为目标数据库原生可直接执行的等价 SQL。"
            if is_freeform_sql
            else "在保留确定性建表草稿全部字段物理顺序、字段名、注释与分布/分区特性的基础上，优化目标方言数据类型细节与特有建表子句；若源语句包含视图查询逻辑，可在建表 DDL 之后以注释或标准视图形式附上适配后的查询逻辑（如将 NVL/DECODE/SYSDATE/IFNULL 等异构函数转为目标库原生函数）。"
        )
        sys_prompt = (
            "你是精通 GaussDB(DWS)、PostgreSQL、星环 Inceptor、MySQL、达梦 DM8、Oracle、SQLite 的数据库内核方言转换专家。"
            f"\n来源方言：{source_dialect} -> 目标方言：{target_dialect}（存储模式：{storage_mode}）。"
            f"\n任务要求：{task_desc}"
            '\n请严格返回 JSON 对象：{"target_sql": "完整可执行的目标方言SQL", "notes": ["关键语法或函数适配点1", "适配点2"]}'
        )
        user_parts = [f"【来源 SQL ({source_dialect})】:\n{source_sql}"]
        if draft_target_ddl:
            user_parts.append(f"【AST 引擎生成的目标方言基准草稿 ({target_dialect})】:\n{draft_target_ddl}")

        chat_res = self.ctx.llm_hub.chat_completion(
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": "\n\n".join(user_parts)},
            ],
            provider_id=prof.provider_id,
            model=eff_model,
            temperature=0.1,
        )
        raw_content = chat_res["content"]
        target_sql_out = ""
        notes_out: List[str] = []
        try:
            parsed = extract_json_payload(raw_content)
            if isinstance(parsed, dict):
                target_sql_out = str(parsed.get("target_sql") or parsed.get("sql") or "").strip()
                if isinstance(parsed.get("notes"), list):
                    notes_out = [str(n).strip() for n in parsed["notes"] if str(n).strip()]
        except Exception:
            fence = re.search(r"```(?:sql)?\s*([\s\S]*?)```", raw_content, flags=re.IGNORECASE)
            target_sql_out = fence.group(1).strip() if fence else raw_content.strip()

        if not target_sql_out:
            return None
        return {
            "target_ddl": target_sql_out,
            "ai_model": f"{prof.name} · {eff_model}",
            "ai_notes": notes_out,
            "elapsed_ms": chat_res.get("elapsed_ms", 0.0),
        }

    def translate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        eff_mode = (
            str(payload.get("source_role") or payload.get("source_mode") or "connection")
            .strip()
            .lower()
        )
        source_schema = clean_identifier_input(str(payload.get("source_schema") or ""))
        source_table = clean_identifier_input(str(payload.get("source_table") or ""))
        source_dialect_hint = str(payload.get("source_dialect_hint") or "").strip().upper()
        target_dialect = str(payload.get("target_dialect") or "DWS").strip().upper()
        storage_mode = str(payload.get("storage_mode") or "AUTO").strip().upper()
        target_schema = clean_identifier_input(str(payload.get("target_schema") or ""))
        target_table = clean_identifier_input(str(payload.get("target_table") or ""))
        apply_std = bool(payload.get("apply_standard_mapping", False))
        std_table_key = str(payload.get("std_table_key") or "").strip()
        sql_text = str(payload.get("sql_text") or "").strip()
        view_sql = str(payload.get("view_sql") or sql_text or "").strip()
        force_refresh = bool(payload.get("force_refresh", False))
        ai_assist = bool(payload.get("ai_assist", False))
        provider_id = str(payload.get("provider_id") or "").strip()
        model = str(payload.get("model") or "").strip()

        from_live_db = False
        source_dialect = source_dialect_hint or "SQL"
        source_ddl = ""
        meta: Optional[TableMeta] = None

        # 模式 A：粘贴任意 SQL / 视图定义 / 建表语句 (view_sql 或 sql_text)
        if eff_mode in ("view_sql", "sql_text") or (view_sql and not source_table and eff_mode != "standard"):
            if not view_sql:
                raise ValueError("请粘贴有效的 SQL、CREATE TABLE 或 CREATE VIEW 语句")

            parsed_ct = _parse_simple_create_table_sql(view_sql, default_schema=source_schema or "public")
            parsed_v = (
                None
                if parsed_ct is not None
                else parse_view_sql_lineage(view_sql, default_schema=source_schema or "public")
            )

            if parsed_ct is not None:
                meta = parsed_ct
                source_dialect = source_dialect_hint or "DDL_SQL"
                source_ddl = format_readable_sql_ddl(view_sql)
            elif parsed_v and parsed_v.get("is_view_sql") and parsed_v.get("columns"):
                v_sch = parsed_v.get("view_schema") or source_schema or "public"
                v_tbl = parsed_v.get("view_name") or source_table or target_table or "v_translated_table"
                meta = TableMeta(
                    schema_name=v_sch,
                    table_name=v_tbl,
                    exists=True,
                    table_comment=parsed_v.get("view_comment") or "",
                    columns=[
                        ColumnMeta(
                            ordinal_position=c["ordinal_position"],
                            column_name=c["column_name"],
                            data_type=c["inferred_type"],
                            column_comment=c.get("inline_comment", ""),
                        )
                        for c in parsed_v["columns"]
                    ],
                    raw_ddl=view_sql,
                    object_type="VIEW",
                    view_definition=view_sql,
                )

                probe_cid = (
                    str(payload.get("probe_connection_id") or payload.get("connection_id") or "").strip()
                    or str(self.ctx.store.get_meta("cross_source_connection_id", "") or "")
                )
                probe_fn = None
                if probe_cid and self.ctx.connection_hub.get_profile(probe_cid):
                    prof = self.ctx.connection_hub.get_profile(probe_cid)
                    source_dialect = source_dialect_hint or (prof.db_type if prof else "VIEW_SQL")
                    from_live_db = True

                    def _fetch_live_obj(up_s: str, up_t: str) -> Optional[TableMeta]:
                        return self.ctx.metadata_hub.get_table_metadata(
                            probe_cid, up_s, up_t, force_refresh=False, penetrate_views=True
                        )

                    probe_fn = _fetch_live_obj
                else:
                    source_dialect = source_dialect_hint or "VIEW_SQL"

                meta = enrich_view_columns_by_lineage(
                    meta, fetch_object_fn=probe_fn, max_depth=4
                )
                source_ddl = format_readable_sql_ddl(view_sql)
            elif ai_assist:
                # 自由异构 SQL（如存储过程片段、复杂 MERGE/DML），直接交由大模型转换
                ai_only = self._refine_sql_with_llm(
                    source_sql=view_sql,
                    draft_target_ddl="",
                    source_dialect=source_dialect_hint or "SQL",
                    target_dialect=target_dialect,
                    storage_mode=storage_mode,
                    provider_id=provider_id,
                    model=model,
                    is_freeform_sql=True,
                )
                if ai_only is None:
                    raise ValueError(
                        "当前 SQL 不是标准建表或视图语句，需开启并配置「系统配置 → AI 模型」API Key 后进行智能方言转译"
                    )
                self.ctx.store.add_log(
                    "INFO",
                    "多邻国方言翻译",
                    f"已通过 AI ({ai_only['ai_model']}) 完成自由 SQL ({source_dialect} -> {target_dialect}) 转译。",
                    sql_text=ai_only["target_ddl"][:2000],
                )
                return {
                    "ok": True,
                    "source_dialect": source_dialect,
                    "source_schema": source_schema or "public",
                    "source_table": source_table or "custom_sql",
                    "source_object_type": "SQL",
                    "penetrated_base_tables": [],
                    "from_live_db": False,
                    "source_ddl": view_sql,
                    "target_dialect": target_dialect,
                    "target_schema": target_schema or source_schema or "public",
                    "target_table": target_table or source_table or "custom_sql",
                    "storage_mode": storage_mode,
                    "column_count": 0,
                    "target_ddl": ai_only["target_ddl"],
                    "column_mappings": [],
                    "ai_used": True,
                    "engine_mode": "AI",
                    "ai_model": ai_only["ai_model"],
                    "ai_notes": ai_only["ai_notes"],
                    "ai_warning": "",
                }
            else:
                raise ValueError("未能从输入的 SQL 中解析出有效的建表列或 SELECT 投影列，请检查语法或开启「AI 语法适配」")

        # 模式 B：基于数据标准清单直接生成任意方言建表 DDL
        elif eff_mode == "standard":
            meta = self._build_meta_from_standard(
                std_table_key=std_table_key or source_table,
                fallback_schema=source_schema or "public",
                fallback_table=source_table or std_table_key,
            )
            source_dialect = "DATA_STANDARD"
            src_built = build_dialect_ddl(
                meta,
                source_dialect="DATA_STANDARD",
                target_dialect="PGSQL",
                target_schema=meta.schema_name,
                target_table=meta.table_name,
            )
            source_ddl = src_built["ddl"]

        # 模式 C：从任意命名数据源连接读取表或视图
        else:
            conn_id = self._resolve_connection_id(
                eff_mode, str(payload.get("connection_id") or "")
            )
            if not conn_id:
                raise ValueError("请先选择有效的来源数据源连接，或切换为「粘贴 SQL」/「数据标准」模式")
            profile = self.ctx.connection_hub.require_profile(conn_id)
            if not source_table:
                raise ValueError("请输入或选择要读取转换的来源表名/视图名")
            meta = self.ctx.metadata_hub.get_table_metadata(
                conn_id,
                schema_name=source_schema or profile.default_schema,
                table_name=source_table,
                force_refresh=force_refresh,
                penetrate_views=True,
            )
            if not meta.exists or not meta.columns:
                raise ValueError(
                    f"在数据源 [{profile.name}] 中未找到表或视图 [{source_schema or profile.default_schema}.{source_table}]"
                )
            from_live_db = True
            source_dialect = profile.db_type
            if meta.raw_ddl:
                source_ddl = format_readable_sql_ddl(meta.raw_ddl)
            else:
                reconstructed = build_dialect_ddl(
                    meta,
                    source_dialect=profile.db_type,
                    target_dialect=profile.db_type,
                    target_schema=meta.schema_name,
                    target_table=meta.table_name,
                )
                source_ddl = reconstructed["ddl"]

        assert meta is not None
        if apply_std:
            meta = self._apply_standard_overlay(
                meta, std_table_key=std_table_key or meta.table_name, mode=eff_mode
            )

        eff_target_table = target_table or meta.table_name
        if meta.object_type in ("VIEW", "MATVIEW") and not target_table:
            eff_target_table = meta.table_name

        built = build_dialect_ddl(
            meta,
            source_dialect=source_dialect,
            target_dialect=target_dialect,
            target_schema=target_schema or meta.schema_name,
            target_table=eff_target_table,
            storage_mode=storage_mode,
        )

        final_ddl = built["ddl"]
        ai_used = False
        engine_mode = "AST"
        ai_model = ""
        ai_notes: List[str] = []
        ai_warning = ""

        if ai_assist:
            refined = self._refine_sql_with_llm(
                source_sql=meta.view_definition or source_ddl,
                draft_target_ddl=built["ddl"],
                source_dialect=source_dialect,
                target_dialect=built["target_dialect"],
                storage_mode=built["storage_mode"],
                provider_id=provider_id,
                model=model,
                is_freeform_sql=False,
            )
            if refined is not None:
                final_ddl = refined["target_ddl"]
                ai_used = True
                engine_mode = "AST+AI"
                ai_model = refined["ai_model"]
                ai_notes = refined["ai_notes"]
            else:
                ai_warning = "未配置 AI 模型密钥，已使用确定性 AST 方言引擎输出建表语句"

        self.ctx.store.add_log(
            "INFO",
            "多邻国方言翻译",
            f"已完成 [{meta.schema_name}.{meta.table_name}] ({source_dialect} -> {built['target_dialect']}, {engine_mode}) 转换，共 {built['column_count']} 列。",
            sql_text=final_ddl[:2000],
        )

        return {
            "ok": True,
            "source_dialect": source_dialect,
            "source_schema": meta.schema_name,
            "source_table": meta.table_name,
            "source_object_type": meta.object_type or "TABLE",
            "penetrated_base_tables": list(meta.penetrated_base_tables or []),
            "from_live_db": from_live_db,
            "source_ddl": source_ddl,
            "target_dialect": built["target_dialect"],
            "target_schema": built["target_schema"],
            "target_table": built["target_table"],
            "storage_mode": built["storage_mode"],
            "column_count": built["column_count"],
            "target_ddl": final_ddl,
            "ast_ddl": built["ddl"],
            "column_mappings": built["column_mappings"],
            "ai_used": ai_used,
            "engine_mode": engine_mode,
            "ai_model": ai_model,
            "ai_notes": ai_notes,
            "ai_warning": ai_warning,
        }
