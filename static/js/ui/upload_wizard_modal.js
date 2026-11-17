/**
 * Data.O.G 公共两步式模板导入向导弹窗 (UploadWizardModal)
 * 规范全平台所有数据导入入口：
 * 1. 先下载标准系统模板（含可选预填模板），再上传填写好的工作簿；
 * 2. 内置双阶段实时进度条（XHR 字节流上传进度 + 服务端实时行级/表级解析核验进度轮询 + 实时计时器）。
 */

import { platformApi } from "../core/api.js";
import { badge, clearEl, el, formatBytes, pickLocalFile, svgIcon } from "../core/dom.js";
import { Modal } from "./modal.js";
import { Toast } from "./toast.js";

function makeProgressId() {
  const rand = Math.random().toString(36).slice(2, 10);
  return `imp_${Date.now().toString(36)}_${rand}`;
}

/**
 * @param {Object} opts
 * @param {string} opts.title
 * @param {string} [opts.description]
 * @param {Array<{name: string, required?: boolean}>} [opts.templateColumns]
 * @param {() => Promise<any>} opts.onDownloadBlankTemplate
 * @param {string} [opts.prefillButtonLabel]
 * @param {(() => Promise<any>) | null} [opts.onDownloadPrefilledTemplate]
 * @param {Array<{value: string, label: string, desc?: string}>} [opts.modes]
 * @param {string} [opts.defaultMode]
 * @param {string} [opts.accept]
 * @param {(file: File, mode: string, uploadCtx: {progressId: string, onUploadProgress: (ratio: number, loaded: number, total: number) => void}) => Promise<void>} opts.onUpload
 */
export function openUploadWizardModal(opts) {
  const {
    title = "导入数据",
    description = "请先获取系统标准 Excel 模板填写后再上传解析，确保字段格式与编号严格匹配。",
    templateColumns = [],
    onDownloadBlankTemplate,
    prefillButtonLabel = "",
    onDownloadPrefilledTemplate = null,
    modes = [],
    defaultMode = "",
    accept = ".xlsx",
    onUpload,
  } = opts || {};

  let selectedFile = null;
  let selectedMode = defaultMode || (modes[0]?.value ?? "");
  let uploading = false;
  let pollTimer = null;
  let elapsedTimer = null;
  let kickPollTimer = null;
  let attemptSeq = 0;
  let modalOpen = true;

  const stopTimers = () => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    if (elapsedTimer) {
      clearInterval(elapsedTimer);
      elapsedTimer = null;
    }
    if (kickPollTimer) {
      clearTimeout(kickPollTimer);
      kickPollTimer = null;
    }
  };

  const fileStatusEl = el("div", { className: "wizard-file-status" });
  const cancelBtn = el("button", {
    type: "button",
    className: "mac-btn",
    text: "取消",
    onClick: () => {
      if (!uploading) Modal.close("cancel");
    },
  });
  const uploadBtn = el("button", {
    type: "button",
    className: "mac-btn mac-btn-primary",
    disabled: true,
    text: "开始导入",
  });

  // 实时导入进度条组件（默认隐藏，点击开始导入后展开）
  const spinnerEl = el("span", { className: "upload-progress-spinner", "aria-hidden": "true" });
  const stageTitleEl = el("strong", { className: "upload-progress-stage", text: "准备导入..." });
  const progressMetaEl = el("span", { className: "upload-progress-meta", text: "0% · 0.0s" });
  const progressFillEl = el("div", {
    className: "upload-progress-fill",
    style: { width: "0%" },
  });
  const progressTrackEl = el(
    "div",
    {
      className: "upload-progress-track",
      role: "progressbar",
      "aria-label": "导入进度",
      "aria-valuemin": "0",
      "aria-valuemax": "100",
      "aria-valuenow": "0",
    },
    progressFillEl
  );
  const progressDetailEl = el("div", {
    className: "upload-progress-detail",
    text: "等待上传工作簿...",
  });
  const progressPanelEl = el(
    "div",
    {
      className: "upload-progress-panel",
      style: { display: "none" },
    },
    el(
      "div",
      { className: "upload-progress-head" },
      el("div", { className: "upload-progress-stage-wrap" }, spinnerEl, stageTitleEl),
      progressMetaEl
    ),
    progressTrackEl,
    progressDetailEl
  );

  let currentPercent = 0;
  let startedAtMs = 0;

  const updateProgressView = ({ percent = null, stage = null, detail = null, state = "active" } = {}) => {
    if (percent !== null && Number.isFinite(percent)) {
      currentPercent = Math.max(0, Math.min(100, percent));
    }
    if (stage !== null) {
      stageTitleEl.textContent = stage;
    }
    if (detail !== null) {
      progressDetailEl.textContent = detail;
    }
    const roundedPct = Math.round(currentPercent);
    const elapsedSec = startedAtMs > 0 ? (( performance.now() - startedAtMs ) / 1000).toFixed(1) : "0.0";
    progressFillEl.style.width = `${currentPercent.toFixed(1)}%`;
    progressTrackEl.setAttribute("aria-valuenow", String(roundedPct));
    progressMetaEl.textContent = `${roundedPct}% · 已用时 ${elapsedSec}s`;

    progressPanelEl.classList.remove("is-active", "is-success", "is-error");
    if (state === "success") {
      progressPanelEl.classList.add("is-success");
    } else if (state === "error") {
      progressPanelEl.classList.add("is-error");
    } else {
      progressPanelEl.classList.add("is-active");
    }

    if (uploading && state === "active") {
      uploadBtn.textContent = `正在导入 (${roundedPct}%)...`;
    }
  };

  const renderFileStatus = () => {
    if (!selectedFile) {
      clearEl(
        fileStatusEl,
        svgIcon("upload", 20),
        el("div", { style: { fontWeight: "500" }, text: "点击选择文件或将填写好的表格拖放至此" }),
        el("div", {
          style: { fontSize: "11px", color: "var(--os-muted)" },
          text: `支持格式: ${accept}（自动取公式计算结果，#N/A 自动识别为空值）`,
        })
      );
      uploadBtn.disabled = true;
    } else {
      clearEl(
        fileStatusEl,
        svgIcon("check", 20),
        el("div", { style: { fontWeight: "600", color: "var(--os-text)" }, text: selectedFile.name }),
        el("div", {
          style: { fontSize: "11px", color: "var(--os-muted)" },
          text: `文件大小: ${formatBytes(selectedFile.size)}（点击可重新选择）`,
        })
      );
      uploadBtn.disabled = uploading;
    }
  };

  const pickFileAction = async () => {
    if (uploading) return;
    const f = await pickLocalFile(accept);
    if (!f) return;
    selectedFile = f;
    currentPercent = 0;
    progressPanelEl.style.display = "none";
    progressPanelEl.classList.remove("is-active", "is-success", "is-error");
    uploadBtn.textContent = "开始导入";
    renderFileStatus();
  };

  const dropzone = el(
    "div",
    {
      className: "upload-dropzone",
      role: "button",
      tabindex: "0",
      onClick: pickFileAction,
      onKeydown: (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          pickFileAction();
        }
      },
      onDragover: (e) => {
        if (uploading) return;
        e.preventDefault();
        dropzone.classList.add("is-dragover");
      },
      onDragleave: () => dropzone.classList.remove("is-dragover"),
      onDrop: (e) => {
        if (uploading) return;
        e.preventDefault();
        dropzone.classList.remove("is-dragover");
        const f = e.dataTransfer?.files?.[0];
        if (f) {
          selectedFile = f;
          currentPercent = 0;
          progressPanelEl.style.display = "none";
          progressPanelEl.classList.remove("is-active", "is-success", "is-error");
          uploadBtn.textContent = "开始导入";
          renderFileStatus();
        }
      },
    },
    fileStatusEl
  );
  renderFileStatus();

  const blankBtn = el(
    "button",
    {
      type: "button",
      className: "mac-btn mac-btn-sm",
      onClick: async () => {
        if (uploading) return;
        try {
          blankBtn.disabled = true;
          await onDownloadBlankTemplate?.();
          Toast.success("空白系统模板已开始下载");
        } catch (err) {
          Toast.error(err.message);
        } finally {
          if (!uploading) blankBtn.disabled = false;
        }
      },
    },
    svgIcon("download", 14),
    el("span", { text: "下载空白系统模板 (.xlsx)" })
  );

  const prefillBtn =
    typeof onDownloadPrefilledTemplate === "function" && prefillButtonLabel
      ? el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-sm",
            onClick: async () => {
              if (uploading) return;
              try {
                prefillBtn.disabled = true;
                await onDownloadPrefilledTemplate();
                Toast.success("已导出包含当前库表字段的预填模板");
              } catch (err) {
                Toast.error(err.message);
              } finally {
                if (!uploading) prefillBtn.disabled = false;
              }
            },
          },
          svgIcon("table", 14),
          el("span", { text: prefillButtonLabel })
        )
      : null;

  const step1Box = el(
    "section",
    { className: "upload-wizard-step" },
    el(
      "div",
      { className: "upload-wizard-step-head" },
      badge("第 1 步", "info"),
      el("strong", { text: "获取系统标准模板并按列填写" })
    ),
    el("p", {
      style: { margin: "0", fontSize: "12px", color: "var(--os-text-secondary)", lineHeight: "1.5" },
      text: description,
    }),
    templateColumns.length
      ? el(
          "div",
          { className: "wizard-col-tags" },
          templateColumns.map((col) =>
            badge(col.required ? `${col.name} *` : col.name, col.required ? "info" : "neutral")
          )
        )
      : null,
    el(
      "div",
      { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap", marginTop: "2px" } },
      blankBtn,
      prefillBtn
    )
  );

  const radioInputs = [];
  const modeRadios = modes.length
    ? el(
        "div",
        { className: "wizard-mode-group" },
        modes.map((m) => {
          const radio = el("input", {
            type: "radio",
            name: "upload-wizard-mode",
            value: m.value,
            checked: selectedMode === m.value,
            onChange: () => {
              selectedMode = m.value;
            },
          });
          radioInputs.push(radio);
          return el(
            "label",
            { className: "wizard-mode-opt" },
            radio,
            el(
              "span",
              {},
              el("strong", { style: { fontSize: "12px" }, text: m.label }),
              m.desc
                ? el("span", {
                    style: { marginLeft: "6px", fontSize: "11px", color: "var(--os-muted)" },
                    text: m.desc,
                  })
                : null
            )
          );
        })
      )
    : null;

  const step2Box = el(
    "section",
    { className: "upload-wizard-step" },
    el(
      "div",
      { className: "upload-wizard-step-head" },
      badge("第 2 步", "success"),
      el("strong", { text: "选择或拖入填写完成的 Excel 文件" })
    ),
    dropzone,
    modeRadios,
    progressPanelEl
  );

  const setControlsLocked = (locked) => {
    uploadBtn.disabled = locked || !selectedFile;
    cancelBtn.disabled = locked;
    blankBtn.disabled = locked;
    if (prefillBtn) prefillBtn.disabled = locked;
    radioInputs.forEach((r) => {
      r.disabled = locked;
    });
    dropzone.classList.toggle("is-disabled", locked);
    Modal.setCloseLocked(locked);
  };

  uploadBtn.addEventListener("click", async () => {
    if (!selectedFile || uploading) return;
    stopTimers();
    const myAttempt = ++attemptSeq;
    uploading = true;
    startedAtMs = performance.now();
    currentPercent = 2;
    progressPanelEl.style.display = "flex";
    setControlsLocked(true);

    const progressId = makeProgressId();
    let serverReported = false;
    let pollInFlight = false;
    let progressState = "active";

    updateProgressView({
      percent: 3,
      stage: "正在上传文件",
      detail: `准备传输 ${selectedFile.name} (${formatBytes(selectedFile.size)})...`,
      state: "active",
    });

    const pollServerProgress = async () => {
      if (!uploading || !modalOpen || myAttempt !== attemptSeq || pollInFlight) return;
      pollInFlight = true;
      try {
        const res = await platformApi.getImportProgress(progressId);
        if (!uploading || !modalOpen || myAttempt !== attemptSeq) return;
        const p = res?.progress;
        if (res?.found && p) {
          serverReported = true;
          const srvPct = Math.max(currentPercent, Number(p.percent || 0));
          const pState =
            p.status === "failed"
              ? "error"
              : p.status === "completed"
              ? "success"
              : "active";
          progressState = pState;
          updateProgressView({
            percent: srvPct,
            stage: p.stage || "正在解析导入",
            detail: p.detail || "正在处理工作簿数据...",
            state: pState,
          });
          if ((p.status === "completed" || p.status === "failed") && pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
          }
        }
      } catch (_e) {
        // 忽略偶发轮询抖动，以主请求最终结果为准
      } finally {
        pollInFlight = false;
      }
    };

    elapsedTimer = setInterval(() => {
      if (!uploading || !modalOpen || myAttempt !== attemptSeq) return;
      // 当文件已上传完毕但大工作簿首轮 XML 解析尚未返回时，平滑微推进进度以提供明确活跃反馈
      if (!serverReported && currentPercent >= 14 && currentPercent < 22) {
        currentPercent = Math.min(22, currentPercent + 0.35);
      }
      updateProgressView({ state: progressState });
    }, 100);

    pollTimer = setInterval(pollServerProgress, 320);

    const onUploadProgress = (ratio, loaded, total) => {
      if (!uploading || !modalOpen || myAttempt !== attemptSeq || serverReported) return;
      const mappedPct = 2 + Math.min(1, Math.max(0, ratio)) * 12;
      if (ratio >= 0.999) {
        updateProgressView({
          percent: Math.max(currentPercent, 14),
          stage: "正在解析 Excel 工作簿",
          detail: "文件已送达本地引擎，正在解析公式计算结果并匹配数据标准...",
          state: "active",
        });
        if (kickPollTimer) {
          clearTimeout(kickPollTimer);
        }
        kickPollTimer = setTimeout(() => {
          kickPollTimer = null;
          pollServerProgress();
        }, 35);
      } else {
        updateProgressView({
          percent: Math.max(currentPercent, mappedPct),
          stage: "正在上传文件",
          detail: `已传输 ${formatBytes(loaded)} / ${formatBytes(total)}`,
          state: "active",
        });
      }
    };

    try {
      await onUpload?.(selectedFile, selectedMode, { progressId, onUploadProgress });
      stopTimers();
      uploading = false;
      updateProgressView({
        percent: 100,
        stage: "导入完成",
        detail: "全部记录已解析并入库完成",
        state: "success",
      });
      uploadBtn.textContent = "导入完成";
      await new Promise((r) => setTimeout(r, 180));
      setControlsLocked(false);
      if (modalOpen) {
        Modal.close("uploaded");
      }
    } catch (err) {
      stopTimers();
      uploading = false;
      setControlsLocked(false);
      updateProgressView({
        percent: Math.max(8, currentPercent),
        stage: "导入未完成",
        detail: err?.message || "解析导入失败，请检查文件后重试",
        state: "error",
      });
      uploadBtn.textContent = "重新导入";
      Toast.error(err.message);
    }
  });

  const body = el("div", { className: "upload-wizard-body" }, step1Box, step2Box);

  Modal.open({
    title,
    width: "min(620px, 94vw)",
    body,
    footer: [cancelBtn, uploadBtn],
    onClose: () => {
      modalOpen = false;
      attemptSeq++;
      stopTimers();
      Modal.setCloseLocked(false);
    },
  });
}
