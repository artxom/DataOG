/**
 * Data.O.G 平台级轻量实时任务活动条 (TaskActivityBar)
 * - 180ms 静默宽限期：本地缓存命中等瞬时响应零闪烁
 * - 优先通过 SSE (/api/platform/tasks/{id}/stream) 接收阶段、百分比与微日志，自动无缝降级 250ms 轮询
 * - 120ms 渐进式耗时计时与未分块等待平滑推进（封顶 88%）
 * - 支持排他任务自动中止、用户手动取消、可折叠毛玻璃步骤明细气泡
 */

import { isAbortError, tasksApi } from "../core/api.js";
import { badge, clearEl, el } from "../core/dom.js";

const TERMINAL_STATUSES = new Set([
  "completed",
  "done",
  "succeeded",
  "failed",
  "error",
  "cancelled",
  "canceled",
]);

const SSE_EVENT_NAMES = [
  "snapshot",
  "progress",
  "log",
  "done",
  "complete",
  "completed",
  "cancelled",
  "canceled",
];

export function createTaskId(prefix = "tsk") {
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function normalizeLevel(level) {
  const lv = String(level || "info").toLowerCase();
  if (lv === "success" || lv === "ok" || lv === "done" || lv === "completed") {
    return { dotClass: "dot-success", badgeTone: "success" };
  }
  if (lv === "warn" || lv === "warning") {
    return { dotClass: "dot-warn", badgeTone: "warn" };
  }
  if (lv === "error" || lv === "danger" || lv === "failed") {
    return { dotClass: "dot-danger", badgeTone: "danger" };
  }
  if (lv === "debug" || lv === "muted") {
    return { dotClass: "dot-muted", badgeTone: "neutral" };
  }
  return { dotClass: "dot-info", badgeTone: "info" };
}

function formatLogElapsed(entry) {
  if (!entry || typeof entry !== "object") return "+0.0s";
  if (typeof entry.elapsed === "string" && entry.elapsed.trim()) {
    const raw = entry.elapsed.trim();
    return raw.startsWith("+") ? raw : `+${raw}`;
  }
  const msVal = entry.elapsed_ms ?? entry.offset_ms;
  if (msVal !== undefined && msVal !== null && Number.isFinite(Number(msVal))) {
    return `+${(Math.max(0, Number(msVal)) / 1000).toFixed(1)}s`;
  }
  const secVal = entry.elapsed_sec ?? entry.elapsed_s;
  if (secVal !== undefined && secVal !== null && Number.isFinite(Number(secVal))) {
    return `+${Math.max(0, Number(secVal)).toFixed(1)}s`;
  }
  return "+0.0s";
}

function pickLatestLog(logs) {
  if (!Array.isArray(logs) || logs.length === 0) return null;
  let best = logs[logs.length - 1];
  let bestSeq = best && best.seq !== undefined && best.seq !== null ? Number(best.seq) : -Infinity;
  for (const item of logs) {
    if (item && item.seq !== undefined && item.seq !== null) {
      const seqNum = Number(item.seq);
      if (Number.isFinite(seqNum) && seqNum >= bestSeq) {
        bestSeq = seqNum;
        best = item;
      }
    }
  }
  return best;
}

export class TaskActivityBar {
  constructor({ graceMs = 180, compact = false, autoHideDelayMs = 600 } = {}) {
    this.graceMs = Math.max(0, Number(graceMs ?? 180));
    this.compact = Boolean(compact);
    this.autoHideDelayMs = Math.max(0, Number(autoHideDelayMs ?? 600));

    this._destroyed = false;
    this._runSeq = 0;
    this._activeRun = null;
    this._autoHideTimer = null;
    this._popoverOpen = false;

    this.fillEl = el("div", {
      className: "task-activity-fill",
      style: { width: "0%" },
    });
    this.trackEl = el(
      "div",
      {
        className: "task-activity-track",
        role: "progressbar",
        "aria-label": "任务执行进度",
        "aria-valuemin": "0",
        "aria-valuemax": "100",
        "aria-valuenow": "0",
      },
      this.fillEl
    );

    this.pulseEl = el("span", {
      className: "task-activity-pulse",
      "aria-hidden": "true",
    });
    this.stageEl = el("strong", {
      className: "task-activity-stage",
      text: "",
    });
    this.tickerEl = el("div", {
      className: "task-activity-ticker",
    });
    this.timerEl = el("span", {
      className: "task-activity-timer",
      text: "0.0s",
    });

    this.stepsBtn = el("button", {
      type: "button",
      className: "mac-btn btn-xs task-activity-steps-btn",
      hidden: true,
      "aria-expanded": "false",
      text: "步骤 (0) ▾",
      onClick: (e) => {
        e.stopPropagation();
        this._togglePopover();
      },
    });

    this.cancelBtn = el("button", {
      type: "button",
      className: "mac-btn btn-xs task-activity-cancel-btn",
      hidden: true,
      text: "取消",
      onClick: (e) => {
        e.stopPropagation();
        this.cancel();
      },
    });

    this.dismissBtn = el("button", {
      type: "button",
      className: "mac-btn btn-xs mac-btn-ghost task-activity-dismiss-btn",
      hidden: true,
      title: "关闭提示",
      "aria-label": "关闭任务提示",
      text: "×",
      onClick: (e) => {
        e.stopPropagation();
        this.hide();
      },
    });

    this.actionsEl = el(
      "div",
      { className: "task-activity-actions" },
      this.timerEl,
      this.stepsBtn,
      this.cancelBtn,
      this.dismissBtn
    );

    this.bodyEl = el(
      "div",
      { className: "task-activity-body" },
      this.pulseEl,
      this.stageEl,
      this.tickerEl,
      this.actionsEl
    );

    this.popoverHeaderBadge = badge("0", "neutral");
    this.popoverListEl = el("div", { className: "task-activity-log-list" });
    this.popoverEl = el(
      "div",
      {
        className: "task-activity-popover",
        hidden: true,
        role: "region",
        "aria-label": "执行步骤日志",
      },
      el(
        "div",
        { className: "task-activity-popover-head" },
        el("span", { className: "task-activity-popover-title", text: "执行微日志" }),
        this.popoverHeaderBadge
      ),
      this.popoverListEl
    );

    this.el = el(
      "div",
      {
        className: `task-activity-bar is-hidden${this.compact ? " is-compact" : ""}`,
        role: "status",
        "aria-live": "polite",
      },
      this.trackEl,
      this.bodyEl,
      this.popoverEl
    );

    this._onDocClick = (e) => {
      if (this._popoverOpen && !this.el.contains(e.target)) {
        this._setPopoverOpen(false);
        this._scheduleAutoHideIfCompleted();
      }
    };
    if (typeof document !== "undefined" && document.addEventListener) {
      document.addEventListener("click", this._onDocClick);
    }
  }

  get isRunning() {
    return Boolean(this._activeRun && this._activeRun.running);
  }

  _isCurrentRun(run) {
    return Boolean(!this._destroyed && run && run.runId === this._runSeq);
  }

  _clearAutoHideTimer() {
    if (this._autoHideTimer) {
      clearTimeout(this._autoHideTimer);
      this._autoHideTimer = null;
    }
  }

  _cleanupRun(run) {
    if (!run) return;
    if (run.graceTimer) {
      clearTimeout(run.graceTimer);
      run.graceTimer = null;
    }
    if (run.tickerTimer) {
      clearInterval(run.tickerTimer);
      run.tickerTimer = null;
    }
    if (run.pollTimer) {
      clearInterval(run.pollTimer);
      run.pollTimer = null;
    }
    if (run.eventSource) {
      try {
        run.eventSource.close();
      } catch (_e) {
        // 忽略关闭异常
      }
      run.eventSource = null;
    }
  }

  _reveal() {
    if (this._destroyed) return;
    this.el.classList.remove("is-hidden");
  }

  _setToneClass(status) {
    this.el.classList.remove("is-warn", "is-error", "is-done", "is-running");
    if (status === "warn") {
      this.el.classList.add("is-warn");
    } else if (status === "error") {
      this.el.classList.add("is-error");
    } else if (status === "done") {
      this.el.classList.add("is-done");
    } else {
      this.el.classList.add("is-running");
    }
  }

  _setPopoverOpen(open) {
    this._popoverOpen = Boolean(open);
    this.popoverEl.hidden = !this._popoverOpen;
    this.popoverEl.classList.toggle("is-open", this._popoverOpen);
    this.stepsBtn.setAttribute("aria-expanded", this._popoverOpen ? "true" : "false");
    const count = this._activeRun?.recentLogs?.length || 0;
    this.stepsBtn.textContent = `步骤 (${count}) ${this._popoverOpen ? "▴" : "▾"}`;
    if (this._popoverOpen && this._activeRun) {
      this._renderPopoverLogs(this._activeRun.recentLogs);
    }
  }

  _togglePopover() {
    const next = !this._popoverOpen;
    if (next) {
      this._clearAutoHideTimer();
    }
    this._setPopoverOpen(next);
    if (!next) {
      this._scheduleAutoHideIfCompleted();
    }
  }

  _scheduleAutoHideIfCompleted() {
    const run = this._activeRun;
    if (!run || run.running || run.status !== "done" || this._popoverOpen) return;
    this._clearAutoHideTimer();
    this._autoHideTimer = setTimeout(() => {
      if (!this._destroyed && this._activeRun === run && !run.running && !this._popoverOpen) {
        this.hide();
      }
    }, this.autoHideDelayMs);
  }

  _renderPopoverLogs(logs) {
    const items = Array.isArray(logs) ? logs : [];
    this.popoverHeaderBadge.textContent = `${items.length}`;
    if (items.length === 0) {
      clearEl(
        this.popoverListEl,
        el("div", { className: "task-activity-log-empty", text: "暂无步骤日志" })
      );
      return;
    }
    const rows = items.map((entry) => {
      const { dotClass, badgeTone } = normalizeLevel(entry?.level);
      const stageText = String(entry?.stage || "执行");
      const summaryText = String(entry?.summary || entry?.detail || entry?.message || "");
      const stageBadge = badge(stageText, badgeTone);
      stageBadge.classList.add("task-activity-log-stage");
      return el(
        "div",
        { className: "task-activity-log-row" },
        el("span", { className: `dot ${dotClass} task-activity-log-dot` }),
        el("span", {
          className: "task-activity-log-elapsed",
          text: formatLogElapsed(entry),
        }),
        stageBadge,
        el("span", {
          className: "task-activity-log-summary",
          title: summaryText,
          text: summaryText,
        })
      );
    });
    clearEl(this.popoverListEl, rows);
    this.popoverListEl.scrollTop = this.popoverListEl.scrollHeight;
  }

  _updateTicker(run) {
    const latest = pickLatestLog(run.recentLogs);
    const latestSeq =
      latest && latest.seq !== undefined && latest.seq !== null && Number.isFinite(Number(latest.seq))
        ? Number(latest.seq)
        : null;
    const candidateText = latest
      ? String(latest.summary || latest.detail || latest.message || latest.stage || run.summary || "")
      : String(run.summary || "");
    const nextText = candidateText || "正在处理...";
    const seqChanged = latestSeq !== null && latestSeq !== run.lastLogSeq;
    const textChanged = nextText !== run.lastTickerText;
    if (seqChanged || textChanged || !this.tickerEl.firstChild) {
      if (latestSeq !== null) run.lastLogSeq = latestSeq;
      run.lastTickerText = nextText;
      clearEl(
        this.tickerEl,
        el("span", {
          className: "task-activity-ticker-text",
          title: nextText,
          text: nextText,
        })
      );
    }
  }

  _updateProgressAndTimer(run) {
    const pct = Math.max(0, Math.min(100, Number(run.displayPercent || 0)));
    const rounded = Math.round(pct);
    this.fillEl.style.width = `${pct.toFixed(1)}%`;
    this.trackEl.setAttribute("aria-valuenow", String(rounded));
    const elapsedSec =
      run.finalElapsedSec !== null && run.finalElapsedSec !== undefined
        ? run.finalElapsedSec
        : Math.max(0, (performance.now() - run.startedAt) / 1000);
    this.timerEl.textContent = `${elapsedSec.toFixed(1)}s`;
  }

  _renderRun(run) {
    if (!this._isCurrentRun(run) || !run.revealed) return;
    this._setToneClass(run.status);
    this.stageEl.textContent = run.stage || run.title || "执行中";
    this._updateTicker(run);
    this._updateProgressAndTimer(run);

    const logCount = Array.isArray(run.recentLogs) ? run.recentLogs.length : 0;
    this.stepsBtn.hidden = logCount === 0;
    this.stepsBtn.textContent = `步骤 (${logCount}) ${this._popoverOpen ? "▴" : "▾"}`;
    this.stepsBtn.setAttribute("aria-expanded", this._popoverOpen ? "true" : "false");

    const showCancel = Boolean(run.running && run.cancellable);
    this.cancelBtn.hidden = !showCancel;

    const showDismiss = Boolean(!run.running);
    this.dismissBtn.hidden = !showDismiss;

    if (this._popoverOpen) {
      this._renderPopoverLogs(run.recentLogs);
    }
  }

  _applySnapshot(run, rawSnap) {
    if (!this._isCurrentRun(run) || !run.running || !rawSnap) return;
    const snap = rawSnap.task || rawSnap.data || rawSnap;
    if (!snap || typeof snap !== "object") return;

    if (snap.stage) {
      run.stage = String(snap.stage);
    }
    const nextSummary = snap.detail || snap.summary || snap.message;
    if (nextSummary) {
      run.summary = String(nextSummary);
    }
    const rawPct = snap.percent ?? snap.progress;
    if (rawPct !== undefined && rawPct !== null && Number.isFinite(Number(rawPct))) {
      const pct = Math.max(0, Math.min(100, Number(rawPct)));
      if (pct > run.serverPercent) {
        run.serverPercent = pct;
        run.hasServerPercent = true;
        run.lastProgressAt = performance.now();
      }
      run.displayPercent = Math.max(run.displayPercent, Math.min(99.5, run.serverPercent));
    }
    const logs = snap.recent_logs || snap.logs;
    if (Array.isArray(logs)) {
      run.recentLogs = logs.slice(-50);
    }

    const statusLower = String(snap.status || "").toLowerCase();
    if (TERMINAL_STATUSES.has(statusLower)) {
      run.streamTerminal = true;
      if (run.eventSource) {
        try {
          run.eventSource.close();
        } catch (_e) {
          // 忽略关闭异常
        }
        run.eventSource = null;
      }
      if (run.pollTimer) {
        clearInterval(run.pollTimer);
        run.pollTimer = null;
      }
    }

    if (run.revealed) {
      this._renderRun(run);
    }
  }

  _startTicker(run) {
    if (run.tickerTimer) return;
    run.tickerTimer = setInterval(() => {
      if (!this._isCurrentRun(run) || !run.running) return;
      const now = performance.now();
      const elapsedSec = Math.max(0, (now - run.startedAt) / 1000);
      let asymptotic = 0;
      if (!run.hasServerPercent) {
        asymptotic = 88 * (1 - Math.exp(-elapsedSec / 4.5));
      } else if (run.serverPercent < 88) {
        const stalledSec = Math.max(0, (now - run.lastProgressAt) / 1000);
        asymptotic = run.serverPercent + (88 - run.serverPercent) * (1 - Math.exp(-stalledSec / 8.0));
      } else {
        asymptotic = run.serverPercent;
      }
      run.displayPercent = Math.max(run.displayPercent, Math.min(99.5, asymptotic));
      if (run.revealed) {
        this._updateProgressAndTimer(run);
      }
    }, 120);
  }

  _startPolling(run) {
    if (run.pollTimer || !this._isCurrentRun(run) || !run.running || run.streamTerminal) return;
    let inFlight = false;
    const pollOnce = async () => {
      if (!this._isCurrentRun(run) || !run.running || run.streamTerminal) {
        if (run.pollTimer) {
          clearInterval(run.pollTimer);
          run.pollTimer = null;
        }
        return;
      }
      if (inFlight) return;
      inFlight = true;
      try {
        const snap = await tasksApi.get(run.taskId);
        if (this._isCurrentRun(run) && run.running && snap) {
          this._applySnapshot(run, snap);
        }
      } catch (_e) {
        // 任务尚未注册或刚结束时忽略瞬时轮询错误
      } finally {
        inFlight = false;
      }
    };
    run.pollTimer = setInterval(pollOnce, 250);
    pollOnce();
  }

  _subscribeProgress(run) {
    if (!this._isCurrentRun(run) || !run.running) return;
    if (typeof EventSource !== "function") {
      this._startPolling(run);
      return;
    }

    let es = null;
    try {
      es = new EventSource(tasksApi.streamUrl(run.taskId));
    } catch (_e) {
      this._startPolling(run);
      return;
    }

    run.eventSource = es;

    const handlePayload = (rawData) => {
      if (!this._isCurrentRun(run) || !run.running || typeof rawData !== "string") return;
      const trimmed = rawData.trim();
      if (!trimmed) return;
      let parsed = null;
      try {
        parsed = JSON.parse(trimmed);
      } catch (_e) {
        return;
      }
      this._applySnapshot(run, parsed);
    };

    es.onmessage = (ev) => handlePayload(ev?.data);
    for (const evtName of SSE_EVENT_NAMES) {
      es.addEventListener(evtName, (ev) => handlePayload(ev?.data));
    }
    es.addEventListener("error", (ev) => {
      if (ev && typeof ev.data === "string" && ev.data.trim()) {
        handlePayload(ev.data);
        return;
      }
      if (run.eventSource === es) {
        try {
          es.close();
        } catch (_e) {
          // 忽略关闭异常
        }
        run.eventSource = null;
      }
      if (this._isCurrentRun(run) && run.running && !run.streamTerminal) {
        this._startPolling(run);
      }
    });
  }

  _beginVisibleExecution(run) {
    if (!this._isCurrentRun(run) || !run.running) return;
    run.revealed = true;
    this._reveal();
    this._renderRun(run);
    this._startTicker(run);
    this._subscribeProgress(run);
  }

  async track(title, asyncFn, { prefix = "tsk", cancellable = true, exclusive = true } = {}) {
    if (this._destroyed) {
      const fallbackController = new AbortController();
      return asyncFn({ taskId: createTaskId(prefix), signal: fallbackController.signal });
    }

    if (exclusive && this._activeRun && this._activeRun.running) {
      const prev = this._activeRun;
      prev.running = false;
      try {
        prev.abortController.abort();
      } catch (_e) {
        // 忽略中止异常
      }
      this._cleanupRun(prev);
    }

    this._clearAutoHideTimer();
    this._setPopoverOpen(false);
    this.el.classList.add("is-hidden");
    this.el.classList.remove("is-warn", "is-error", "is-done", "is-running");

    const runId = ++this._runSeq;
    const taskId = createTaskId(prefix);
    const abortController = new AbortController();
    const cleanTitle = String(title || "执行中");

    const run = {
      runId,
      taskId,
      title: cleanTitle,
      cancellable: Boolean(cancellable),
      abortController,
      running: true,
      cancelled: false,
      revealed: false,
      startedAt: performance.now(),
      finalElapsedSec: null,
      lastProgressAt: performance.now(),
      serverPercent: 0,
      hasServerPercent: false,
      displayPercent: 0,
      status: "running",
      stage: cleanTitle,
      summary: "正在连接与准备...",
      recentLogs: [],
      lastLogSeq: -1,
      lastTickerText: "",
      graceTimer: null,
      tickerTimer: null,
      pollTimer: null,
      eventSource: null,
      streamTerminal: false,
    };

    this._activeRun = run;

    if (this.graceMs <= 0) {
      this._beginVisibleExecution(run);
    } else {
      run.graceTimer = setTimeout(() => {
        this._beginVisibleExecution(run);
      }, this.graceMs);
    }

    try {
      const result = await asyncFn({ taskId: run.taskId, signal: run.abortController.signal });
      this._cleanupRun(run);
      run.running = false;
      run.finalElapsedSec = Math.max(0, (performance.now() - run.startedAt) / 1000);

      if (!this._isCurrentRun(run) || run.cancelled) {
        return result;
      }

      const warnText =
        typeof result?.warning === "string"
          ? result.warning.trim()
          : result?.warning
          ? String(result?.message || "操作完成，但存在告警")
          : "";
      const hasWarning = Boolean(warnText);

      if (!run.revealed && !hasWarning) {
        this.hide();
        return result;
      }

      run.revealed = true;
      run.displayPercent = 100;
      run.status = hasWarning ? "warn" : "done";
      if (hasWarning) {
        run.summary = warnText;
      } else if (!run.summary || run.summary === "正在连接与准备...") {
        run.summary = String(result?.message || "已完成");
      }

      this._reveal();
      this._renderRun(run);

      if (!hasWarning && !this._popoverOpen) {
        this._autoHideTimer = setTimeout(() => {
          if (this._isCurrentRun(run) && !run.running && !this._popoverOpen) {
            this.hide();
          }
        }, this.autoHideDelayMs);
      }

      return result;
    } catch (err) {
      this._cleanupRun(run);
      run.running = false;
      run.finalElapsedSec = Math.max(0, (performance.now() - run.startedAt) / 1000);

      if (isAbortError(err) || run.cancelled || run.abortController.signal.aborted) {
        if (this._isCurrentRun(run)) {
          this.hide();
        }
        throw err;
      }

      if (this._isCurrentRun(run)) {
        const errMsg = String(err?.message || err || "执行失败");
        run.revealed = true;
        run.status = "error";
        run.summary = errMsg;
        if (!run.stage || run.stage === run.title) {
          run.stage = `${run.title}失败`;
        }
        if (run.recentLogs.length > 0) {
          const tail = run.recentLogs[run.recentLogs.length - 1];
          const tailMsg = String(tail?.summary || tail?.detail || tail?.message || "");
          if (String(tail?.level || "").toLowerCase() !== "error" || tailMsg !== errMsg) {
            run.recentLogs = [
              ...run.recentLogs.slice(-49),
              {
                seq: Number.isFinite(Number(tail?.seq)) ? Number(tail.seq) + 1 : run.recentLogs.length + 1,
                elapsed_ms: Math.round(run.finalElapsedSec * 1000),
                level: "error",
                stage: run.stage,
                summary: errMsg,
              },
            ];
          }
        }
        this._reveal();
        this._renderRun(run);
      }

      throw err;
    }
  }

  cancel() {
    const run = this._activeRun;
    if (!run || !run.running) return;
    run.cancelled = true;
    run.running = false;
    tasksApi.cancel(run.taskId).catch(() => {});
    try {
      run.abortController.abort();
    } catch (_e) {
      // 忽略中止异常
    }
    this._cleanupRun(run);
    this.hide();
  }

  hide() {
    this._clearAutoHideTimer();
    this._setPopoverOpen(false);
    this.el.classList.add("is-hidden");
    this.el.classList.remove("is-warn", "is-error", "is-done", "is-running");
  }

  destroy() {
    this._destroyed = true;
    this._runSeq += 1;
    this._clearAutoHideTimer();
    if (this._activeRun) {
      const run = this._activeRun;
      run.running = false;
      try {
        run.abortController.abort();
      } catch (_e) {
        // 忽略中止异常
      }
      this._cleanupRun(run);
      this._activeRun = null;
    }
    if (typeof document !== "undefined" && document.removeEventListener && this._onDocClick) {
      document.removeEventListener("click", this._onDocClick);
    }
    this.hide();
  }
}
