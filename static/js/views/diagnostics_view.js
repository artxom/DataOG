/**
 * Data.O.G 系统配置 · 全链路性能诊断与基准压测面板 (DiagnosticsView)
 *
 * 核心能力：
 * 1. 一键运行前后端全栈基准测试：浏览器环回 RTT（10 串行 + 6 并发）、FastAPI 事件循环抖动、
 *    线程池调度、本机网卡/DNS 解析、磁盘 4KB fsync / 1MB 吞吐、SQLite 开关库与 WAL 事务、
 *    Bootstrap 子阶段剖析，以及可选的远端数据库连接/AI 端点逐段握手探测；
 * 2. 合并展示浏览器端与服务端 Trace 耗时分解（快速区分代理/环回阻塞 vs 鉴权 vs 磁盘/EDR vs DB）；
 * 3. 一键导出全量脱敏诊断日志包 (.json) 供离线分析并制定精确优化方案。
 */

import {
  badge,
  clearEl,
  copyText,
  el,
  formatBytes,
  svgIcon,
} from "../core/dom.js";
import { perfMonitor } from "../core/perf_monitor.js";
import { diagnosticsApi } from "../core/platform_api.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";

function fmtMs(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "-";
  return `${Number(val).toFixed(1)} ms`;
}

export class DiagnosticsView {
  constructor({ onChange = null } = {}) {
    this.onChange = onChange;
    this.overview = null;
    this.loading = false;
    this.runningBench = false;
    this.exporting = false;
    this.includeConnections = false;
    this.includeLlm = false;
    this.traceFilterMode = "all";
    this.traceKeyword = "";

    this.root = el("div", { className: "diag-view-root" });
  }

  destroy() {
    // 保留无状态视图句柄，无常驻定时器
  }

  async refreshFromServer() {
    this.loading = true;
    this.render();
    try {
      const res = await diagnosticsApi.overview();
      this.overview = res;
    } catch (err) {
      Toast.error(err.message || "获取性能诊断数据失败");
    } finally {
      this.loading = false;
      this.render();
      this.onChange?.();
    }
  }

  async runFullBenchmark() {
    if (this.runningBench) return;
    this.runningBench = true;
    this.render();
    try {
      await perfMonitor.runLoopbackBenchmark(() => diagnosticsApi.ping());
      await diagnosticsApi.runBenchmark({
        include_connections: this.includeConnections,
        include_llm: this.includeLlm,
      });
      await this.refreshFromServer();
      Toast.success("全栈基准测试已完成");
    } catch (err) {
      Toast.error(err.message || "基准测试执行失败");
    } finally {
      this.runningBench = false;
      this.render();
    }
  }

  async exportBundle() {
    if (this.exporting) return;
    this.exporting = true;
    this.render();
    try {
      const clientSnapshot = perfMonitor.collectClientSnapshot();
      const fname = await diagnosticsApi.exportBundle({
        client_snapshot: clientSnapshot,
        include_service_log: true,
        include_execution_logs: true,
      });
      Toast.success(`已导出脱敏诊断日志包：${fname}`);
    } catch (err) {
      Toast.error(err.message || "导出诊断日志包失败");
    } finally {
      this.exporting = false;
      this.render();
    }
  }

  async copyCompactSummary() {
    const ov = this.overview || {};
    const clientSnap = perfMonitor.collectClientSnapshot();
    const compact = {
      captured_at: new Date().toISOString(),
      auto_findings: ov.auto_findings || [],
      server_summary: ov.summary || {},
      client_summary: clientSnap.summary || {},
      loopback_benchmark: clientSnap.last_loopback_benchmark || null,
      server_benchmark: ov.last_benchmark || null,
      environment: ov.environment || {},
      sqlite_stats: ov.sqlite_stats || {},
    };
    try {
      await copyText(JSON.stringify(compact, null, 2));
      Toast.success("精简性能摘要已复制到剪贴板");
    } catch (err) {
      Toast.error(err.message || "复制失败");
    }
  }

  async clearSamples() {
    try {
      await diagnosticsApi.clear();
      perfMonitor.clear();
      await this.refreshFromServer();
      Toast.success("性能采样记录已清空");
    } catch (err) {
      Toast.error(err.message || "清空失败");
    }
  }

  async updateSlowThreshold(msVal) {
    try {
      const res = await diagnosticsApi.updateConfig({ slow_threshold_ms: Number(msVal) });
      if (this.overview?.summary) {
        this.overview.summary.slow_threshold_ms = res.slow_threshold_ms;
      }
      await this.refreshFromServer();
      Toast.success(`慢请求阈值已设为 ${res.slow_threshold_ms} ms`);
    } catch (err) {
      Toast.error(err.message || "更新阈值失败");
    }
  }

  _buildMergedTraceRows() {
    const ov = this.overview || {};
    const srvList =
      this.traceFilterMode === "slow"
        ? ov.slow_traces || []
        : ov.recent_traces || [];
    const clientByTid = new Map();
    for (const cr of perfMonitor.requests || []) {
      if (cr.trace_id) {
        clientByTid.set(cr.trace_id, cr);
      }
    }

    const kw = (this.traceKeyword || "").trim().toLowerCase();
    return srvList
      .map((srv) => {
        const cr = srv.trace_id ? clientByTid.get(srv.trace_id) : null;
        return {
          ...srv,
          client_total_ms: cr ? cr.client_total_ms : null,
          network_gap_ms: cr ? cr.network_gap_ms : null,
        };
      })
      .filter((row) => {
        if (!kw) return true;
        const hay = `${row.method} ${row.path} ${row.trace_id} ${row.status_code}`.toLowerCase();
        return hay.includes(kw);
      });
  }

  async openTraceWaterfallModal(traceRow) {
    let fullTrace = traceRow;
    if (traceRow.trace_id && (!traceRow.spans || !traceRow.spans.length)) {
      try {
        const res = await diagnosticsApi.getTrace(traceRow.trace_id);
        if (res.trace) {
          fullTrace = { ...traceRow, ...res.trace };
        }
      } catch (_e) {
        // 使用既有摘要展示
      }
    }

    const catEntries = Object.entries(fullTrace.category_totals || {}).sort(
      (a, b) => Number(b[1]?.total_ms || 0) - Number(a[1]?.total_ms || 0)
    );
    const catWrap = el(
      "div",
      { style: { display: "flex", flexWrap: "wrap", gap: "6px" } },
      catEntries.length
        ? catEntries.map(([cat, info]) =>
            badge(
              `${cat}: ${Number(info.total_ms || 0).toFixed(1)} ms (${info.count} 次, max ${Number(info.max_ms || 0).toFixed(1)} ms)`,
              Number(info.total_ms || 0) >= 100 ? "warn" : "neutral"
            )
          )
        : el("span", { className: "muted", text: "无细分 Span 分类统计" })
    );

    const spans = fullTrace.spans || [];
    const totalMs = Math.max(1, Number(fullTrace.total_ms || 1));
    const spanRows = spans.map((sp) => {
      const dur = Number(sp.duration_ms || 0);
      const pct = Math.max(2, Math.min(100, (dur / totalMs) * 100));
      const metaText = sp.meta ? JSON.stringify(sp.meta) : "";
      return el(
        "tr",
        {},
        el("td", { className: "mono", text: `+${Number(sp.start_offset_ms || 0).toFixed(1)}` }),
        el(
          "td",
          { className: "mono" },
          el("div", { text: `${dur.toFixed(2)} ms` }),
          el("div", {
            className: `diag-span-bar ${dur >= 100 ? "is-hot" : ""}`,
            style: { width: `${pct}%` },
          })
        ),
        el("td", {}, badge(sp.category || "-", "info")),
        el("td", { className: "mono", text: sp.name || "-" }),
        el(
          "td",
          {},
          sp.is_main_thread
            ? badge("MainThread", dur >= 50 ? "danger" : "warn")
            : badge(sp.thread_name || "Worker", "neutral")
        ),
        el("td", {
          className: "mono",
          style: { fontSize: "11px", color: "var(--os-muted)", wordBreak: "break-all" },
          text: metaText || (sp.error_type ? `ERR:${sp.error_type}` : "-"),
        })
      );
    });

    const bodyNode = el(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "12px" } },
      el(
        "div",
        { className: "diag-meta-strip" },
        badge(`${fullTrace.method || "GET"} ${fullTrace.path || "/"}`, "info"),
        badge(`状态 ${fullTrace.status_code || 200}`, Number(fullTrace.status_code || 200) >= 400 ? "danger" : "success"),
        badge(`服务端 ${fmtMs(fullTrace.total_ms)}`, "neutral"),
        badge(`Gate1 鉴权 ${fmtMs(fullTrace.auth_gate1_ms)}`, "neutral"),
        badge(`业务处理 ${fmtMs(fullTrace.handler_ms)}`, "neutral"),
        traceRow.client_total_ms !== null && traceRow.client_total_ms !== undefined
          ? badge(`浏览器 ${fmtMs(traceRow.client_total_ms)}`, "neutral")
          : null,
        traceRow.network_gap_ms !== null && traceRow.network_gap_ms !== undefined
          ? badge(`环回差值 ${fmtMs(traceRow.network_gap_ms)}`, Number(traceRow.network_gap_ms) >= 100 ? "warn" : "neutral")
          : null
      ),
      el(
        "div",
        {},
        el("div", { className: "form-label", text: "分类耗时累计 (category_totals)" }),
        catWrap
      ),
      spans.length
        ? el(
            "div",
            { className: "diag-table-wrap" },
            el(
              "table",
              { className: "diag-trace-table" },
              el(
                "thead",
                {},
                el(
                  "tr",
                  {},
                  el("th", { text: "偏移 (ms)" }),
                  el("th", { text: "耗时" }),
                  el("th", { text: "分类" }),
                  el("th", { text: "阶段" }),
                  el("th", { text: "执行线程" }),
                  el("th", { text: "上下文标签" })
                )
              ),
              el("tbody", {}, spanRows)
            )
          )
        : el("div", { className: "grid-empty", text: "该请求未命中明细 Span 记录（总耗时极短或无深层底座调用）" })
    );

    Modal.open({
      title: `请求链路剖析 · Trace ${fullTrace.trace_id || "-"}`,
      width: "min(880px, 92vw)",
      body: bodyNode,
      footer: [
        el("button", {
          type: "button",
          className: "mac-btn mac-btn-primary",
          text: "关闭",
          onClick: () => Modal.close("ok"),
        }),
      ],
    });
  }

  _renderMetricRows(rows) {
    return el(
      "div",
      { className: "diag-kv-list" },
      rows.map(([label, val, tone]) =>
        el(
          "div",
          { className: "diag-kv-row" },
          el("span", { className: "diag-kv-lbl", text: label }),
          tone
            ? badge(String(val), tone)
            : el("span", { className: "diag-kv-val mono", text: String(val ?? "-") })
        )
      )
    );
  }

  render() {
    const ov = this.overview || {};
    const summary = ov.summary || {};
    const env = ov.environment || {};
    const sqlStats = ov.sqlite_stats || {};
    const bench = ov.last_benchmark || null;
    const clientSnap = perfMonitor.collectClientSnapshot();
    const clientSum = clientSnap.summary || {};
    const lbBench = clientSnap.last_loopback_benchmark || null;
    const rawFindings = [...(ov.auto_findings || [])];
    const clientGapP95 = Math.max(
      Number(clientSum.network_gap_p95_ms || 0),
      Number(lbBench?.seq_network_gap_p95_ms || 0)
    );
    let findings = rawFindings;
    if (
      clientGapP95 > 100 &&
      !rawFindings.some((f) => f.code === "CLIENT_LOOPBACK_OVERHEAD")
    ) {
      findings = [
        {
          code: "CLIENT_LOOPBACK_OVERHEAD",
          severity: clientGapP95 > 500 ? "critical" : "warn",
          title: "浏览器到本地 127.0.0.1 环回请求存在显著额外耗时",
          evidence: `浏览器与服务端耗时差值 (network_gap_ms) P95 达 ${clientGapP95.toFixed(1)} ms（服务端处理很快，但浏览器侧耗时明显放大）。`,
          suggestion:
            "重点排查企业代理/PAC 脚本未豁免 127.0.0.1/localhost、安全软件本地 HTTP 流量扫描，或浏览器并发连接数受限。",
        },
        ...rawFindings.filter((f) => f.code !== "OK_BASELINE"),
      ];
    }

    // 1. 顶部操作区
    const curThreshold = String(Math.round(Number(summary.slow_threshold_ms || 300)));
    const headerCard = el(
      "div",
      { className: "mac-group-box mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el(
          "div",
          {},
          el("h3", { className: "mac-section-title", text: "全链路性能诊断与基准压测 (Benchmark)" }),
          el("div", {
            style: { fontSize: "11.5px", color: "var(--os-muted)", marginTop: "3px" },
            text: "全内存零写盘采集浏览器环回、代理差值、鉴权、事件循环、磁盘/EDR 与数据库耗时；导出日志包严格脱敏零路径零密钥。",
          })
        ),
        badge(this.loading ? "加载中..." : `采样 ${summary.recent_count ?? 0} 条`, "neutral")
      ),
      el(
        "div",
        { className: "ws-action-bar" },
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-primary",
            disabled: this.runningBench,
            onClick: () => this.runFullBenchmark(),
          },
          svgIcon("activity", 13),
          this.runningBench ? "正在压测全链路..." : "运行全栈基准测试"
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn",
            disabled: this.exporting,
            onClick: () => this.exportBundle(),
          },
          svgIcon("download", 13),
          this.exporting ? "正在脱敏打包..." : "导出诊断日志包 (.json)"
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn",
            onClick: () => this.copyCompactSummary(),
          },
          svgIcon("copy", 13),
          "复制精简摘要"
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn",
            disabled: this.loading,
            onClick: () => this.refreshFromServer(),
          },
          svgIcon("refresh", 13),
          "刷新"
        ),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-ghost",
            onClick: () => this.clearSamples(),
          },
          svgIcon("trash", 13),
          "清空采样"
        )
      ),
      el(
        "div",
        { className: "diag-options-row" },
        el(
          "label",
          { className: "diag-check-label" },
          el("input", {
            type: "checkbox",
            checked: this.includeConnections,
            onChange: (e) => {
              this.includeConnections = Boolean(e.target.checked);
            },
          }),
          el("span", { text: "压测时同时探测已配置数据库连接 (DNS / TCP / 握手)" })
        ),
        el(
          "label",
          { className: "diag-check-label" },
          el("input", {
            type: "checkbox",
            checked: this.includeLlm,
            onChange: (e) => {
              this.includeLlm = Boolean(e.target.checked);
            },
          }),
          el("span", { text: "压测时同时探测已启用 AI 端点 (DNS / TCP / TLS / 模型列表)" })
        ),
        el(
          "div",
          { style: { marginLeft: "auto", display: "flex", alignItems: "center", gap: "6px" } },
          el("span", { style: { fontSize: "11.5px", color: "var(--os-muted)" }, text: "慢请求阈值" }),
          el(
            "select",
            {
              className: "mac-input mac-input-sm",
              value: curThreshold,
              onChange: (e) => this.updateSlowThreshold(e.target.value),
            },
            ["150", "300", "500", "1000"].map((v) =>
              el("option", { value: v, selected: curThreshold === v, text: `${v} ms` })
            )
          )
        )
      )
    );

    // 2. 自动瓶颈研判卡片
    const findingsCard = el(
      "div",
      { className: "mac-group-box mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: "自动瓶颈研判与线索" }),
        badge(`${findings.length} 项`, "neutral")
      ),
      el(
        "div",
        { className: "diag-finding-list" },
        findings.length
          ? findings.map((f) => {
              const tone =
                f.severity === "critical"
                  ? "danger"
                  : f.severity === "warn"
                  ? "warn"
                  : "info";
              return el(
                "div",
                { className: `diag-finding-card is-${f.severity || "info"}` },
                el(
                  "div",
                  { className: "diag-finding-head" },
                  badge(f.severity === "critical" ? "高优瓶颈" : f.severity === "warn" ? "需关注" : "正常", tone),
                  el("strong", { text: f.title || f.code }),
                  el("span", { className: "mono muted", style: { marginLeft: "auto", fontSize: "11px" }, text: f.code || "" })
                ),
                el("div", { className: "diag-finding-evidence", text: f.evidence || "" }),
                f.suggestion
                  ? el("div", { className: "diag-finding-suggestion", text: `建议：${f.suggestion}` })
                  : null
              );
            })
          : el("div", { className: "grid-empty", text: "点击上方「运行全栈基准测试」或先执行几步业务操作后刷新即可自动诊断" })
      )
    );

    // 3. 6 格 KPI 看板
    const kpiGrid = el(
      "div",
      { className: "ws-kpi-grid" },
      [
        { lbl: "服务端采样请求", val: String(summary.total_requests ?? 0) },
        { lbl: "服务端耗时 P95", val: fmtMs(summary.server_p95_ms) },
        { lbl: "浏览器端耗时 P95", val: fmtMs(clientSum.client_p95_ms) },
        { lbl: "环回/代理差值 P95", val: fmtMs(clientSum.network_gap_p95_ms) },
        { lbl: "Gate 1 鉴权均值", val: fmtMs(summary.auth_gate1_avg_ms) },
        { lbl: "SQLite 锁等待峰值", val: fmtMs(sqlStats.lock_wait_max_ms) },
      ].map((tile) =>
        el(
          "div",
          { className: "ws-kpi-tile" },
          el("span", { className: "ws-kpi-val", text: tile.val }),
          el("span", { className: "ws-kpi-lbl", text: tile.lbl })
        )
      )
    );

    // 4. 基准测试与环境明细卡片
    const osEnv = env.os || {};
    const stEnv = env.storage || {};
    const proxyEnv = env.network_proxy || {};
    const compEnv = env.components || {};
    const benchGrid = el(
      "div",
      { className: "diag-bench-grid" },
      el(
        "div",
        { className: "mac-group-box mac-section" },
        el(
          "div",
          { className: "mac-section-header" },
          el("h3", { className: "mac-section-title", text: "前端环回 / 事件循环 / 鉴权与 DNS" }),
          lbBench ? badge("已测试", "success") : badge("待运行", "neutral")
        ),
        this._renderMetricRows([
          ["浏览器 10 次串行 Ping (Avg / P95)", lbBench ? `${fmtMs(lbBench.seq_rtt_avg_ms)} / ${fmtMs(lbBench.seq_rtt_p95_ms)}` : "待运行"],
          ["浏览器 6 并发 Ping 总墙钟 / Max", lbBench ? `${fmtMs(lbBench.conc_wall_ms)} / ${fmtMs(lbBench.conc_rtt_max_ms)}` : "待运行"],
          ["浏览器网络代理差值 P95", lbBench ? fmtMs(lbBench.seq_network_gap_p95_ms) : "待运行"],
          ["事件循环唤醒抖动 (Avg / Max)", bench?.event_loop_lag ? `${fmtMs(bench.event_loop_lag.avg_lag_ms)} / ${fmtMs(bench.event_loop_lag.max_lag_ms)}` : "待运行"],
          ["线程池调度排队 (Avg / Max)", bench?.threadpool_dispatch ? `${fmtMs(bench.threadpool_dispatch.avg_ms)} / ${fmtMs(bench.threadpool_dispatch.max_ms)}` : "待运行"],
          ["设备指纹探针 (缓存 / 强刷)", bench?.license_probe ? `${fmtMs(bench.license_probe.device_probe_cached_ms)} / ${fmtMs(bench.license_probe.device_probe_fresh_ms)}` : "待运行"],
          ["主机名 gethostbyname / localhost", bench?.dns_and_hostname ? `${fmtMs(bench.dns_and_hostname.gethostbyname_ms)} / ${fmtMs(bench.dns_and_hostname.getaddrinfo_localhost_ms)}` : "待运行"],
          ["CPU Ed25519 验签均值", bench?.cpu_ed25519 ? fmtMs(bench.cpu_ed25519.avg_ms) : "待运行"],
        ])
      ),
      el(
        "div",
        { className: "mac-group-box mac-section" },
        el(
          "div",
          { className: "mac-section-header" },
          el("h3", { className: "mac-section-title", text: "磁盘 I/O / SQLite / 代理与运行环境" }),
          badge(stEnv.storage_drive_type || "UNKNOWN", stEnv.storage_drive_type === "REMOTE" || stEnv.is_unc_path ? "danger" : "neutral")
        ),
        this._renderMetricRows([
          ["4KB 写+fsync 延迟 (Avg / Max)", bench?.disk_io ? `${fmtMs(bench.disk_io.fsync_4k_avg_ms)} / ${fmtMs(bench.disk_io.fsync_4k_max_ms)}` : "待运行"],
          ["1MB 读写吞吐 (写 / 读)", bench?.disk_io ? `${bench.disk_io.write_1mb_mb_s} MB/s / ${bench.disk_io.read_1mb_mb_s} MB/s` : "待运行"],
          ["SQLite 开关连接 (私有 / 工作区)", bench?.sqlite_io ? `${fmtMs(bench.sqlite_io.private_open_close_avg_ms)} / ${fmtMs(bench.sqlite_io.workspace_open_close_avg_ms)}` : "待运行"],
          ["SQLite 临时 WAL 写入提交均值", bench?.sqlite_io ? fmtMs(bench.sqlite_io.temp_wal_write_commit_avg_ms) : "待运行"],
          ["Bootstrap 剖析 (耗时 / 体积)", bench?.bootstrap_profile?.total_ms !== undefined ? `${fmtMs(bench.bootstrap_profile.total_ms)} (${formatBytes(bench.bootstrap_profile.json_bytes || 0)})` : "待运行"],
          ["代理与环回豁免", proxyEnv.has_proxy_configured ? (proxyEnv.no_proxy_covers_loopback ? "已配代理(含127.0.0.1豁免)" : "已配代理(未豁免环回!)") : "无代理", proxyEnv.has_proxy_configured && !proxyEnv.no_proxy_covers_loopback ? "warn" : null],
          ["运行态与原生护盾", `${osEnv.system || osEnv.platform || "-"} · ${osEnv.is_frozen ? "PyInstaller" : "源码"} · 护盾 ${compEnv.native_license_shield_loaded ? "已加载" : "回退"}`],
          ["主库体积 (私有 / 工作区)", `${formatBytes(stEnv.private_db_size_bytes || 0)} / ${formatBytes(stEnv.workspace_db_size_bytes || 0)}`],
        ])
      )
    );

    // 可选数据库/AI 探测结果卡
    const probeCards = [];
    if (Array.isArray(bench?.connections_probe) && bench.connections_probe.length) {
      probeCards.push(
        el(
          "div",
          { className: "mac-group-box mac-section" },
          el("div", { className: "mac-section-header" }, el("h3", { className: "mac-section-title", text: "数据库连接逐段延迟探测" })),
          el(
            "div",
            { className: "diag-table-wrap" },
            el(
              "table",
              { className: "diag-trace-table" },
              el(
                "thead",
                {},
                el("tr", {}, ["名称", "类型", "DNS", "TCP 握手", "驱动/探针", "状态"].map((h) => el("th", { text: h })))
              ),
              el(
                "tbody",
                {},
                bench.connections_probe.map((c) =>
                  el(
                    "tr",
                    {},
                    el("td", { text: c.name || c.connection_id }),
                    el("td", {}, badge(c.db_type || "-", "neutral")),
                    el("td", { className: "mono", text: fmtMs(c.dns_ms) }),
                    el("td", { className: "mono", text: fmtMs(c.tcp_connect_ms) }),
                    el("td", { className: "mono", text: fmtMs(c.handshake_ms) }),
                    el("td", {}, badge(c.ok ? "连通" : c.error || "失败", c.ok ? "success" : "danger"))
                  )
                )
              )
            )
          )
        )
      );
    }
    if (Array.isArray(bench?.llm_probe) && bench.llm_probe.length) {
      probeCards.push(
        el(
          "div",
          { className: "mac-group-box mac-section" },
          el("div", { className: "mac-section-header" }, el("h3", { className: "mac-section-title", text: "AI 端点逐段延迟探测" })),
          el(
            "div",
            { className: "diag-table-wrap" },
            el(
              "table",
              { className: "diag-trace-table" },
              el(
                "thead",
                {},
                el("tr", {}, ["服务商", "主机", "DNS", "TCP", "TLS 上下文", "HTTP /models", "状态"].map((h) => el("th", { text: h })))
              ),
              el(
                "tbody",
                {},
                bench.llm_probe.map((p) =>
                  el(
                    "tr",
                    {},
                    el("td", { text: p.name || p.provider_id }),
                    el("td", { className: "mono", text: p.host || "-" }),
                    el("td", { className: "mono", text: fmtMs(p.dns_ms) }),
                    el("td", { className: "mono", text: fmtMs(p.tcp_connect_ms) }),
                    el("td", { className: "mono", text: fmtMs(p.ssl_ctx_ms) }),
                    el("td", { className: "mono", text: fmtMs(p.models_http_ms) }),
                    el("td", {}, badge(p.ok ? `正常 (${p.model_count || 0})` : p.error || "失败", p.ok ? "success" : "danger"))
                  )
                )
              )
            )
          )
        )
      );
    }

    // 5. 请求 Trace 明细表
    const traceRows = this._buildMergedTraceRows();
    const tracesCard = el(
      "div",
      { className: "mac-group-box mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: "最近请求与慢请求链路追踪（点击行查看阶段瀑布）" }),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px" } },
          el(
            "div",
            { className: "seg-group" },
            [
              { id: "all", label: `全部 (${(ov.recent_traces || []).length})` },
              { id: "slow", label: `慢请求 (${(ov.slow_traces || []).length})` },
            ].map((opt) =>
              el("button", {
                type: "button",
                className: `seg-btn ${this.traceFilterMode === opt.id ? "is-active" : ""}`,
                text: opt.label,
                onClick: () => {
                  this.traceFilterMode = opt.id;
                  this.render();
                },
              })
            )
          ),
          el("input", {
            className: "mac-input mac-input-sm",
            style: { width: "170px" },
            value: this.traceKeyword,
            placeholder: "按路径 / TraceId 过滤...",
            onInput: (e) => {
              this.traceKeyword = e.target.value;
              this.render();
            },
          })
        )
      ),
      traceRows.length
        ? el(
            "div",
            { className: "diag-table-wrap" },
            el(
              "table",
              { className: "diag-trace-table is-interactive" },
              el(
                "thead",
                {},
                el(
                  "tr",
                  {},
                  ["时间", "请求", "状态", "浏览器端", "服务端", "Gate 1 鉴权", "网络差值", "最耗时子阶段"].map((h) =>
                    el("th", { text: h })
                  )
                )
              ),
              el(
                "tbody",
                {},
                traceRows.slice(0, 80).map((r) => {
                  const topSpan = [...(r.spans || [])].sort(
                    (a, b) => Number(b.duration_ms || 0) - Number(a.duration_ms || 0)
                  )[0];
                  const isSlow = Number(r.total_ms || 0) >= Number(summary.slow_threshold_ms || 300);
                  return el(
                    "tr",
                    {
                      className: isSlow ? "is-slow-row" : "",
                      onClick: () => this.openTraceWaterfallModal(r),
                    },
                    el("td", { className: "mono", style: { fontSize: "11px", color: "var(--os-muted)" }, text: (r.started_at || "").split("T").pop() || "-" }),
                    el("td", { className: "mono", style: { maxWidth: "240px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, text: `${r.method || "GET"} ${r.path || "/"}` }),
                    el("td", {}, badge(String(r.status_code || 200), Number(r.status_code || 200) >= 400 ? "danger" : "neutral")),
                    el("td", { className: "mono", text: fmtMs(r.client_total_ms) }),
                    el("td", { className: "mono", text: fmtMs(r.total_ms) }),
                    el("td", { className: "mono", text: fmtMs(r.auth_gate1_ms) }),
                    el("td", { className: "mono", text: fmtMs(r.network_gap_ms) }),
                    el("td", {
                      className: "mono",
                      style: { fontSize: "11px", color: "var(--os-text-secondary)" },
                      text: topSpan ? `${topSpan.category}.${topSpan.name} (${Number(topSpan.duration_ms || 0).toFixed(1)} ms)` : "-",
                    })
                  );
                })
              )
            )
          )
        : el("div", { className: "grid-empty", text: "暂无匹配的请求 Trace 记录" })
    );

    clearEl(
      this.root,
      headerCard,
      findingsCard,
      kpiGrid,
      benchGrid,
      ...probeCards,
      tracesCard
    );
  }
}
