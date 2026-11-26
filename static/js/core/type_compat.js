/**
 * Data.O.G 跨库字段物理类型解析与双维兼容性前端实时计算器
 * 与后端 `core/platform/dialects/types.py` (`analyze_cross_column_compatibility`) 保持规则一致，
 * 用于「3. 跨环境比对」拖拽配对未保存前即时反馈「目标长度/精度包络」与「是否涉及类型转换」。
 */

const MYSQL_FAMILY_DIALECTS = new Set([
  "MYSQL",
  "MARIADB",
  "OCEANBASE",
  "DORIS",
  "STARROCKS",
  "TIDB",
  "GOLDENDB",
]);

const INT_SPEC_TABLE = {
  tinyint: [8, 2, 3],
  int1: [8, 2, 3],
  byteint: [8, 2, 3],
  smallint: [16, 4, 5],
  int2: [16, 4, 5],
  mediumint: [24, 6, 7],
  int: [32, 9, 10],
  integer: [32, 9, 10],
  int4: [32, 9, 10],
  serial: [32, 9, 10],
  binary_integer: [32, 9, 10],
  pls_integer: [32, 9, 10],
  bigint: [64, 18, 19],
  int8: [64, 18, 19],
  bigserial: [64, 18, 19],
  largeint: [128, 38, 39],
  hugeint: [128, 38, 39],
  int16: [128, 38, 39],
};

function cleanSqlTypeInput(rawType) {
  return String(rawType || "")
    .replace(/[\u200b\u3000]/g, " ")
    .replace(/（/g, "(")
    .replace(/）/g, ")")
    .replace(/，/g, ",")
    .trim()
    .replace(/\s+/g, " ");
}

function parseSqlTypeSpec(rawType, dbType = "") {
  const cleaned = cleanSqlTypeInput(rawType);
  const low = cleaned.toLowerCase();
  if (!low || low === "unknown" || low === "any" || low === "void") {
    return {
      empty: true,
      raw: cleaned,
      base: "",
      family: "UNKNOWN",
      sub: "",
      family_cn: "未知",
      unbounded: false,
      unsigned: false,
      args: [],
      unit: "",
    };
  }

  const unsigned = /\bunsigned\b/.test(low);
  let unit = "";
  const args = [];
  const mParen = low.match(/\(([^)]*)\)/);
  if (mParen) {
    const inner = mParen[1].trim();
    if (/\bbyte\b/.test(inner)) unit = "BYTE";
    else if (/\bchar\b/.test(inner)) unit = "CHAR";
    for (const part of inner.split(",")) {
      const tok = part.replace(/\b(?:byte|char)\b/g, "").trim();
      if (tok === "*") args.push(38);
      else if (/^[+-]?\d+$/.test(tok)) args.push(parseInt(tok, 10));
    }
  }

  let base = low
    .replace(/\([^)]*\)/g, "")
    .replace(/\b(?:unsigned|signed|zerofill)\b/g, "")
    .replace(/\s+/g, " ")
    .trim();
  if (!unit) {
    if (/\bbyte\b/.test(base)) {
      unit = "BYTE";
      base = base.replace(/\bbyte\b/g, "").trim();
    } else if (/\bchar\b/.test(base) && !["char", "nchar", "bpchar"].includes(base)) {
      unit = "CHAR";
      base = base.replace(/\bchar\b/g, "").trim();
    }
  }

  const arg1 = args.length >= 1 ? args[0] : null;
  const arg2 = args.length >= 2 ? args[1] : null;
  const dt = String(dbType || "").trim().toUpperCase();

  const spec = {
    empty: false,
    raw: cleaned,
    base,
    family: "UNKNOWN",
    sub: "",
    family_cn: "未知",
    unbounded: false,
    unsigned,
    args,
    unit,
    char_len: null,
    byte_len: null,
    bits: null,
    precision: null,
    scale: null,
    int_digits: null,
    safe_dec_digits: null,
    max_dec_digits: null,
    frac: null,
  };

  if (["varchar", "varchar2", "nvarchar", "nvarchar2", "character varying"].includes(base)) {
    spec.family = "STRING";
    spec.sub = "VARCHAR";
    spec.family_cn = "字符";
    if (arg1 !== null) {
      spec.char_len = Math.max(0, arg1);
      spec.unbounded = false;
    } else {
      spec.unbounded = true;
    }
    return spec;
  }
  if (["char", "nchar", "character", "bpchar"].includes(base)) {
    spec.family = "STRING";
    spec.sub = "CHAR";
    spec.family_cn = "字符";
    spec.char_len = arg1 !== null ? Math.max(0, arg1) : 1;
    spec.unbounded = false;
    return spec;
  }
  if (base === "tinytext") {
    spec.family = "STRING";
    spec.sub = "TEXT";
    spec.family_cn = "字符";
    spec.char_len = 255;
    spec.unbounded = false;
    return spec;
  }
  if (["text", "mediumtext", "longtext", "string", "clob", "nclob", "long", "longvarchar", "ntext"].includes(base)) {
    spec.family = "STRING";
    spec.sub = "TEXT";
    spec.family_cn = "字符";
    spec.unbounded = true;
    return spec;
  }

  if (base === "bool" || base === "boolean" || (base === "bit" && (arg1 === null || arg1 === 1))) {
    spec.family = "BOOL";
    spec.sub = "BOOL";
    spec.family_cn = "布尔";
    spec.bits = 1;
    return spec;
  }

  if (base in INT_SPEC_TABLE) {
    const [bits, safeD, maxD] = INT_SPEC_TABLE[base];
    spec.family = "NUMERIC";
    spec.sub = "INT";
    spec.family_cn = "整型";
    spec.bits = bits;
    spec.safe_dec_digits = safeD;
    spec.max_dec_digits = unsigned && ["mediumint", "bigint", "int8", "bigserial"].includes(base) ? maxD + 1 : maxD;
    return spec;
  }

  if (["decimal", "numeric", "number", "dec", "fixed"].includes(base)) {
    spec.family = "NUMERIC";
    spec.sub = "DECIMAL";
    spec.family_cn = "定点数值";
    if (arg1 !== null) {
      let p = Math.max(1, arg1);
      const rawS = arg2 !== null ? arg2 : 0;
      let intD;
      let sc;
      if (rawS < 0) {
        intD = p + Math.abs(rawS);
        sc = 0;
        p = intD;
      } else {
        sc = rawS;
        intD = Math.max(0, p - sc);
      }
      spec.precision = p;
      spec.scale = sc;
      spec.int_digits = intD;
      spec.unbounded = false;
    } else if (MYSQL_FAMILY_DIALECTS.has(dt)) {
      spec.precision = 10;
      spec.scale = 0;
      spec.int_digits = 10;
      spec.unbounded = false;
    } else {
      spec.unbounded = true;
    }
    return spec;
  }

  if (base === "money" || base === "smallmoney") {
    const p = base === "money" ? 19 : 10;
    spec.family = "NUMERIC";
    spec.sub = "DECIMAL";
    spec.family_cn = "定点数值";
    spec.precision = p;
    spec.scale = 4;
    spec.int_digits = p - 4;
    spec.unbounded = false;
    return spec;
  }

  if (["real", "float4", "binary_float"].includes(base)) {
    spec.family = "NUMERIC";
    spec.sub = "FLOAT";
    spec.family_cn = "浮点数值";
    spec.bits = 32;
    return spec;
  }
  if (["double", "double precision", "float8", "binary_double"].includes(base)) {
    spec.family = "NUMERIC";
    spec.sub = "FLOAT";
    spec.family_cn = "浮点数值";
    spec.bits = 64;
    return spec;
  }
  if (base === "float") {
    spec.family = "NUMERIC";
    spec.sub = "FLOAT";
    spec.family_cn = "浮点数值";
    spec.bits = arg1 !== null && arg1 > 24 ? 64 : 32;
    return spec;
  }

  if (base === "date") {
    spec.family = "TEMPORAL";
    spec.sub = "DATE";
    spec.family_cn = "日期";
    return spec;
  }
  if (["time", "timetz", "time without time zone", "time with time zone"].includes(base)) {
    spec.family = "TEMPORAL";
    spec.sub = "TIME";
    spec.family_cn = "时间";
    spec.frac = arg1;
    return spec;
  }
  if (
    [
      "timestamp",
      "timestamptz",
      "timestamp without time zone",
      "timestamp with time zone",
      "datetime",
      "smalldatetime",
    ].includes(base)
  ) {
    spec.family = "TEMPORAL";
    spec.sub = "TIMESTAMP";
    spec.family_cn = "时间戳";
    spec.frac = base === "smalldatetime" ? 0 : arg1;
    return spec;
  }
  if (base === "year") {
    spec.family = "TEMPORAL";
    spec.sub = "YEAR";
    spec.family_cn = "年份";
    return spec;
  }

  if (["binary", "varbinary", "raw"].includes(base)) {
    spec.family = "BINARY";
    spec.sub = "VARBINARY";
    spec.family_cn = "二进制";
    if (arg1 !== null) {
      spec.byte_len = Math.max(0, arg1);
      spec.unbounded = false;
    } else {
      spec.unbounded = true;
    }
    return spec;
  }
  if (base === "bit" && arg1 !== null && arg1 > 1) {
    spec.family = "BINARY";
    spec.sub = "VARBINARY";
    spec.family_cn = "二进制";
    spec.byte_len = Math.floor((arg1 + 7) / 8);
    spec.unbounded = false;
    return spec;
  }
  if (base === "tinyblob") {
    spec.family = "BINARY";
    spec.sub = "BLOB";
    spec.family_cn = "二进制";
    spec.byte_len = 255;
    spec.unbounded = false;
    return spec;
  }
  if (["blob", "mediumblob", "longblob", "bytea"].includes(base)) {
    spec.family = "BINARY";
    spec.sub = "BLOB";
    spec.family_cn = "二进制";
    spec.unbounded = true;
    return spec;
  }

  if (base === "json" || base === "jsonb") {
    spec.family = "JSON";
    spec.sub = "JSON";
    spec.family_cn = "JSON";
    spec.unbounded = true;
    return spec;
  }

  return spec;
}

function compatResult({
  applicable,
  capStatus,
  capLabel,
  capShort,
  capDetail,
  convStatus,
  convLabel,
  convShort,
  convDetail,
}) {
  const capOk = capStatus === "CONTAINED" ? true : capStatus === "INSUFFICIENT" ? false : null;
  return {
    applicable,
    capacity_status: capStatus,
    capacity_ok: capOk,
    capacity_label: capLabel,
    capacity_short: capShort,
    capacity_detail: capDetail,
    has_type_conversion: convStatus === "SAFE_CAST" || convStatus === "CAST_REQUIRED",
    type_conv_status: convStatus,
    type_conv_label: convLabel,
    type_conv_short: convShort,
    type_conv_detail: convDetail,
  };
}

function stringCapacity(src, tgt) {
  if (tgt.unbounded) {
    return ["CONTAINED", "可包住", "可包住", `目标类型 ${tgt.raw} 无固定位长限制，可包住来源 ${src.raw}`];
  }
  const tLen = Number(tgt.char_len || 0);
  if (src.unbounded) {
    return [
      "INSUFFICIENT",
      `无法包住 (无界→${tLen})`,
      `位长不足 无界→${tLen}`,
      `来源无界字符 (${src.raw}) 转目标定长 (${tgt.raw}) 可能截断`,
    ];
  }
  const sLen = Number(src.char_len || 0);
  if (tLen < sLen) {
    return [
      "INSUFFICIENT",
      `无法包住 (位长 ${sLen}→${tLen})`,
      `位长不足 ${sLen}→${tLen}`,
      `目标位长 ${tLen} 小于来源位长 ${sLen}`,
    ];
  }
  if (src.unit === "CHAR" && tgt.unit === "BYTE" && tLen < sLen * 3) {
    return [
      "RISK",
      `需核验 (${sLen} CHAR→${tLen} BYTE)`,
      "需核验 CHAR→BYTE",
      `来源为字符长度 ${sLen} CHAR，目标为字节长度 ${tLen} BYTE`,
    ];
  }
  return [
    "CONTAINED",
    "可包住",
    "可包住",
    tLen === sLen ? `双侧位长一致 (${sLen})` : `目标位长 ${tLen} 大于来源 ${sLen}`,
  ];
}

function decimalCapacity(src, tgt) {
  if (tgt.unbounded) {
    return ["CONTAINED", "可包住", "可包住", `目标 ${tgt.raw} 为无界定点数值，可包住来源 ${src.raw}`];
  }
  if (src.unbounded) {
    return [
      "INSUFFICIENT",
      `无法包住 (无界→${tgt.precision},${tgt.scale})`,
      "位长/精度不足",
      `来源 ${src.raw} 未限定精度，转入定长 ${tgt.raw} 存在溢出或截断风险`,
    ];
  }
  const sI = Number(src.int_digits || 0);
  const sS = Number(src.scale || 0);
  const tI = Number(tgt.int_digits || 0);
  const tS = Number(tgt.scale || 0);
  const intShort = tI < sI;
  const scaleShort = tS < sS;
  if (intShort && scaleShort) {
    return [
      "INSUFFICIENT",
      `无法包住 (整数位 ${sI}→${tI}位, 小数 ${sS}→${tS}位)`,
      "位长/精度不足",
      `目标整数位 (${tI}位) 与小数精度 (${tS}位) 均小于来源 (${sI}位, ${sS}位)`,
    ];
  }
  if (intShort) {
    return [
      "INSUFFICIENT",
      `无法包住 (整数位 ${sI}→${tI}位)`,
      `整数位不足 ${sI}→${tI}`,
      `目标整数位 ${tI} 位小于来源 ${sI} 位`,
    ];
  }
  if (scaleShort) {
    return [
      "INSUFFICIENT",
      `无法包住 (小数精度 ${sS}→${tS}位)`,
      `精度不足 ${sS}→${tS}`,
      `目标小数精度 ${tS} 位小于来源 ${sS} 位，存在截断`,
    ];
  }
  return ["CONTAINED", "可包住", "可包住", `目标整数位 ${tI} ≥ ${sI} 且小数位 ${tS} ≥ ${sS}`];
}

export function analyzeCrossColumnCompat(
  sourceType,
  targetType,
  sourceDbType = "",
  targetDbType = ""
) {
  const src = parseSqlTypeSpec(sourceType, sourceDbType);
  const tgt = parseSqlTypeSpec(targetType, targetDbType);
  if (src.empty || tgt.empty) {
    return compatResult({
      applicable: false,
      capStatus: "N/A",
      capLabel: "-",
      capShort: "-",
      capDetail: "",
      convStatus: "N/A",
      convLabel: "-",
      convShort: "-",
      convDetail: "",
    });
  }

  const sf = src.family;
  const tf = tgt.family;
  const ss = src.sub;
  const ts = tgt.sub;

  if (sf === "UNKNOWN" || tf === "UNKNOWN") {
    if (src.base === tgt.base && JSON.stringify(src.args) === JSON.stringify(tgt.args)) {
      return compatResult({
        applicable: true,
        capStatus: "CONTAINED",
        capLabel: "可包住",
        capShort: "可包住",
        capDetail: `类型定义一致 (${src.raw})`,
        convStatus: "NONE",
        convLabel: "否",
        convShort: "无转换",
        convDetail: "同类型无需转换",
      });
    }
    if (src.base === tgt.base) {
      return compatResult({
        applicable: true,
        capStatus: "RISK",
        capLabel: "需核验 (参数差异)",
        capShort: "需核验",
        capDetail: `${src.raw} → ${tgt.raw}`,
        convStatus: "NONE",
        convLabel: "否",
        convShort: "无转换",
        convDetail: "同类型参数差异，无需类型转换",
      });
    }
    return compatResult({
      applicable: true,
      capStatus: "RISK",
      capLabel: `需核验 (${src.raw}→${tgt.raw})`,
      capShort: "需核验",
      capDetail: `异构类型映射: ${src.raw} → ${tgt.raw}`,
      convStatus: "CAST_REQUIRED",
      convLabel: `是 (${src.raw}→${tgt.raw})`,
      convShort: `转: ${src.raw}→${tgt.raw}`,
      convDetail: `需要从 ${src.raw} 转换为 ${tgt.raw}`,
    });
  }

  if (sf === "STRING" && tf === "STRING") {
    const [cs, cl, csh, cd] = stringCapacity(src, tgt);
    return compatResult({
      applicable: true,
      capStatus: cs,
      capLabel: cl,
      capShort: csh,
      capDetail: cd,
      convStatus: "NONE",
      convLabel: "否",
      convShort: "无转换",
      convDetail: "同属字符类型，无需类型转换",
    });
  }

  if (sf === "NUMERIC" && tf === "NUMERIC" && ss === "DECIMAL" && ts === "DECIMAL") {
    const [cs, cl, csh, cd] = decimalCapacity(src, tgt);
    return compatResult({
      applicable: true,
      capStatus: cs,
      capLabel: cl,
      capShort: csh,
      capDetail: cd,
      convStatus: "NONE",
      convLabel: "否",
      convShort: "无转换",
      convDetail: "同属定点数值类型，无需类型转换",
    });
  }

  if (sf === "NUMERIC" && tf === "NUMERIC" && ss === "INT" && ts === "INT") {
    const sBits = Number(src.bits || 32);
    const tBits = Number(tgt.bits || 32);
    let cs;
    let cl;
    let csh;
    let cd;
    if (tBits > sBits) {
      [cs, cl, csh, cd] = ["CONTAINED", "可包住", "可包住", `目标 ${tBits} 位整型可包住来源 ${sBits} 位整型`];
    } else if (tBits === sBits) {
      if (src.unsigned && !tgt.unsigned) {
        [cs, cl, csh, cd] = [
          "INSUFFICIENT",
          `无法包住 (无符号${sBits}位→有符号${tBits}位)`,
          "无符号溢出风险",
          `无符号 ${sBits} 位上限超出有符号 ${tBits} 位整型正数上限`,
        ];
      } else {
        [cs, cl, csh, cd] = ["CONTAINED", "可包住", "可包住", `同为 ${tBits} 位整型，容量一致`];
      }
    } else {
      [cs, cl, csh, cd] = [
        "INSUFFICIENT",
        `无法包住 (${sBits}位→${tBits}位整型)`,
        `位宽不足 ${sBits}→${tBits}位`,
        `目标 ${tBits} 位整型无法包住来源 ${sBits} 位整型`,
      ];
    }
    return compatResult({
      applicable: true,
      capStatus: cs,
      capLabel: cl,
      capShort: csh,
      capDetail: cd,
      convStatus: "NONE",
      convLabel: "否",
      convShort: "无转换",
      convDetail: "同属整型（已忽略显示宽度差异），无需类型转换",
    });
  }

  if (sf === "NUMERIC" && tf === "NUMERIC" && ss === "INT" && ts === "DECIMAL") {
    const sMax = Number(src.max_dec_digits || 10);
    const tI = Number(tgt.int_digits || 0);
    const ok = tgt.unbounded || tI >= sMax;
    return compatResult({
      applicable: true,
      capStatus: ok ? "CONTAINED" : "INSUFFICIENT",
      capLabel: ok ? "可包住" : `无法包住 (${src.bits}位整型→整数位${tI}位)`,
      capShort: ok ? "可包住" : `整数位不足 ${sMax}→${tI}`,
      capDetail: ok ? `目标定点整数位可完全容纳 ${src.bits} 位整型` : `来源最大需 ${sMax} 位整数，目标仅 ${tI} 位`,
      convStatus: "SAFE_CAST",
      convLabel: "是 (兼容: 整型→定点)",
      convShort: "兼容: 整型→定点",
      convDetail: "整型升格为定点数值，属于同域兼容转换",
    });
  }

  if (sf === "NUMERIC" && tf === "NUMERIC" && ss === "DECIMAL" && ts === "INT") {
    if (src.unbounded || Number(src.scale || 0) > 0) {
      return compatResult({
        applicable: true,
        capStatus: "INSUFFICIENT",
        capLabel: "无法包住 (小数精度丢失)",
        capShort: "小数精度丢失",
        capDetail: src.unbounded ? "来源无界定点数值可能带小数" : `来源含 ${src.scale} 位小数，转整型将丢失小数精度`,
        convStatus: "CAST_REQUIRED",
        convLabel: "是 (定点→整型)",
        convShort: "转: 定点→整型",
        convDetail: "定点数值转整型将截断小数，涉及显式类型转换",
      });
    }
    const sI = Number(src.int_digits || 0);
    const tSafe = Number(tgt.safe_dec_digits || 9);
    const tMax = Number(tgt.max_dec_digits || 10);
    let cs;
    let cl;
    let csh;
    let cd;
    if (sI <= tSafe) {
      [cs, cl, csh, cd] = ["CONTAINED", "可包住", "可包住", `来源 ${sI} 位零小数定点可安全落入 ${tgt.bits} 位整型`];
    } else if (sI === tMax) {
      [cs, cl, csh, cd] = ["RISK", `需核验 (${sI}位→${tgt.bits}位整型极值)`, "极值待核验", `可能超过 ${tgt.bits} 位整型极值`];
    } else {
      [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (整数位 ${sI}→${tgt.bits}位整型)`, `位宽不足 ${sI}位`, `超出目标整型最大容限`];
    }
    return compatResult({
      applicable: true,
      capStatus: cs,
      capLabel: cl,
      capShort: csh,
      capDetail: cd,
      convStatus: "SAFE_CAST",
      convLabel: "是 (兼容: 零小数→整型)",
      convShort: "兼容: 零小数→整型",
      convDetail: "零小数定点数值与整型属于同域兼容转换",
    });
  }

  if (sf === "NUMERIC" && tf === "NUMERIC" && (ss === "FLOAT" || ts === "FLOAT")) {
    if (ss === "FLOAT" && ts === "FLOAT") {
      const sB = Number(src.bits || 32);
      const tB = Number(tgt.bits || 32);
      const ok = tB >= sB;
      return compatResult({
        applicable: true,
        capStatus: ok ? "CONTAINED" : "INSUFFICIENT",
        capLabel: ok ? "可包住" : `无法包住 (${sB}位→${tB}位浮点)`,
        capShort: ok ? "可包住" : "浮点精度不足",
        capDetail: ok ? `目标 ${tB} 位浮点可包住来源 ${sB} 位浮点` : `双精度浮点降至 ${tB} 位存在精度损失`,
        convStatus: "NONE",
        convLabel: "否",
        convShort: "无转换",
        convDetail: "同属浮点类型，无需类型转换",
      });
    }
    if (ss === "INT" && ts === "FLOAT") {
      const sB = Number(src.bits || 32);
      const tB = Number(tgt.bits || 32);
      const ok = (sB <= 16 && tB >= 32) || (sB <= 32 && tB >= 64);
      return compatResult({
        applicable: true,
        capStatus: ok ? "CONTAINED" : "RISK",
        capLabel: ok ? "可包住" : `需核验 (${sB}位整型→${tB}位浮点精度)`,
        capShort: ok ? "可包住" : "浮点有效位风险",
        capDetail: ok ? `${tB} 位浮点尾数可精确表示 ${sB} 位整型` : `${sB} 位大整数转 ${tB} 位浮点可能发生有效位舍入`,
        convStatus: "SAFE_CAST",
        convLabel: "是 (兼容: 整型→浮点)",
        convShort: "兼容: 整型→浮点",
        convDetail: "整型转浮点属于数值域转换",
      });
    }
    if (ss === "DECIMAL" && ts === "FLOAT") {
      const tB = Number(tgt.bits || 32);
      const maxSig = tB >= 64 ? 15 : 7;
      const ins = src.unbounded || Number(src.precision || 0) > maxSig;
      return compatResult({
        applicable: true,
        capStatus: ins ? "INSUFFICIENT" : "RISK",
        capLabel: ins ? `无法包住 (定点精确位>${maxSig}→${tB}位浮点)` : "需核验 (十进制→二进制浮点舍入)",
        capShort: ins ? "浮点有效位不足" : "浮点舍入风险",
        capDetail: ins ? `超过 ${tB} 位浮点有效十进制位上限 (${maxSig} 位)` : "定点十进制转二进制浮点可能产生微小尾数误差",
        convStatus: "CAST_REQUIRED",
        convLabel: "是 (定点→浮点)",
        convShort: "转: 定点→浮点",
        convDetail: "精确十进制转二进制浮点涉及类型转换",
      });
    }
    if (ss === "FLOAT" && ts === "INT") {
      return compatResult({
        applicable: true,
        capStatus: "INSUFFICIENT",
        capLabel: "无法包住 (浮点小数精度丢失)",
        capShort: "小数精度丢失",
        capDetail: "浮点类型转整型会截断小数部分且可能溢出",
        convStatus: "CAST_REQUIRED",
        convLabel: "是 (浮点→整型)",
        convShort: "转: 浮点→整型",
        convDetail: "需要从浮点数值显式转换为整型",
      });
    }
    if (ss === "FLOAT" && ts === "DECIMAL") {
      const ok = tgt.unbounded;
      const risk = !ok && Number(tgt.int_digits || 0) >= 15 && Number(tgt.scale || 0) >= 4;
      return compatResult({
        applicable: true,
        capStatus: ok ? "CONTAINED" : risk ? "RISK" : "INSUFFICIENT",
        capLabel: ok ? "可包住" : risk ? "需核验 (浮点→定点值域)" : `无法包住 (浮点→${tgt.raw})`,
        capShort: ok ? "可包住" : risk ? "浮点范围核验" : "定点范围可能不足",
        capDetail: ok ? `目标无界 ${tgt.raw} 可容纳浮点值` : `需核验浮点值域及小数位未超出 ${tgt.raw}`,
        convStatus: "CAST_REQUIRED",
        convLabel: "是 (浮点→定点)",
        convShort: "转: 浮点→定点",
        convDetail: "二进制浮点转定点十进制涉及类型转换",
      });
    }
  }

  if (sf === "TEMPORAL" && tf === "TEMPORAL") {
    if (ss === ts) {
      const shortFrac = src.frac !== null && tgt.frac !== null && tgt.frac < src.frac;
      return compatResult({
        applicable: true,
        capStatus: shortFrac ? "INSUFFICIENT" : "CONTAINED",
        capLabel: shortFrac ? `无法包住 (秒精度 ${src.frac}→${tgt.frac}位)` : "可包住",
        capShort: shortFrac ? `精度不足 ${src.frac}→${tgt.frac}` : "可包住",
        capDetail: shortFrac ? `目标秒小数精度 (${tgt.frac}) 小于来源 (${src.frac})` : `同属${src.family_cn}类型，容量可覆盖`,
        convStatus: "NONE",
        convLabel: "否",
        convShort: "无转换",
        convDetail: `同属${src.family_cn}类型，无需类型转换`,
      });
    }
    if (ss === "DATE" && ts === "TIMESTAMP") {
      return compatResult({
        applicable: true,
        capStatus: "CONTAINED",
        capLabel: "可包住",
        capShort: "可包住",
        capDetail: "TIMESTAMP 可完整包含 DATE 日期值",
        convStatus: "SAFE_CAST",
        convLabel: "是 (兼容: 日期→时间戳)",
        convShort: "兼容: 日期→时间戳",
        convDetail: "日期升格为时间戳，补零时分秒，属于兼容转换",
      });
    }
    if (ss === "TIMESTAMP" && ts === "DATE") {
      return compatResult({
        applicable: true,
        capStatus: "INSUFFICIENT",
        capLabel: "无法包住 (时分秒精度丢失)",
        capShort: "时分秒丢失",
        capDetail: "TIMESTAMP 降级为 DATE 将丢失时分秒及毫秒精度",
        convStatus: "CAST_REQUIRED",
        convLabel: "是 (时间戳→日期)",
        convShort: "转: 时间戳→日期",
        convDetail: "需要从时间戳显式转换为日期",
      });
    }
    return compatResult({
      applicable: true,
      capStatus: "INSUFFICIENT",
      capLabel: `无法包住 (${src.family_cn}→${tgt.family_cn})`,
      capShort: "时间分量丢失",
      capDetail: `${src.raw} 转为 ${tgt.raw} 无法完整保留原时间信息`,
      convStatus: "CAST_REQUIRED",
      convLabel: `是 (${src.family_cn}→${tgt.family_cn})`,
      convShort: `转: ${src.family_cn}→${tgt.family_cn}`,
      convDetail: `涉及时间子类型转换 (${src.raw} → ${tgt.raw})`,
    });
  }

  if (sf === "BOOL" && tf === "BOOL") {
    return compatResult({
      applicable: true,
      capStatus: "CONTAINED",
      capLabel: "可包住",
      capShort: "可包住",
      capDetail: "同属布尔类型",
      convStatus: "NONE",
      convLabel: "否",
      convShort: "无转换",
      convDetail: "同属布尔类型，无需类型转换",
    });
  }
  if (sf === "BOOL" && tf === "NUMERIC" && (ts === "INT" || ts === "DECIMAL")) {
    const badDec = ts === "DECIMAL" && !tgt.unbounded && Number(tgt.int_digits || 0) < 1;
    return compatResult({
      applicable: true,
      capStatus: badDec ? "INSUFFICIENT" : "CONTAINED",
      capLabel: badDec ? "无法包住 (整数位不足1位)" : "可包住",
      capShort: badDec ? "整数位不足" : "可包住",
      capDetail: badDec ? `目标 ${tgt.raw} 无整数位存储布尔标志 0/1` : `目标 ${tgt.raw} 可容纳布尔 0/1 标志`,
      convStatus: badDec ? "CAST_REQUIRED" : "SAFE_CAST",
      convLabel: badDec ? `是 (布尔→${tgt.family_cn})` : `是 (兼容: 布尔→${tgt.family_cn})`,
      convShort: badDec ? `转: 布尔→${tgt.family_cn}` : `兼容: 布尔→${tgt.family_cn}`,
      convDetail: "布尔值与数值 0/1 转换",
    });
  }
  if (sf === "NUMERIC" && tf === "BOOL") {
    const isOneBit =
      (ss === "INT" && src.args.length && src.args[0] === 1) ||
      (ss === "DECIMAL" && src.precision === 1 && src.scale === 0);
    if (isOneBit) {
      return compatResult({
        applicable: true,
        capStatus: "CONTAINED",
        capLabel: "可包住",
        capShort: "可包住",
        capDetail: `1 位数值标志 (${src.raw}) 可映射为布尔值`,
        convStatus: "SAFE_CAST",
        convLabel: "是 (兼容: 1位标志→布尔)",
        convShort: "兼容: 标志→布尔",
        convDetail: "1 位数值标志与布尔类型兼容转换",
      });
    }
    return compatResult({
      applicable: true,
      capStatus: "INSUFFICIENT",
      capLabel: "无法包住 (多值数值→布尔)",
      capShort: "值域缩窄",
      capDetail: `${src.raw} 可能包含非 0/1 数值，转布尔会缩窄值域`,
      convStatus: "CAST_REQUIRED",
      convLabel: `是 (${src.family_cn}→布尔)`,
      convShort: `转: ${src.family_cn}→布尔`,
      convDetail: `需要从 ${src.raw} 显式转换为布尔类型`,
    });
  }

  if (sf === "BINARY" && tf === "BINARY") {
    let cs = "CONTAINED";
    let cl = "可包住";
    let csh = "可包住";
    let cd = "目标无界二进制可包住来源";
    if (!tgt.unbounded) {
      const tB = Number(tgt.byte_len || 0);
      if (src.unbounded) {
        [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (无界→${tB}字节)`, `位长不足 无界→${tB}`, "来源无界二进制转定长可能截断"];
      } else {
        const sB = Number(src.byte_len || 0);
        if (tB >= sB) {
          cd = `目标 ${tB} 字节 ≥ 来源 ${sB} 字节`;
        } else {
          [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (位长 ${sB}→${tB}字节)`, `位长不足 ${sB}→${tB}`, `目标字节长 ${tB} 小于来源 ${sB}`];
        }
      }
    }
    return compatResult({
      applicable: true,
      capStatus: cs,
      capLabel: cl,
      capShort: csh,
      capDetail: cd,
      convStatus: "NONE",
      convLabel: "否",
      convShort: "无转换",
      convDetail: "同属二进制类型，无需类型转换",
    });
  }

  if (sf === "JSON" && tf === "JSON") {
    return compatResult({
      applicable: true,
      capStatus: "CONTAINED",
      capLabel: "可包住",
      capShort: "可包住",
      capDetail: "同属 JSON 类型",
      convStatus: "NONE",
      convLabel: "否",
      convShort: "无转换",
      convDetail: "同属 JSON 类型，无需类型转换",
    });
  }

  if (sf === "STRING") {
    const convLbl = `是 (字符→${tgt.family_cn})`;
    const convSht = `转: 字符→${tgt.family_cn}`;
    const convDtl = `来源为字符 (${src.raw})，目标为${tgt.family_cn} (${tgt.raw})，需要显式类型转换`;
    let cs = "RISK";
    let cl = `需核验 (字符→${tgt.family_cn}格式)`;
    let csh = "需核验格式";
    let cd = `需确保来源字符数据均符合目标 ${tgt.raw} 格式规范`;
    if (tf === "NUMERIC" && ts === "INT") {
      const tMax = Number(tgt.max_dec_digits || 10);
      if (src.unbounded || Number(src.char_len || 0) > tMax) {
        [cs, cl, csh, cd] = [
          "INSUFFICIENT",
          `无法包住 (字符位长${src.char_len || "无界"}超${tgt.bits}位整型上限)`,
          "字符转整型溢出风险",
          `来源字符位长可能超过 ${tgt.bits} 位整型最大数字位 (${tMax} 位)`,
        ];
      }
    } else if (tf === "NUMERIC" && ts === "DECIMAL" && !tgt.unbounded) {
      const maxFmtLen = Number(tgt.precision || 0) + (Number(tgt.scale || 0) > 0 ? 1 : 0) + 1;
      if (src.unbounded || Number(src.char_len || 0) > maxFmtLen) {
        [cs, cl, csh, cd] = [
          "INSUFFICIENT",
          `无法包住 (字符位长${src.char_len || "无界"}→${tgt.raw})`,
          "字符超定点精度",
          `来源字符位长超过目标 ${tgt.raw} 最大文本表示长度 (${maxFmtLen} 位)`,
        ];
      }
    } else if (tf === "BINARY" && !tgt.unbounded) {
      const tB = Number(tgt.byte_len || 0);
      if (src.unbounded || tB < Number(src.char_len || 0)) {
        [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (字符位长${src.char_len || "无界"}→${tB}字节)`, "字节长不足", "目标字节长度可能不足"];
      } else {
        [cs, cl, csh, cd] = ["RISK", "需核验 (字符→二进制编码)", "需核验编码", "多字节字符需核验编码字节数"];
      }
    }
    return compatResult({
      applicable: true,
      capStatus: cs,
      capLabel: cl,
      capShort: csh,
      capDetail: cd,
      convStatus: "CAST_REQUIRED",
      convLabel: convLbl,
      convShort: convSht,
      convDetail: convDtl,
    });
  }

  if (tf === "STRING") {
    const convLbl = `是 (${src.family_cn}→字符)`;
    const convSht = `转: ${src.family_cn}→字符`;
    const convDtl = `来源为${src.family_cn} (${src.raw})，目标为字符 (${tgt.raw})，需要显式类型转换`;
    let cs = "CONTAINED";
    let cl = "可包住";
    let csh = "可包住";
    let cd = `目标 ${tgt.raw} 无固定长度限制，可包住格式化输出`;
    if (!tgt.unbounded) {
      const tLen = Number(tgt.char_len || 0);
      if (sf === "TEMPORAL") {
        if (ss === "DATE") {
          if (tLen >= 10) cd = `目标 ${tLen} 位可包住标准日期 YYYY-MM-DD (10位)`;
          else if (tLen >= 8) [cs, cl, csh, cd] = ["RISK", `需核验 (位长 ${tLen} 仅支持紧凑日期 YYYYMMDD)`, `位长边界 ${tLen}<10`, "不足标准 10 位日期"];
          else [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (日期需8~10位→${tLen})`, `位长不足 10→${tLen}`, "目标字符长度不足以表示完整日期"];
        } else if (ss === "TIMESTAMP") {
          const req = 19 + (src.frac && src.frac > 0 ? 1 + src.frac : 0);
          if (tLen >= req) cd = `目标 ${tLen} 位可包住完整时间戳 (${req} 位)`;
          else [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (时间戳需${req}位→${tLen})`, `位长不足 ${req}→${tLen}`, `标准时间戳输出需 ${req} 位`];
        } else if (ss === "TIME") {
          const req = 8 + (src.frac && src.frac > 0 ? 1 + src.frac : 0);
          if (tLen >= req) cd = `目标 ${tLen} 位可包住时间输出 (${req} 位)`;
          else [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (时间需${req}位→${tLen})`, `位长不足 ${req}→${tLen}`, `时间格式输出需 ${req} 位`];
        } else if (tLen < 4) {
          [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (年份需4位→${tLen})`, `位长不足 4→${tLen}`, "年份转字符至少需 4 位"];
        }
      } else if (sf === "NUMERIC" && ss === "INT") {
        const req = Number(src.max_dec_digits || 10) + (src.unsigned ? 0 : 1);
        const safeReq = Number(src.safe_dec_digits || 9);
        if (tLen >= req) cd = `目标 ${tLen} 位可包住 ${src.bits} 位整型文本`;
        else if (tLen >= safeReq) [cs, cl, csh, cd] = ["RISK", `需核验 (整型文本最宽${req}位→${tLen})`, `位长边界 ${req}→${tLen}`, `极值最大需 ${req} 位`];
        else [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (整型需${req}位→${tLen})`, `位长不足 ${req}→${tLen}`, `整型文本表示至多需 ${req} 位`];
      } else if (sf === "NUMERIC" && ss === "DECIMAL") {
        if (src.unbounded) {
          [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (无界数值→定长${tLen})`, "位长不足", "无界定点数值转定长字符无法保证不截断"];
        } else {
          const p = Number(src.precision || 0);
          const sc = Number(src.scale || 0);
          const baseReq = p + (sc > 0 ? 1 : 0);
          const reqSigned = baseReq + 1;
          if (tLen >= reqSigned) cd = `目标 ${tLen} 位可包住 ${src.raw}`;
          else if (tLen >= baseReq) [cs, cl, csh, cd] = ["RISK", `需核验 (负值含符号需${reqSigned}位→${tLen})`, `位长边界 ${reqSigned}→${tLen}`, `带负号时需 ${reqSigned} 位`];
          else [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (定点需${reqSigned}位→${tLen})`, `位长不足 ${reqSigned}→${tLen}`, `${src.raw} 转字符需 ${baseReq}~${reqSigned} 位`];
        }
      } else if (sf === "NUMERIC" && ss === "FLOAT") {
        const req = Number(src.bits || 32) >= 64 ? 24 : 15;
        if (tLen >= req) cd = `目标 ${tLen} 位可包住浮点文本`;
        else if (tLen >= 10) [cs, cl, csh, cd] = ["RISK", `需核验 (浮点文本最宽${req}位→${tLen})`, `位长边界 ${req}→${tLen}`, "长小数输出可能超长"];
        else [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (浮点需${req}位→${tLen})`, `位长不足 ${req}→${tLen}`, "不足以输出浮点文本"];
      } else if (sf === "BOOL") {
        if (tLen >= 5) cd = "可包住 'true'/'false' 布尔文本";
        else if (tLen >= 1) [cs, cl, csh, cd] = ["RISK", `需核验 (位长 ${tLen} 仅支持单字符编码)`, `位长边界 ${tLen}<5`, "小于 5 位仅支持单字符布尔编码"];
        else [cs, cl, csh, cd] = ["INSUFFICIENT", "无法包住 (目标位长0)", "位长不足", "目标长度为 0"];
      } else if (sf === "JSON") {
        [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (JSON→定长${tLen})`, "JSON转定长截断", "JSON 长度不固定，转定长字符存在截断风险"];
      } else if (sf === "BINARY") {
        const sB = Number(src.byte_len || 0);
        if (src.unbounded || tLen < sB) [cs, cl, csh, cd] = ["INSUFFICIENT", `无法包住 (二进制→定长${tLen})`, "位长不足", "二进制转定长字符长度不足"];
        else if (tLen < sB * 2) [cs, cl, csh, cd] = ["RISK", `需核验 (Hex编码需${sB * 2}位→${tLen})`, "编码位长核验", "十六进制编码需要 2 倍字节长度"];
      } else {
        [cs, cl, csh, cd] = ["RISK", "需核验", "需核验", `${src.raw} → ${tgt.raw}`];
      }
    }
    return compatResult({
      applicable: true,
      capStatus: cs,
      capLabel: cl,
      capShort: csh,
      capDetail: cd,
      convStatus: "CAST_REQUIRED",
      convLabel: convLbl,
      convShort: convSht,
      convDetail: convDtl,
    });
  }

  if (sf === "TEMPORAL" && ss === "DATE" && tf === "NUMERIC") {
    let cs = "RISK";
    let cl = "需核验 (日期→浮点精度)";
    let csh = "需核验";
    let cd = "浮点不推荐存储 8 位日期键";
    if (ts === "INT") {
      const ok = Number(tgt.safe_dec_digits || 0) >= 8;
      cs = ok ? "CONTAINED" : "INSUFFICIENT";
      cl = ok ? "可包住" : `无法包住 (8位日期键→${tgt.bits}位整型)`;
      csh = ok ? "可包住" : "位宽不足";
      cd = ok ? "8 位 YYYYMMDD 日期整数可落入 32/64 位整型" : "目标整型位宽无法容纳 8 位 YYYYMMDD 整数";
    } else if (ts === "DECIMAL") {
      const ok = tgt.unbounded || Number(tgt.int_digits || 0) >= 8;
      cs = ok ? "CONTAINED" : "INSUFFICIENT";
      cl = ok ? "可包住" : `无法包住 (8位日期→整数位${tgt.int_digits}位)`;
      csh = ok ? "可包住" : `整数位不足 8→${tgt.int_digits}`;
      cd = ok ? "目标整数位 ≥ 8，可容纳 YYYYMMDD 日期键" : "目标整数位不足 8 位";
    }
    return compatResult({
      applicable: true,
      capStatus: cs,
      capLabel: cl,
      capShort: csh,
      capDetail: cd,
      convStatus: "CAST_REQUIRED",
      convLabel: `是 (日期→${tgt.family_cn})`,
      convShort: `转: 日期→${tgt.family_cn}`,
      convDetail: "日期类型转为数值编码（如 YYYYMMDD），涉及类型转换",
    });
  }
  if (sf === "NUMERIC" && tf === "TEMPORAL" && ts === "DATE") {
    return compatResult({
      applicable: true,
      capStatus: "RISK",
      capLabel: "需核验 (数值→日期 YYYYMMDD 语义)",
      capShort: "需核验日期语义",
      capDetail: `需确认 ${src.raw} 数值满足合法 YYYYMMDD 日期语义`,
      convStatus: "CAST_REQUIRED",
      convLabel: `是 (${src.family_cn}→日期)`,
      convShort: `转: ${src.family_cn}→日期`,
      convDetail: "数值转日期涉及显式格式解析与类型转换",
    });
  }

  return compatResult({
    applicable: true,
    capStatus: "INSUFFICIENT",
    capLabel: `无法包住 (异构不可直接映射: ${src.raw}→${tgt.raw})`,
    capShort: "异构类型不兼容",
    capDetail: `来源 ${src.raw} 与目标 ${tgt.raw} 语义域不兼容，无法直接包住`,
    convStatus: "CAST_REQUIRED",
    convLabel: `是 (${src.family_cn}→${tgt.family_cn})`,
    convShort: `转: ${src.family_cn}→${tgt.family_cn}`,
    convDetail: `跨语义族映射 (${src.raw} → ${tgt.raw})，涉及强制类型转换`,
  });
}
