/**
 * Data.O.G 前端统一 HTTP 通信原语 (http.js)
 *
 * 3.0 新模块开发规范：新业务模块仅从本文件导入传输原语 (`requestJson` / `uploadFormData` /
 * `downloadApiFile` / `withRequestOpts` / `isAbortError`) 并在自身目录维护独立 API 客户端，
 * 严禁继续向 `core/api.js` 堆积业务模块端点。
 */

import { downloadBlob } from "./dom.js";

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
  { method = "GET", body = undefined, cache = undefined, signal = undefined, taskId = "" } = {}
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
  const resp = await fetch(url, opts);
  return parseJsonOrThrow(resp);
}

export async function uploadFormData(url, formData, { onUploadProgress = null } = {}) {
  if (typeof XMLHttpRequest !== "undefined") {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
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
          reject(new Error(typeof msg === "string" ? msg : JSON.stringify(msg)));
          return;
        }
        resolve(payload);
      });
      xhr.addEventListener("error", () => {
        reject(new Error("网络请求中断，请检查本地服务是否正在运行"));
      });
      xhr.addEventListener("abort", () => {
        reject(new Error("上传已取消"));
      });
      xhr.send(formData);
    });
  }

  const resp = await fetch(url, {
    method: "POST",
    body: formData,
  });
  return parseJsonOrThrow(resp);
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
  const resp = await fetch(url, opts);
  if (!resp.ok) {
    await parseJsonOrThrow(resp);
  }
  const disp = resp.headers.get("content-disposition") || "";
  let filename = fallbackName;
  const utfMatch = disp.match(/filename\*=UTF-8''([^;]+)/i);
  const plainMatch = disp.match(/filename="?([^";]+)"?/i);
  if (utfMatch && utfMatch[1]) {
    filename = decodeURIComponent(utfMatch[1]);
  } else if (plainMatch && plainMatch[1]) {
    filename = plainMatch[1];
  }
  const blob = await resp.blob();
  downloadBlob(blob, filename);
  return filename;
}
