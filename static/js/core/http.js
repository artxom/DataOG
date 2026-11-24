/**
 * Data.O.G 前端统一 HTTP 通信原语 (http.js)
 *
 * 3.0 新模块开发规范：新业务模块仅从本文件导入传输原语 (`requestJson` / `uploadFormData` /
 * `downloadApiFile` / `withRequestOpts` / `isAbortError`) 并在自身目录维护独立 API 客户端，
 * 严禁继续向 `core/api.js` 堆积业务模块端点。
 */

import { downloadBlob } from "./dom.js";
import { perfMonitor } from "./perf_monitor.js";

async function parseJsonOrThrow(resp) {
  let payload = null;
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    payload = await resp.json();
  } else {
    const txt = await resp.text();
    try {
      payload = JSON.parse(txt);
    } catch (_e) {
      payload = { ok: resp.ok, error: txt || `HTTP ${resp.status}` };
    }
  }
  if (!resp.ok || (payload && payload.ok === false)) {
    if (
      resp.status === 401 ||
      (payload && payload.code === "LICENSE_REQUIRED")
    ) {
      if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
        window.dispatchEvent(
          new CustomEvent("dataog:auth-required", { detail: payload || {} })
        );
      }
    }
    const msg =
      (payload && (payload.error || payload.detail || payload.message)) ||
      `请求失败 (HTTP ${resp.status})`;
    const err = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    err.status = resp.status;
    err.data = payload;
    err.code = payload?.code || "";
    err._headers = resp.headers || null;
    throw err;
  }
  return payload;
}

export function withRequestOpts(base = {}, opts = {}) {
  const next = { ...base };
  if (opts && typeof opts === "object") {
    if (opts.signal !== undefined) next.signal = opts.signal;
    if (opts.taskId) next.taskId = opts.taskId;
    if (opts.cache !== undefined && next.cache === undefined) next.cache = opts.cache;
    if (opts.trackInRing !== undefined) next.trackInRing = opts.trackInRing;
  }
  return next;
}

export function isAbortError(err) {
  return Boolean(
    err && (err.name === "AbortError" || String(err.message || "").includes("AbortError"))
  );
}

export async function requestJson(
  url,
  {
    method = "GET",
    body = undefined,
    cache = undefined,
    signal = undefined,
    taskId = "",
    trackInRing = true,
  } = {}
) {
  const opts = {
    method,
    headers: { Accept: "application/json" },
  };
  if (cache) {
    opts.cache = cache;
  }
  if (signal) {
    opts.signal = signal;
  }
  const normalizedTaskId = taskId ? String(taskId).trim() : "";
  if (normalizedTaskId) {
    opts.headers["X-Task-Id"] = normalizedTaskId;
  }
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const reqId = perfMonitor.beginRequest({ method, url, kind: "fetch", trackInRing });
  let fetchDoneMs = null;
  let respHeaders = null;
  let respStatus = 0;
  try {
    const resp = await fetch(url, opts);
    fetchDoneMs = perfMonitor.now();
    respHeaders = resp?.headers || null;
    respStatus = Number(resp?.status || 0);
    const parsed = await parseJsonOrThrow(resp);
    perfMonitor.finishRequest(reqId, {
      status: respStatus,
      ok: true,
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: respHeaders,
    });
    return parsed;
  } catch (err) {
    perfMonitor.finishRequest(reqId, {
      status: err?.status || respStatus,
      ok: false,
      error: err?.message || String(err),
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: err?._headers || respHeaders,
    });
    throw err;
  }
}

export async function uploadFormData(url, formData, { onUploadProgress = null } = {}) {
  if (typeof XMLHttpRequest !== "undefined") {
    const reqId = perfMonitor.beginRequest({ method: "POST", url, kind: "xhr_upload" });
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const collectXhrHeaders = () => ({
        get: (name) => {
          try {
            return xhr.getResponseHeader(name);
          } catch (_e) {
            return null;
          }
        },
      });
      xhr.open("POST", url, true);
      xhr.setRequestHeader("Accept", "application/json");
      if (typeof onUploadProgress === "function" && xhr.upload) {
        let reportedDone = false;
        xhr.upload.addEventListener("progress", (ev) => {
          if (ev.lengthComputable && ev.total > 0) {
            if (ev.loaded >= ev.total) reportedDone = true;
            onUploadProgress(ev.loaded / ev.total, ev.loaded, ev.total);
          }
        });
        xhr.upload.addEventListener("load", () => {
          if (!reportedDone) {
            reportedDone = true;
            onUploadProgress(1, 1, 1);
          }
        });
      }
      xhr.addEventListener("load", () => {
        const doneMs = perfMonitor.now();
        const rawText = xhr.responseText || "";
        let payload = null;
        try {
          payload = rawText ? JSON.parse(rawText) : { ok: xhr.status >= 200 && xhr.status < 300 };
        } catch (_e) {
          payload = {
            ok: xhr.status >= 200 && xhr.status < 300,
            error: rawText || `HTTP ${xhr.status}`,
          };
        }
        const httpOk = xhr.status >= 200 && xhr.status < 300;
        const hdrs = collectXhrHeaders();
        if (!httpOk || (payload && payload.ok === false)) {
          if (
            xhr.status === 401 ||
            (payload && payload.code === "LICENSE_REQUIRED")
          ) {
            if (typeof window !== "undefined" && typeof window.dispatchEvent === "function") {
              window.dispatchEvent(
                new CustomEvent("dataog:auth-required", { detail: payload || {} })
              );
            }
          }
          const msg =
            (payload && (payload.error || payload.detail || payload.message)) ||
            `请求失败 (HTTP ${xhr.status})`;
          const errMsg = typeof msg === "string" ? msg : JSON.stringify(msg);
          perfMonitor.finishRequest(reqId, {
            status: xhr.status,
            ok: false,
            error: errMsg,
            fetchDoneMs: doneMs,
            bodyDoneMs: doneMs,
            headers: hdrs,
          });
          reject(new Error(errMsg));
          return;
        }
        perfMonitor.finishRequest(reqId, {
          status: xhr.status,
          ok: true,
          fetchDoneMs: doneMs,
          bodyDoneMs: doneMs,
          headers: hdrs,
        });
        resolve(payload);
      });
      xhr.addEventListener("error", () => {
        const errMsg = "网络请求中断，请检查本地服务是否正在运行";
        perfMonitor.finishRequest(reqId, { status: 0, ok: false, error: errMsg });
        reject(new Error(errMsg));
      });
      xhr.addEventListener("abort", () => {
        const errMsg = "上传已取消";
        perfMonitor.finishRequest(reqId, { status: 0, ok: false, error: errMsg });
        reject(new Error(errMsg));
      });
      xhr.send(formData);
    });
  }

  const reqId = perfMonitor.beginRequest({ method: "POST", url, kind: "fetch_upload" });
  let fetchDoneMs = null;
  let respHeaders = null;
  let respStatus = 0;
  try {
    const resp = await fetch(url, {
      method: "POST",
      body: formData,
    });
    fetchDoneMs = perfMonitor.now();
    respHeaders = resp?.headers || null;
    respStatus = Number(resp?.status || 0);
    const parsed = await parseJsonOrThrow(resp);
    perfMonitor.finishRequest(reqId, {
      status: respStatus,
      ok: true,
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: respHeaders,
    });
    return parsed;
  } catch (err) {
    perfMonitor.finishRequest(reqId, {
      status: err?.status || respStatus,
      ok: false,
      error: err?.message || String(err),
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: err?._headers || respHeaders,
    });
    throw err;
  }
}

export async function downloadApiFile(
  url,
  {
    method = "GET",
    body = undefined,
    fallbackName = "export.bin",
    taskId = "",
    signal = undefined,
  } = {}
) {
  const opts = { method, headers: {} };
  if (taskId) {
    opts.headers["X-Task-Id"] = taskId;
  }
  if (signal) {
    opts.signal = signal;
  }
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const reqId = perfMonitor.beginRequest({ method, url, kind: "download" });
  let fetchDoneMs = null;
  let respHeaders = null;
  let respStatus = 0;
  try {
    const resp = await fetch(url, opts);
    fetchDoneMs = perfMonitor.now();
    respHeaders = resp?.headers || null;
    respStatus = Number(resp?.status || 0);
    if (!resp.ok) {
      await parseJsonOrThrow(resp);
    }
    const disp = resp.headers?.get("content-disposition") || "";
    let filename = fallbackName;
    const utfMatch = disp.match(/filename\*=UTF-8''([^;]+)/i);
    const plainMatch = disp.match(/filename="?([^";]+)"?/i);
    if (utfMatch && utfMatch[1]) {
      filename = decodeURIComponent(utfMatch[1]);
    } else if (plainMatch && plainMatch[1]) {
      filename = plainMatch[1];
    }
    const blob = await resp.blob();
    perfMonitor.finishRequest(reqId, {
      status: respStatus,
      ok: true,
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: respHeaders,
      responseBytes: Number(blob?.size || 0) || null,
    });
    downloadBlob(blob, filename);
    return filename;
  } catch (err) {
    perfMonitor.finishRequest(reqId, {
      status: err?.status || respStatus,
      ok: false,
      error: err?.message || String(err),
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: err?._headers || respHeaders,
    });
    throw err;
  }
}

async function _consumeSseStream(resp, { onEvent = null, initialLastEventId = 0 } = {}) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let donePayload = null;
  let terminalPayload = null;
  let streamError = null;
  let latestEventId = Number(initialLastEventId || 0) || 0;

  const dispatchFrames = (flushAll = false) => {
    const parts = buf.split(/\r?\n\r?\n/);
    if (!flushAll) {
      buf = parts.pop() ?? "";
    } else {
      buf = "";
    }
    for (const frame of parts) {
      if (!frame || !frame.trim()) continue;
      const lines = frame.split(/\r?\n/);
      let eventName = "message";
      let frameId = 0;
      const dataLines = [];
      for (const line of lines) {
        if (!line || line.startsWith(":")) continue;
        if (line.startsWith("id:")) {
          const parsedId = Number(line.slice(3).trim());
          if (Number.isFinite(parsedId) && parsedId > 0) {
            frameId = parsedId;
            if (parsedId > latestEventId) {
              latestEventId = parsedId;
            }
          }
        } else if (line.startsWith("event:")) {
          eventName = line.slice(6).trim() || "message";
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trimStart());
        }
      }
      if (!dataLines.length) continue;
      const rawData = dataLines.join("\n");
      let parsed = null;
      try {
        parsed = JSON.parse(rawData);
      } catch (_e) {
        parsed = { raw: rawData };
      }
      const effEvent =
        parsed && typeof parsed === "object" && parsed.type
          ? String(parsed.type)
          : eventName;
      if (effEvent === "done") {
        donePayload = parsed;
        terminalPayload = parsed;
      } else if (effEvent === "run_paused" || effEvent === "run_cancelled") {
        terminalPayload = parsed;
      }
      if (effEvent === "error") {
        streamError = new Error(
          (parsed && (parsed.error || parsed.message)) || "流式响应发生错误"
        );
        streamError.lastEventId = latestEventId;
      }
      if (typeof onEvent === "function") {
        onEvent(effEvent, parsed, frameId);
      }
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        buf += decoder.decode();
        dispatchFrames(true);
        break;
      }
      if (value) {
        buf += decoder.decode(value, { stream: true });
        dispatchFrames(false);
      }
    }
  } finally {
    try {
      reader.releaseLock?.();
    } catch (_e) {
      // ignore release lock error
    }
  }

  return { donePayload, terminalPayload, streamError, latestEventId };
}

export async function streamSsePost(
  url,
  {
    body = undefined,
    signal = undefined,
    taskId = "",
    onEvent = null,
    trackInRing = true,
  } = {}
) {
  const opts = {
    method: "POST",
    headers: { Accept: "text/event-stream" },
  };
  if (signal) {
    opts.signal = signal;
  }
  const normalizedTaskId = taskId ? String(taskId).trim() : "";
  if (normalizedTaskId) {
    opts.headers["X-Task-Id"] = normalizedTaskId;
  }
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }

  const reqId = perfMonitor.beginRequest({ method: "POST", url, kind: "sse_post", trackInRing });
  let fetchDoneMs = null;
  let respHeaders = null;
  let respStatus = 0;

  try {
    const resp = await fetch(url, opts);
    fetchDoneMs = perfMonitor.now();
    respHeaders = resp?.headers || null;
    respStatus = Number(resp?.status || 0);

    if (!resp.ok) {
      await parseJsonOrThrow(resp);
    }

    const ct = String(resp.headers?.get("content-type") || "").toLowerCase();
    if (!ct.includes("text/event-stream") || !resp.body || typeof resp.body.getReader !== "function") {
      const parsed = await parseJsonOrThrow(resp);
      perfMonitor.finishRequest(reqId, {
        status: respStatus,
        ok: true,
        fetchDoneMs,
        bodyDoneMs: perfMonitor.now(),
        headers: respHeaders,
      });
      return { fallbackJson: parsed };
    }

    const { donePayload, terminalPayload, streamError } = await _consumeSseStream(resp, { onEvent });
    if (streamError && !donePayload && !terminalPayload) {
      throw streamError;
    }
    perfMonitor.finishRequest(reqId, {
      status: respStatus,
      ok: true,
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: respHeaders,
    });
    return donePayload || terminalPayload || { ok: true };
  } catch (err) {
    perfMonitor.finishRequest(reqId, {
      status: err?.status || respStatus,
      ok: false,
      error: err?.message || String(err),
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: err?._headers || respHeaders,
    });
    throw err;
  }
}

export async function streamSseGet(
  url,
  {
    signal = undefined,
    lastEventId = 0,
    onEvent = null,
    trackInRing = true,
  } = {}
) {
  const opts = {
    method: "GET",
    headers: { Accept: "text/event-stream" },
  };
  if (signal) {
    opts.signal = signal;
  }
  const parsedInitId = Number(lastEventId || 0) || 0;
  if (parsedInitId > 0) {
    opts.headers["Last-Event-ID"] = String(parsedInitId);
  }

  const reqId = perfMonitor.beginRequest({ method: "GET", url, kind: "sse_get", trackInRing });
  let fetchDoneMs = null;
  let respHeaders = null;
  let respStatus = 0;

  try {
    const resp = await fetch(url, opts);
    fetchDoneMs = perfMonitor.now();
    respHeaders = resp?.headers || null;
    respStatus = Number(resp?.status || 0);

    if (!resp.ok) {
      await parseJsonOrThrow(resp);
    }

    const ct = String(resp.headers?.get("content-type") || "").toLowerCase();
    if (!ct.includes("text/event-stream") || !resp.body || typeof resp.body.getReader !== "function") {
      const parsed = await parseJsonOrThrow(resp);
      perfMonitor.finishRequest(reqId, {
        status: respStatus,
        ok: true,
        fetchDoneMs,
        bodyDoneMs: perfMonitor.now(),
        headers: respHeaders,
      });
      return { fallbackJson: parsed, lastEventId: parsedInitId };
    }

    const { donePayload, terminalPayload, streamError, latestEventId } = await _consumeSseStream(
      resp,
      { onEvent, initialLastEventId: parsedInitId }
    );
    if (streamError && !donePayload && !terminalPayload) {
      streamError.lastEventId = latestEventId;
      throw streamError;
    }
    perfMonitor.finishRequest(reqId, {
      status: respStatus,
      ok: true,
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: respHeaders,
    });
    return {
      ...(donePayload || terminalPayload || { ok: true }),
      lastEventId: latestEventId,
      reachedTerminal: Boolean(donePayload || terminalPayload),
    };
  } catch (err) {
    perfMonitor.finishRequest(reqId, {
      status: err?.status || respStatus,
      ok: false,
      error: err?.message || String(err),
      fetchDoneMs,
      bodyDoneMs: perfMonitor.now(),
      headers: err?._headers || respHeaders,
    });
    throw err;
  }
}
