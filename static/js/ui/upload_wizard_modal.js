/**
 * Data.O.G 公共两步式模板导入向导弹窗 (UploadWizardModal)
 * 规范全平台所有数据导入入口：先下载标准系统模板（含可选预填模板），再上传填写好的工作簿
 */

import { badge, clearEl, el, formatBytes, pickLocalFile, svgIcon } from "../core/dom.js";
import { Modal } from "./modal.js";
import { Toast } from "./toast.js";

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
 * @param {(file: File, mode: string) => Promise<void>} opts.onUpload
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

  const fileStatusEl = el("div", { className: "wizard-file-status" });
  const uploadBtn = el("button", {
    type: "button",
    className: "mac-btn mac-btn-primary",
    disabled: true,
    text: "开始导入",
  });

  const renderFileStatus = () => {
    if (!selectedFile) {
      clearEl(
        fileStatusEl,
        svgIcon("upload", 20),
        el("div", { style: { fontWeight: "500" }, text: "点击选择文件或将填写好的表格拖放至此" }),
        el("div", {
          style: { fontSize: "11px", color: "var(--os-muted)" },
          text: `支持格式: ${accept}`,
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
        e.preventDefault();
        dropzone.classList.add("is-dragover");
      },
      onDragleave: () => dropzone.classList.remove("is-dragover"),
      onDrop: (e) => {
        e.preventDefault();
        dropzone.classList.remove("is-dragover");
        const f = e.dataTransfer?.files?.[0];
        if (f) {
          selectedFile = f;
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
        try {
          blankBtn.disabled = true;
          await onDownloadBlankTemplate?.();
          Toast.success("空白系统模板已开始下载");
        } catch (err) {
          Toast.error(err.message);
        } finally {
          blankBtn.disabled = false;
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
              try {
                prefillBtn.disabled = true;
                await onDownloadPrefilledTemplate();
                Toast.success("已导出包含当前库表字段的预填模板");
              } catch (err) {
                Toast.error(err.message);
              } finally {
                prefillBtn.disabled = false;
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
    modeRadios
  );

  uploadBtn.addEventListener("click", async () => {
    if (!selectedFile || uploading) return;
    uploading = true;
    uploadBtn.disabled = true;
    uploadBtn.textContent = "正在解析导入...";
    try {
      await onUpload?.(selectedFile, selectedMode);
      Modal.close("uploaded");
    } catch (err) {
      Toast.error(err.message);
      uploading = false;
      uploadBtn.disabled = false;
      uploadBtn.textContent = "开始导入";
    }
  });

  const body = el("div", { className: "upload-wizard-body" }, step1Box, step2Box);

  Modal.open({
    title,
    width: "min(620px, 94vw)",
    body,
    footer: [
      el("button", { type: "button", className: "mac-btn", text: "取消", onClick: () => Modal.close("cancel") }),
      uploadBtn,
    ],
  });
}
