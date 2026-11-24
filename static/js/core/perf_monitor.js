/**
 * Data.O.G 浏览器端零依赖全链路性能遥测与本地环回基准测试器 (perf_monitor.js)
 *
 * 职责：
 * 1. 无侵入采集每次 API 请求的客户端总耗时、fetch 网络耗时、响应体解析耗时，
 *    以及服务端通过 `X-DataOG-*` 下发的 Gate 1 鉴权/业务耗时，计算精确 `network_gap_ms`；
 * 2. 监听浏览器主线程 Long Task (>50ms) 与静态资源加载慢清单 (ResourceTiming)；
 * 3. 提供 10 串行 + 6 并发本地环回 RTT 基准测试 (`runLoopbackBenchmark`)，
 *    快速识别企业代理 / PAC / 杀软本地 HTTP 扫描或并发连接队列阻塞。
 */

const MAX_CLIENT_REQUESTS = 300;
const MAX_LONG_TASKS = 50;

function roundMs(v) {
  return Math.round(Number(v || 0) * 100) / 100;
}

function nowMs() {
  try {
    if (typeof performance !== "undefined" && typeof performance.now === "function") {
      return performance.now();
    }
  } catch (_e) {
    // 忽略受限环境异常
  }
  return Date.now();
}

function percentile(values, pct) {
  if (!values || !values.length) return 0;
  const sorted = [...values].map(Number).sort((a, b) => a - b);
  if (sorted.length === 1) return roundMs(sorted[0]);
  const idx = (Math.max(0, Math.min(100, pct)) / 100) * (sorted.length - 1);
  const lo = Math.floor(idx);
  const hi = Math.ceil(idx);
  if (lo === hi) return roundMs(sorted[lo]);
  return roundMs(sorted[lo] * (1 - (idx - lo)) + sorted[hi] * (idx - lo));
}

function normalizeUrlPath(rawUrl) {
  const s = String(rawUrl || "").trim();
  if (!s) return "/";
  try {
    const u = new URL(s, "http://127.0.0.1");
    const keys = [...u.searchParams.keys()];
    return keys.length ? `${u.pathname}?keys=${keys.join(",")}` : u.pathname;
  } catch (_e) {
    const qIdx = s.indexOf("?");
    return qIdx >= 0 ? s.slice(0, qIdx) : s;
  }
}

function readHeader(headers, name) {
  if (!headers) return null;
  try {
    if (typeof headers.get === "function") {
      return headers.get(name) ?? headers.get(name.toLowerCase()) ?? null;
    }
    if (typeof headers === "object") {
      const target = name.toLowerCase();
      for (const [k, v] of Object.entries(headers)) {
        if (String(k).toLowerCase() === target) return v;
      }
    }
  } catch (_e) {
    return null;
  }
  return null;
}

function parseOptionalNum(val) {
  if (val === null || val === undefined || val === "") return null;
  const n = Number(val);
  return Number.isFinite(n) ? roundMs(n) : null;
}

class ClientPerfMonitor {
  constructor() {
    this.requests = [];
    this.inFlight = new Map();
    this.longTasks = [];
    this.lastLoopbackBenchmark = null;
    this.lastFinished = null;
    this._seq = 0;
    this._observerStarted = false;
    this._initLongTaskObserver();
  }

  now() {
    return nowMs();
  }

  _initLongTaskObserver() {
    if (this._observerStarted) return;
    try {
      if (
        typeof PerformanceObserver === "function" &&
        Array.isArray(PerformanceObserver.supportedEntryTypes) &&
        PerformanceObserver.supportedEntryTypes.includes("longtask")
      ) {
        const obs = new PerformanceObserver((list) => {
          for (const entry of list.getEntries()) {
            this.longTasks.push({
              ts_iso: new Date().toISOString(),
              name: String(entry.name || "longtask"),
              start_time_ms: roundMs(entry.startTime),
              duration_ms: roundMs(entry.duration),
            });
            if (this.longTasks.length > MAX_LONG_TASKS) {
              this.longTasks.splice(0, this.longTasks.length - MAX_LONG_TASKS);
            }
          }
        });
        obs.observe({ entryTypes: ["longtask"] });
        this._observerStarted = true;
      }
    } catch (_e) {
      // 忽略不支持 PerformanceObserver 的运行环境
    }
  }

  beginRequest({ method = "GET", url = "/", kind = "fetch", trackInRing = true } = {}) {
    const reqId = `req_${++this._seq}`;
    const t0 = nowMs();
    this.inFlight.set(reqId, {
      req_id: reqId,
      method: String(method || "GET").toUpperCase(),
      url_path: normalizeUrlPath(url),
      kind,
      started_perf_ms: t0,
      ts_iso: new Date().toISOString(),
      in_flight_at_start: this.inFlight.size + 1,
      track_in_ring: trackInRing !== false,
    });
    return reqId;
  }

  finishRequest(
    reqId,
    {
      status = 0,
      ok = true,
      error = "",
      fetchDoneMs = null,
      bodyDoneMs = null,
      headers = null,
      responseBytes = null,
    } = {}
  ) {
    const inflight = this.inFlight.get(reqId);
    if (!inflight) return null;
    this.inFlight.delete(reqId);

    const endMs = bodyDoneMs ?? fetchDoneMs ?? nowMs();
    const fetchEnd = fetchDoneMs ?? endMs;
    const clientTotalMs = roundMs(Math.max(0, endMs - inflight.started_perf_ms));
    const fetchMs = roundMs(Math.max(0, fetchEnd - inflight.started_perf_ms));
    const bodyParseMs = roundMs(Math.max(0, endMs - fetchEnd));

    const traceId = String(readHeader(headers, "x-dataog-trace-id") || "").trim();
    const serverTotalMs = parseOptionalNum(readHeader(headers, "x-dataog-server-ms"));
    const serverAuthMs = parseOptionalNum(readHeader(headers, "x-dataog-auth-ms"));
    const serverHandlerMs = parseOptionalNum(readHeader(headers, "x-dataog-handler-ms"));
    const contentLen = parseOptionalNum(readHeader(headers, "content-length"));

    const networkGapMs =
      serverTotalMs !== null ? roundMs(Math.max(0, fetchMs - serverTotalMs)) : null;

    const record = {
      ts_iso: inflight.ts_iso,
      method: inflight.method,
      url_path: inflight.url_path,
      kind: inflight.kind,
      status: Number(status || 0),
      ok: Boolean(ok),
      error: ok ? "" : String(error || "").slice(0, 220),
      client_total_ms: clientTotalMs,
      fetch_ms: fetchMs,
      body_parse_ms: bodyParseMs,
      server_total_ms: serverTotalMs,
      server_auth_ms: serverAuthMs,
      server_handler_ms: serverHandlerMs,
      network_gap_ms: networkGapMs,
      trace_id: traceId,
      in_flight_at_start: inflight.in_flight_at_start,
      response_bytes: responseBytes ?? (contentLen !== null ? Math.round(contentLen) : null),
    };

    this.lastFinished = record;
    if (inflight.track_in_ring) {
      this.requests.push(record);
      if (this.requests.length > MAX_CLIENT_REQUESTS) {
        this.requests.splice(0, this.requests.length - MAX_CLIENT_REQUESTS);
      }
    }
    return record;
  }

  clear() {
    this.requests = [];
    this.longTasks = [];
    this.lastLoopbackBenchmark = null;
  }

  async runLoopbackBenchmark(pingFn) {
    const seqRecords = [];
    for (let i = 0; i < 10; i++) {
      await pingFn();
      if (this.lastFinished) {
        seqRecords.push({ ...this.lastFinished });
      }
    }

    const tConc0 = nowMs();
    const concResults = await Promise.all(
      Array.from({ length: 6 }, async () => {
        const t0 = nowMs();
        await pingFn();
        const wall = roundMs(nowMs() - t0);
        const last = this.lastFinished ? { ...this.lastFinished } : null;
        return {
          client_total_ms: last?.client_total_ms ?? wall,
          server_total_ms: last?.server_total_ms ?? null,
          network_gap_ms: last?.network_gap_ms ?? null,
        };
      })
    );
    const concWallMs = roundMs(nowMs() - tConc0);

    const seqClient = seqRecords.map((r) => Number(r.client_total_ms || 0));
    const seqServer = seqRecords
      .map((r) => r.server_total_ms)
      .filter((v) => typeof v === "number");
    const seqGap = seqRecords
      .map((r) => r.network_gap_ms)
      .filter((v) => typeof v === "number");
    const concRtts = concResults.map((r) => Number(r.client_total_ms || 0));
    const concGaps = concResults
      .map((r) => r.network_gap_ms)
      .filter((v) => typeof v === "number");

    const avg = (arr) =>
      arr.length ? roundMs(arr.reduce((a, b) => a + b, 0) / arr.length) : null;

    const summary = {
      run_at: new Date().toISOString(),
      seq_count: seqRecords.length,
      seq_rtt_avg_ms: avg(seqClient) ?? 0,
      seq_rtt_p95_ms: percentile(seqClient, 95),
      seq_rtt_max_ms: seqClient.length ? roundMs(Math.max(...seqClient)) : 0,
      seq_server_avg_ms: avg(seqServer),
      seq_network_gap_avg_ms: avg(seqGap),
      seq_network_gap_p95_ms: seqGap.length ? percentile(seqGap, 95) : null,
      seq_network_gap_max_ms: seqGap.length ? roundMs(Math.max(...seqGap)) : null,
      conc_count: concResults.length,
      conc_wall_ms: concWallMs,
      conc_rtt_max_ms: concRtts.length ? roundMs(Math.max(...concRtts)) : 0,
      conc_network_gap_max_ms: concGaps.length ? roundMs(Math.max(...concGaps)) : null,
    };
    this.lastLoopbackBenchmark = summary;
    return summary;
  }

  _collectNavigationTiming() {
    try {
      if (typeof performance === "undefined" || typeof performance.getEntriesByType !== "function") {
        return null;
      }
      const nav = performance.getEntriesByType("navigation")?.[0];
      if (!nav) return null;
      return {
        type: String(nav.type || ""),
        dns_ms: roundMs(Math.max(0, (nav.domainLookupEnd || 0) - (nav.domainLookupStart || 0))),
        tcp_ms: roundMs(Math.max(0, (nav.connectEnd || 0) - (nav.connectStart || 0))),
        ttfb_ms: roundMs(Math.max(0, (nav.responseStart || 0) - (nav.requestStart || 0))),
        download_ms: roundMs(Math.max(0, (nav.responseEnd || 0) - (nav.responseStart || 0))),
        dom_interactive_ms: roundMs(nav.domInteractive || 0),
        load_event_end_ms: roundMs(nav.loadEventEnd || 0),
      };
    } catch (_e) {
      return null;
    }
  }

  _collectSlowStaticResources() {
    try {
      if (typeof performance === "undefined" || typeof performance.getEntriesByType !== "function") {
        return [];
      }
      const entries = performance.getEntriesByType("resource") || [];
      return entries
        .map((e) => {
          const path = normalizeUrlPath(e.name || "");
          return {
            path,
            initiator_type: String(e.initiatorType || ""),
            duration_ms: roundMs(e.duration || 0),
            dns_ms: roundMs(Math.max(0, (e.domainLookupEnd || 0) - (e.domainLookupStart || 0))),
            tcp_ms: roundMs(Math.max(0, (e.connectEnd || 0) - (e.connectStart || 0))),
            ttfb_ms: roundMs(Math.max(0, (e.responseStart || 0) - (e.requestStart || 0))),
            transfer_size: Number(e.transferSize || 0),
          };
        })
        .filter((x) => !x.path.startsWith("/api/"))
        .sort((a, b) => b.duration_ms - a.duration_ms)
        .slice(0, 30);
    } catch (_e) {
      return [];
    }
  }

  collectClientSnapshot() {
    const curMs = nowMs();
    const activeRequests = [...this.inFlight.values()].map((r) => ({
      req_id: r.req_id,
      method: r.method,
      url_path: r.url_path,
      ts_iso: r.ts_iso,
      elapsed_so_far_ms: roundMs(Math.max(0, curMs - r.started_perf_ms)),
      in_flight_at_start: r.in_flight_at_start,
    }));

    const clientTotals = this.requests.map((r) => Number(r.client_total_ms || 0));
    const gaps = this.requests
      .map((r) => r.network_gap_ms)
      .filter((v) => typeof v === "number");

    const navObj = typeof navigator !== "undefined" ? navigator : {};
    const winObj = typeof window !== "undefined" ? window : {};

    return {
      captured_at: new Date().toISOString(),
      environment: {
        user_agent: String(navObj.userAgent || ""),
        platform: String(navObj.platform || ""),
        language: String(navObj.language || ""),
        hardware_concurrency: Number(navObj.hardwareConcurrency || 0) || null,
        device_memory_gb: Number(navObj.deviceMemory || 0) || null,
        viewport:
          winObj.innerWidth && winObj.innerHeight
            ? `${winObj.innerWidth}x${winObj.innerHeight}`
            : "",
      },
      summary: {
        total_recorded: this.requests.length,
        in_flight_count: activeRequests.length,
        client_p50_ms: percentile(clientTotals, 50),
        client_p95_ms: percentile(clientTotals, 95),
        client_max_ms: clientTotals.length ? roundMs(Math.max(...clientTotals)) : 0,
        network_gap_p95_ms: gaps.length ? percentile(gaps, 95) : null,
        long_task_count: this.longTasks.length,
      },
      navigation_timing: this._collectNavigationTiming(),
      slow_static_resources: this._collectSlowStaticResources(),
      long_tasks: [...this.longTasks],
      active_requests: activeRequests,
      last_loopback_benchmark: this.lastLoopbackBenchmark
        ? { ...this.lastLoopbackBenchmark }
        : null,
      requests: [...this.requests].slice(-250).reverse(),
    };
  }
}

export const perfMonitor = new ClientPerfMonitor();
