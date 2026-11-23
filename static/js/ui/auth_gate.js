/**
 * Data.O.G 离线授权门禁与菜单栏授权详情弹窗 (AuthGateController)
 * 严格遵循零 innerHTML / 零阻塞弹窗红线。
 */

import { authApi } from "../core/platform_api.js";
import {
  badge,
  clearEl,
  copyText,
  el,
  renderBrandMark,
  svgIcon,
} from "../core/dom.js";
import { store } from "../core/state.js";
import { Modal } from "./modal.js";
import { Toast } from "./toast.js";

function showActivationSuccessNotice(licenseInfo) {
  const expDate = licenseInfo?.expires_date || "-";
  const days = licenseInfo?.days_remaining ?? 0;
  const scopeLabel = licenseInfo?.scope_label || "离线授权";
  const device = licenseInfo?.device || {};

  const okBtn = el("button", {
    type: "button",
    className: "mac-btn mac-btn-primary",
    text: "开始使用",
    autofocus: true,
    onClick: () => Modal.close("ok"),
  });

  const body = el(
    "div",
    { className: "auth-success-dialog-body" },
    el(
      "div",
      { className: "auth-success-hero" },
      el("span", { className: "auth-success-icon" }, svgIcon("shield", 22)),
      el(
        "div",
        null,
        el("div", { className: "auth-success-headline", text: `当前激活码可用日期至 ${expDate}` }),
        el("div", {
          className: "auth-success-sub",
          text: `${scopeLabel} · 剩余可用 ${days} 天`,
        })
      )
    ),
    el(
      "div",
      { className: "auth-success-meta" },
      el("div", { className: "auth-meta-row" }, el("span", { text: "本机机器码" }), el("code", { className: "mono", text: device.machine_code || "-" })),
      el("div", { className: "auth-meta-row" }, el("span", { text: "本机 IP" }), el("code", { className: "mono", text: device.primary_ip || "-" }))
    )
  );

  Modal.open({
    title: "激活成功",
    width: "460px",
    body,
    footer: [okBtn],
  });
}

export class AuthGateController {
  constructor({ mountEl, onAuthenticated, onLocked } = {}) {
    this.mountEl = mountEl;
    this.onAuthenticated = onAuthenticated;
    this.onLocked = onLocked;
    this.loginMode = "activation_code"; // activation_code | account
    this.submitting = false;
    this.statusData = null;
  }

  hide() {
    if (this.mountEl) {
      this.mountEl.hidden = true;
      clearEl(this.mountEl);
    }
  }

  render(statusData) {
    if (!this.mountEl) return;
    if (statusData) {
      this.statusData = statusData;
    }
    const data = this.statusData || {};
    const device = data.device || {};
    const contact = data.contact || {
      owner: "莫政辉",
      phone: "131-6111-4561",
      email: "artxom@outlook.com",
      notice: "如需激活码，烦请联系 莫政辉  手机 131-6111-4561  邮箱 artxom@outlook.com",
    };
    const appearance = store.state.appearance || {};
    const statusCode = data.status || "UNACTIVATED";
    const statusMsg = data.status_message || "";

    const segCodeBtn = el("button", {
      type: "button",
      className: `mac-seg-btn ${this.loginMode === "activation_code" ? "is-active" : ""}`,
      text: "激活码模式",
      onClick: () => {
        if (this.loginMode === "activation_code") return;
        this.loginMode = "activation_code";
        this.render();
      },
    });
    const segAccBtn = el("button", {
      type: "button",
      className: `mac-seg-btn ${this.loginMode === "account" ? "is-active" : ""}`,
      text: "账号密码登录",
      onClick: () => {
        if (this.loginMode === "account") return;
        this.loginMode = "account";
        this.render();
      },
    });

    const noticeTone =
      statusCode === "EXPIRED" || statusCode === "DEVICE_MISMATCH" || statusCode === "CLOCK_ROLLBACK"
        ? "danger"
        : statusCode === "LOCKED"
        ? "warn"
        : "info";

    const alertBanner =
      statusMsg && statusCode !== "ACTIVE"
        ? el(
            "div",
            { className: `auth-banner auth-banner-${noticeTone}` },
            badge(data.status_label || "需授权", noticeTone),
            el("span", { text: statusMsg })
          )
        : null;

    const codeInput = el("textarea", {
      id: "auth-activation-code-input",
      className: "mac-input mono auth-code-textarea",
      rows: "3",
      autocomplete: "off",
      autocorrect: "off",
      autocapitalize: "characters",
      spellcheck: "false",
      placeholder: "请粘贴以 DOG1- 开头的离线激活码...",
    });

    const errorBox = el("div", { className: "auth-inline-error", hidden: true });
    const submitBtn = el("button", {
      type: "submit",
      className: "mac-btn mac-btn-primary auth-submit-btn",
      text: this.submitting ? "正在验签..." : "立即激活并进入",
      disabled: this.submitting,
    });

    const handleActivateSubmit = async (ev) => {
      ev.preventDefault();
      if (this.submitting) return;
      const code = (codeInput.value || "").trim();
      if (!code) {
        errorBox.textContent = "请输入激活码后再提交";
        errorBox.hidden = false;
        codeInput.focus();
        return;
      }
      errorBox.hidden = true;
      this.submitting = true;
      submitBtn.disabled = true;
      submitBtn.textContent = "正在校验数字签名与设备指纹...";
      try {
        const res = await authApi.activate({
          login_mode: "activation_code",
          activation_code: code,
        });
        const lic = res.license || {};
        this.hide();
        await this.onAuthenticated?.(lic);
        showActivationSuccessNotice(lic);
      } catch (err) {
        errorBox.textContent = err.message || "激活失败";
        errorBox.hidden = false;
      } finally {
        this.submitting = false;
        submitBtn.disabled = false;
        submitBtn.textContent = "立即激活并进入";
      }
    };

    const deviceBox = el(
      "div",
      { className: "auth-device-box" },
      el(
        "div",
        { className: "auth-device-head" },
        el("span", { className: "auth-device-title", text: "本机离线识别信息（自动采集）" }),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-sm",
            onClick: async () => {
              if (!device.request_code) return;
              await copyText(device.request_code);
              Toast.success("已复制设备申请码，请发送给管理员签发激活码");
            },
          },
          svgIcon("copy", 13),
          el("span", { text: "复制设备申请码" })
        )
      ),
      el(
        "div",
        { className: "auth-device-grid" },
        el(
          "div",
          { className: "auth-device-kv" },
          el("span", { className: "auth-kv-k", text: "本机 IP" }),
          el("code", { className: "mono auth-kv-v", text: device.primary_ip || "-" })
        ),
        el(
          "div",
          { className: "auth-device-kv" },
          el("span", { className: "auth-kv-k", text: "本机机器码" }),
          el("code", { className: "mono auth-kv-v", text: device.machine_code || "-" })
        )
      )
    );

    const activationBody = el(
      "form",
      { className: "auth-form", onSubmit: handleActivateSubmit },
      deviceBox,
      el(
        "label",
        { className: "auth-field-label", for: "auth-activation-code-input" },
        el("span", { text: "输入激活码" }),
        el("span", { className: "auth-field-hint", text: "一机一码 · 严格时效保护" })
      ),
      codeInput,
      errorBox,
      submitBtn
    );

    const accountBody = el(
      "div",
      { className: "auth-account-disabled" },
      el(
        "div",
        { className: "auth-account-notice" },
        badge("试用期未开放", "warn"),
        el("span", {
          text: "当前处于离线试用期且系统不联网，账号密码登录暂未开放，请使用「激活码模式」进入工作台。",
        })
      ),
      el(
        "div",
        { className: "auth-disabled-fields" },
        el("input", {
          type: "text",
          className: "mac-input",
          placeholder: "账号（试用期暂未开放）",
          disabled: true,
        }),
        el("input", {
          type: "password",
          className: "mac-input",
          placeholder: "密码（试用期暂未开放）",
          disabled: true,
        }),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-primary",
            onClick: () => {
              this.loginMode = "activation_code";
              this.render();
            },
          },
          el("span", { text: "切换至激活码模式" })
        )
      )
    );

    const contactFooter = el(
      "footer",
      { className: "auth-contact-box" },
      el("div", {
        className: "auth-contact-title",
        text: `如需激活码，烦请联系 ${contact.owner || "莫政辉"}`,
      }),
      el(
        "div",
        { className: "auth-contact-items" },
        el(
          "button",
          {
            type: "button",
            className: "auth-contact-chip",
            title: "点击复制手机号",
            onClick: async () => {
              await copyText(contact.phone || "131-6111-4561");
              Toast.success("已复制手机号");
            },
          },
          el("span", { text: `手机 ${contact.phone || "131-6111-4561"}` })
        ),
        el(
          "button",
          {
            type: "button",
            className: "auth-contact-chip",
            title: "点击复制邮箱",
            onClick: async () => {
              await copyText(contact.email || "artxom@outlook.com");
              Toast.success("已复制邮箱地址");
            },
          },
          el("span", { text: `邮箱 ${contact.email || "artxom@outlook.com"}` })
        )
      )
    );

    const card = el(
      "article",
      { className: "auth-card" },
      el(
        "header",
        { className: "auth-card-head" },
        renderBrandMark(appearance, "md"),
        el(
          "div",
          { className: "auth-title-group" },
          el("h1", { className: "auth-brand-title", text: "Data.O.G" }),
          el("div", { className: "auth-brand-sub", text: "离线试用期 · 本地加密授权登录" })
        )
      ),
      el("div", { className: "mac-segmented auth-mode-seg", role: "tablist" }, segCodeBtn, segAccBtn),
      alertBanner,
      this.loginMode === "activation_code" ? activationBody : accountBody,
      contactFooter
    );

    this.mountEl.hidden = false;
    clearEl(this.mountEl, card);
    if (this.loginMode === "activation_code") {
      setTimeout(() => codeInput.focus(), 20);
    }
  }

  openLicenseDetailModal() {
    const lic = store.state.license || this.statusData || {};
    const device = lic.device || {};
    const contact = lic.contact || {};
    const expDate = lic.expires_date || "-";
    const days = lic.days_remaining ?? 0;

    const renewInput = el("textarea", {
      className: "mac-input mono auth-code-textarea",
      rows: "2",
      placeholder: "输入新的 DOG1-... 激活码即可在线续期或更换...",
    });
    const renewErr = el("div", { className: "auth-inline-error", hidden: true });

    const body = el(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "12px" } },
      el(
        "div",
        { className: "auth-success-hero" },
        el("span", { className: "auth-success-icon" }, svgIcon("key", 20)),
        el(
          "div",
          null,
          el("div", { className: "auth-success-headline", text: `当前激活码可用日期至 ${expDate}` }),
          el("div", {
            className: "auth-success-sub",
            text: `${lic.scope_label || "本地授权"} · 剩余 ${days} 天`,
          })
        )
      ),
      el(
        "div",
        { className: "auth-success-meta" },
        el("div", { className: "auth-meta-row" }, el("span", { text: "当前激活码" }), el("code", { className: "mono", text: lic.masked_code || "-" })),
        el("div", { className: "auth-meta-row" }, el("span", { text: "本机机器码" }), el("code", { className: "mono", text: device.machine_code || "-" })),
        el("div", { className: "auth-meta-row" }, el("span", { text: "本机 IP" }), el("code", { className: "mono", text: device.primary_ip || "-" }))
      ),
      el(
        "div",
        { style: { display: "flex", gap: "8px", alignItems: "center", justifyContent: "space-between" } },
        el("span", { style: { fontSize: "11.5px", color: "var(--os-muted)" }, text: contact.notice || "如需激活码，烦请联系 莫政辉  手机 131-6111-4561  邮箱 artxom@outlook.com" }),
        el(
          "button",
          {
            type: "button",
            className: "mac-btn mac-btn-sm",
            onClick: async () => {
              if (!device.request_code) return;
              await copyText(device.request_code);
              Toast.success("已复制设备申请码");
            },
          },
          svgIcon("copy", 12),
          el("span", { text: "复制申请码" })
        )
      ),
      el("div", { style: { fontSize: "12px", fontWeight: "600" }, text: "更换 / 续期激活码" }),
      renewInput,
      renewErr
    );

    const lockBtn = el(
      "button",
      {
        type: "button",
        className: "mac-btn mac-btn-danger",
        onClick: async () => {
          try {
            const res = await authApi.lock();
            Modal.close("locked");
            this.onLocked?.(res.license || res);
          } catch (err) {
            Toast.error(err.message);
          }
        },
      },
      svgIcon("lock", 13),
      el("span", { text: "锁定并退回登录页" })
    );

    const renewBtn = el("button", {
      type: "button",
      className: "mac-btn mac-btn-primary",
      text: "更新激活码",
      onClick: async () => {
        const val = (renewInput.value || "").trim();
        if (!val) {
          renewErr.textContent = "请输入新的激活码";
          renewErr.hidden = false;
          return;
        }
        renewErr.hidden = true;
        try {
          const res = await authApi.activate({
            login_mode: "activation_code",
            activation_code: val,
          });
          const nextLic = res.license || {};
          store.updateLicense(nextLic);
          Modal.close("renewed");
          showActivationSuccessNotice(nextLic);
        } catch (err) {
          renewErr.textContent = err.message || "更新失败";
          renewErr.hidden = false;
        }
      },
    });

    const closeBtn = el("button", {
      type: "button",
      className: "mac-btn",
      text: "关闭",
      onClick: () => Modal.close("cancel"),
    });

    Modal.open({
      title: "授权信息与安全锁定",
      width: "520px",
      body,
      footer: [lockBtn, closeBtn, renewBtn],
    });
  }
}
