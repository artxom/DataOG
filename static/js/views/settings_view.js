/**
 * Data.O.G 桌面模块 1：系统配置 (SettingsView)
 * 包含四大分区：
 * 1. 外观与图标 — 内置壁纸预设、上传本地桌面壁纸、上传 Data.O.G 软件主图标与四大模块图标（本地持久化）
 * 2. 数据库连接 — 全局共享数据源连接管理
 * 3. JDBC 驱动 — 上传与管理自定义 .jar 驱动包
 * 4. AI 模型 — Cherry Studio 风格多服务商接入（DeepSeek / Google Gemini / OpenAI 兼容网关）
 */

import { platformApi } from "../core/api.js";
import {
  badge,
  clearEl,
  el,
  pickLocalFile,
  renderAppSquircle,
  renderBrandMark,
  statusDot,
} from "../core/dom.js";
import { DESKTOP_APPS, store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";
import { ConnectionsView, renderDriversSection } from "./connections_view.js";

const WALLPAPER_PRESETS = [
  { id: "default", label: "银昼 / 星夜", bg: "var(--wp-default)" },
  { id: "sonoma", label: "Sonoma", bg: "var(--wp-sonoma)" },
  { id: "sequoia", label: "Sequoia", bg: "var(--wp-sequoia)" },
  { id: "aurora", label: "极光深海", bg: "var(--wp-aurora)" },
];

const SECTIONS = [
  { id: "appearance", label: "外观与图标" },
  { id: "connections", label: "数据库连接" },
  { id: "drivers", label: "JDBC 驱动" },
  { id: "llm", label: "AI 模型" },
];

export class SettingsView {
  constructor() {
    this.activeSection = "appearance";
    this.connectionsPane = new ConnectionsView({ onChange: () => this.render() });
    this.selectedProviderId = "deepseek";
    this.llmDraft = null;
    this.newModelInputText = "";
    this.refreshHeader = null;

    this.root = el("div", { className: "mac-split-view" });
    this._unsub = store.subscribe((_state, reason) => {
      if (["appearance", "llm", "connections", "drivers"].includes(reason)) {
        this.render();
      }
    });
  }

  destroy() {
    this._unsub?.();
  }

  getHeaderParts() {
    const secTitle = SECTIONS.find((s) => s.id === this.activeSection)?.label || "系统配置";
    store.setAppSummary("settings", secTitle);
    return {
      center: el("span", { style: { fontSize: "12.5px", fontWeight: "600", color: "var(--os-text-secondary)" }, text: secTitle }),
      right: null,
    };
  }

  mount(container, context = null, refreshHeader = null) {
    this.refreshHeader = refreshHeader;
    if (context?.section && SECTIONS.some((s) => s.id === context.section)) {
      this.activeSection = context.section;
    }
    if (context?.createNew) {
      this.activeSection = "connections";
      this.connectionsPane.resetToNew();
    }
    clearEl(container, this.root);
    this._syncLlmDraftFromStore();
    this.render();
    this.refreshHeader?.();
  }

  _syncLlmDraftFromStore(forceId = "") {
    const providers = store.state.llmProviders || [];
    if (!providers.length) return;
    const targetId = forceId || this.selectedProviderId || providers[0].provider_id;
    const found = providers.find((p) => p.provider_id === targetId) || providers[0];
    this.selectedProviderId = found.provider_id;
    this.llmDraft = {
      provider_id: found.provider_id,
      name: found.name || "",
      provider_type: found.provider_type || "OPENAI_COMPATIBLE",
      enabled: Boolean(found.enabled),
      base_url: found.base_url || "",
      api_key: "",
      has_api_key: Boolean(found.has_api_key),
      api_key_masked: found.api_key_masked || "",
      default_model: found.default_model || "",
      models: [...(found.models || [])],
      is_default: Boolean(found.is_default),
    };
  }

  async _uploadImageSlot(slot) {
    const file = await pickLocalFile("image/png,image/jpeg,image/webp,image/gif");
    if (!file) return;
    try {
      const res = await platformApi.uploadAppearanceAsset(file, slot);
      store.updateAppearance(res.appearance);
      Toast.success("已保存至本地");
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async _resetSlot(slot) {
    try {
      const res = await platformApi.resetAppearance(slot);
      store.updateAppearance(res.appearance);
      Toast.success("已恢复默认");
    } catch (err) {
      Toast.error(err.message);
    }
  }

  _renderAppearanceSection() {
    const app = store.state.appearance || {};
    const wpId = app.wallpaper_id || "default";
    const wpFit = app.wallpaper_fit || "cover";
    const hasCustomWp = Boolean(app.custom_wallpaper_url);

    const swatches = WALLPAPER_PRESETS.map((p) =>
      el(
        "button",
        {
          type: "button",
          className: `wp-swatch ${wpId === p.id ? "is-active" : ""}`,
          style: { backgroundImage: p.bg },
          onClick: async () => {
            try {
              const res = await platformApi.updateAppearance({ wallpaper_id: p.id });
              store.updateAppearance(res.appearance);
            } catch (err) {
              Toast.error(err.message);
            }
          },
        },
        el("span", { className: "wp-swatch-label", text: p.label })
      )
    );

    if (hasCustomWp) {
      swatches.push(
        el(
          "button",
          {
            type: "button",
            className: `wp-swatch ${wpId === "custom" ? "is-active" : ""}`,
            style: {
              backgroundImage: `url("${app.custom_wallpaper_url}")`,
              backgroundSize: "cover",
              backgroundPosition: "center",
            },
            onClick: async () => {
              try {
                const res = await platformApi.updateAppearance({ wallpaper_id: "custom" });
                store.updateAppearance(res.appearance);
              } catch (err) {
                Toast.error(err.message);
              }
            },
          },
          el("span", { className: "wp-swatch-label", text: "本地图片" })
        )
      );
    }

    const fitGroup = el(
      "div",
      { className: "seg-group" },
      [
        { id: "cover", label: "铺满" },
        { id: "contain", label: "适应" },
      ].map((opt) =>
        el("button", {
          type: "button",
          className: `seg-btn ${wpFit === opt.id ? "is-active" : ""}`,
          text: opt.label,
          onClick: async () => {
            try {
              const res = await platformApi.updateAppearance({ wallpaper_fit: opt.id });
              store.updateAppearance(res.appearance);
            } catch (err) {
              Toast.error(err.message);
            }
          },
        })
      )
    );

    const wpBox = el(
      "div",
      { className: "mac-group-box mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: "桌面壁纸" }),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px" } },
          fitGroup,
          el("button", {
            type: "button",
            className: "mac-btn mac-btn-sm mac-btn-primary",
            text: "上传壁纸",
            onClick: () => this._uploadImageSlot("wallpaper"),
          }),
          hasCustomWp
            ? el("button", {
                type: "button",
                className: "mac-btn mac-btn-sm",
                text: "恢复默认",
                onClick: () => this._resetSlot("wallpaper"),
              })
            : null
        )
      ),
      el("div", { className: "wp-grid" }, swatches)
    );

    // 软件主图标 + 四大模块图标
    const brandCard = el(
      "div",
      { className: "icon-slot-card" },
      el(
        "div",
        { className: "icon-slot-info" },
        renderBrandMark(app, "md"),
        el(
          "div",
          {},
          el("div", { style: { fontWeight: "600", fontSize: "12.5px" }, text: "Data.O.G 主图标" }),
          el("div", { style: { fontSize: "11px", color: "var(--os-muted)" }, text: app.brand_icon_url ? "已自定义" : "默认图标" })
        )
      ),
      el(
        "div",
        { style: { display: "flex", gap: "5px" } },
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: "上传",
          onClick: () => this._uploadImageSlot("brand"),
        }),
        app.brand_icon_url
          ? el("button", {
              type: "button",
              className: "btn btn-xs btn-ghost",
              text: "重置",
              onClick: () => this._resetSlot("brand"),
            })
          : null
      )
    );

    const moduleCards = DESKTOP_APPS.map((m) => {
      const hasCustom = Boolean(app.app_icons?.[m.id]);
      return el(
        "div",
        { className: "icon-slot-card" },
        el(
          "div",
          { className: "icon-slot-info" },
          renderAppSquircle(m.id, app, "md"),
          el(
            "div",
            {},
            el("div", { style: { fontWeight: "600", fontSize: "12.5px" }, text: m.title }),
            el("div", { style: { fontSize: "11px", color: "var(--os-muted)" }, text: hasCustom ? "已自定义" : "默认图标" })
          )
        ),
        el(
          "div",
          { style: { display: "flex", gap: "5px" } },
          el("button", {
            type: "button",
            className: "btn btn-xs",
            text: "上传",
            onClick: () => this._uploadImageSlot(`app:${m.id}`),
          }),
          hasCustom
            ? el("button", {
                type: "button",
                className: "btn btn-xs btn-ghost",
                text: "重置",
                onClick: () => this._resetSlot(`app:${m.id}`),
              })
            : null
        )
      );
    });

    const iconsBox = el(
      "div",
      { className: "mac-group-box mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: "软件与模块图标" }),
        el("button", {
          type: "button",
          className: "mac-btn mac-btn-sm",
          text: "全部恢复默认",
          onClick: () => this._resetSlot("all_icons"),
        })
      ),
      el("div", { className: "icon-customizer-grid" }, brandCard, ...moduleCards)
    );

    return el("div", { className: "mac-section", style: { gap: "16px" } }, wpBox, iconsBox);
  }

  _renderLlmSection() {
    const providers = store.state.llmProviders || [];
    if (!this.llmDraft && providers.length) {
      this._syncLlmDraftFromStore();
    }
    const d = this.llmDraft || {
      provider_id: "",
      name: "新服务商",
      provider_type: "OPENAI_COMPATIBLE",
      enabled: true,
      base_url: "https://api.openai.com/v1",
      api_key: "",
      has_api_key: false,
      api_key_masked: "",
      default_model: "",
      models: [],
      is_default: false,
    };
    const isBuiltin = ["deepseek", "gemini", "custom"].includes(d.provider_id);

    const providerList = el(
      "div",
      {
        style: {
          width: "190px",
          flexShrink: "0",
          display: "flex",
          flexDirection: "column",
          gap: "4px",
        },
      },
      ...providers.map((p) =>
        el(
          "button",
          {
            type: "button",
            className: `mac-nav-item ${p.provider_id === d.provider_id ? "is-active" : ""}`,
            onClick: () => {
              this._syncLlmDraftFromStore(p.provider_id);
              this.render();
            },
          },
          el(
            "span",
            { style: { display: "inline-flex", alignItems: "center", gap: "7px", overflow: "hidden" } },
            el("span", { className: `dot ${p.has_api_key && p.enabled ? "dot-success" : "dot-muted"}` }),
            el("span", { style: { overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }, text: p.name })
          ),
          p.is_default ? badge("默认", "info") : null
        )
      ),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-sm",
        style: { marginTop: "6px" },
        text: "+ 新增兼容端点",
        onClick: () => {
          this.selectedProviderId = "";
          this.llmDraft = {
            provider_id: "",
            name: "自定义端点",
            provider_type: "OPENAI_COMPATIBLE",
            enabled: true,
            base_url: "https://api.siliconflow.cn/v1",
            api_key: "",
            has_api_key: false,
            api_key_masked: "",
            default_model: "deepseek-ai/DeepSeek-V3",
            models: ["deepseek-ai/DeepSeek-V3"],
            is_default: false,
          };
          this.render();
        },
      })
    );

    const buildPayload = (extra = {}) => ({
      provider_id: d.provider_id || undefined,
      name: d.name.trim(),
      provider_type: d.provider_type,
      enabled: Boolean(d.enabled),
      base_url: d.base_url.trim(),
      api_key: d.api_key || undefined,
      default_model: d.default_model.trim(),
      models: d.models,
      is_default: Boolean(d.is_default),
      ...extra,
    });

    const modelChips = (d.models || []).map((m) =>
      el(
        "span",
        {
          className: `filter-pill ${d.default_model === m ? "is-active" : ""}`,
          title: "点击设为默认模型",
          onClick: () => {
            d.default_model = m;
            this.render();
          },
        },
        el("span", { className: "mono", text: m }),
        el("span", {
          text: "×",
          style: { marginLeft: "4px", opacity: "0.7" },
          title: "移除模型",
          onClick: (ev) => {
            ev.stopPropagation();
            d.models = d.models.filter((x) => x !== m);
            if (d.default_model === m) d.default_model = d.models[0] || "";
            this.render();
          },
        })
      )
    );

    const addModelInput = el("input", {
      className: "mac-input mac-input-sm mono",
      style: { width: "165px" },
      placeholder: "输入模型 ID 回车添加",
      value: this.newModelInputText,
      onInput: (e) => (this.newModelInputText = e.target.value),
      onKeydown: (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          const val = this.newModelInputText.trim();
          if (val && !d.models.includes(val)) {
            d.models.push(val);
            if (!d.default_model) d.default_model = val;
            this.newModelInputText = "";
            this.render();
          }
        }
      },
    });

    const detailBox = el(
      "div",
      { className: "mac-group-box mac-section", style: { flex: "1", minWidth: "0" } },
      el(
        "div",
        { className: "mac-section-header" },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "10px", flexWrap: "wrap" } },
          el("input", {
            className: "mac-input",
            style: { width: "170px", fontWeight: "600" },
            value: d.name,
            readonly: isBuiltin,
            onInput: (e) => (d.name = e.target.value),
          }),
          el(
            "label",
            { style: { display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "12px" } },
            el("input", {
              type: "checkbox",
              checked: Boolean(d.enabled),
              onChange: (e) => (d.enabled = e.target.checked),
            }),
            el("span", { text: "启用" })
          ),
          el(
            "label",
            { style: { display: "inline-flex", alignItems: "center", gap: "5px", fontSize: "12px" } },
            el("input", {
              type: "checkbox",
              checked: Boolean(d.is_default),
              onChange: (e) => (d.is_default = e.target.checked),
            }),
            el("span", { text: "默认服务商" })
          )
        ),
        !isBuiltin && d.provider_id
          ? el("button", {
              type: "button",
              className: "btn btn-xs btn-danger",
              text: "删除",
              onClick: async () => {
                const ok = await Modal.confirm({
                  title: "删除服务商",
                  message: `确定删除「${d.name}」吗？`,
                  confirmText: "删除",
                  danger: true,
                });
                if (!ok) return;
                try {
                  const res = await platformApi.deleteLlmProvider(d.provider_id);
                  store.updateLlmState(res.providers, res.available_models);
                  this._syncLlmDraftFromStore("deepseek");
                  Toast.success("已删除");
                  this.render();
                } catch (err) {
                  Toast.error(err.message);
                }
              },
            })
          : null
      ),
      el(
        "div",
        { className: "form-grid" },
        el(
          "div",
          { className: "form-field span-2" },
          el("label", { className: "form-label", text: "API 地址 (OpenAI 兼容 Base URL)" }),
          el("input", {
            className: "mac-input mono",
            value: d.base_url,
            placeholder: "https://api.deepseek.com",
            onInput: (e) => (d.base_url = e.target.value),
          })
        ),
        el(
          "div",
          { className: "form-field span-2" },
          el(
            "div",
            { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            el("label", { className: "form-label", text: "API Key" }),
            d.has_api_key
              ? el("button", {
                  type: "button",
                  className: "btn btn-xs btn-ghost",
                  text: "清空已存密钥",
                  onClick: async () => {
                    try {
                      const res = await platformApi.saveLlmProvider(buildPayload({ clear_api_key: true }));
                      store.updateLlmState(res.providers, res.available_models);
                      this._syncLlmDraftFromStore(res.provider.provider_id);
                      Toast.success("已清空密钥");
                      this.render();
                    } catch (err) {
                      Toast.error(err.message);
                    }
                  },
                })
              : null
          ),
          el("input", {
            className: "mac-input mono",
            type: "password",
            autocomplete: "new-password",
            value: d.api_key,
            placeholder: d.has_api_key ? `已保存 (${d.api_key_masked})，留空保持不变` : "sk-...",
            onInput: (e) => (d.api_key = e.target.value),
          })
        ),
        el(
          "div",
          { className: "form-field span-2" },
          el(
            "div",
            { style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
            el("label", { className: "form-label", text: "默认模型" }),
            el("button", {
              type: "button",
              className: "btn btn-xs",
              text: "拉取远端模型",
              onClick: async () => {
                try {
                  const res = await platformApi.fetchLlmModels(buildPayload());
                  d.models = res.models || d.models;
                  d.default_model = res.default_model || d.default_model;
                  store.updateLlmState(res.providers, res.available_models);
                  Toast.success(res.message || "模型列表已同步");
                  this.render();
                } catch (err) {
                  Toast.error(err.message);
                }
              },
            })
          ),
          el("input", {
            className: "mac-input mono",
            value: d.default_model,
            placeholder: "deepseek-chat / gemini-2.5-flash",
            onInput: (e) => (d.default_model = e.target.value),
          })
        )
      ),
      el(
        "div",
        { className: "mac-section", style: { marginTop: "4px" } },
        el("label", { className: "form-label", text: "可用模型列表（点击设为默认）" }),
        el("div", { className: "filter-pills" }, ...modelChips, addModelInput)
      ),
      el(
        "div",
        { style: { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "10px" } },
        el("button", {
          type: "button",
          className: "mac-btn",
          text: "连通检测",
          onClick: async () => {
            try {
              const res = await platformApi.testLlmProvider(buildPayload());
              Toast.success(res.message || "连通正常");
            } catch (err) {
              Toast.error(err.message);
            }
          },
        }),
        el("button", {
          type: "button",
          className: "mac-btn mac-btn-primary",
          text: "保存配置",
          onClick: async () => {
            try {
              const res = d.provider_id
                ? await platformApi.updateLlmProvider(d.provider_id, buildPayload())
                : await platformApi.saveLlmProvider(buildPayload());
              store.updateLlmState(res.providers, res.available_models);
              this._syncLlmDraftFromStore(res.provider.provider_id);
              Toast.success("已保存");
              this.render();
            } catch (err) {
              Toast.error(err.message);
            }
          },
        })
      )
    );

    return el(
      "div",
      { style: { display: "flex", gap: "16px", alignItems: "flex-start" } },
      providerList,
      detailBox
    );
  }

  render() {
    const sidebar = el(
      "aside",
      { className: "mac-sidebar" },
      SECTIONS.map((s) =>
        el(
          "button",
          {
            type: "button",
            className: `mac-nav-item ${this.activeSection === s.id ? "is-active" : ""}`,
            onClick: () => {
              this.activeSection = s.id;
              this.render();
              this.refreshHeader?.();
            },
          },
          el("span", { text: s.label }),
          s.id === "connections" && store.state.connections.length
            ? badge(store.state.connections.length, "neutral")
            : null
        )
      )
    );

    let paneBody;
    if (this.activeSection === "appearance") {
      paneBody = this._renderAppearanceSection();
    } else if (this.activeSection === "connections") {
      this.connectionsPane.render();
      paneBody = this.connectionsPane.root;
    } else if (this.activeSection === "drivers") {
      paneBody = renderDriversSection(() => this.render());
    } else {
      paneBody = this._renderLlmSection();
    }

    clearEl(
      this.root,
      sidebar,
      el("div", { className: "mac-pane mac-pane-padded" }, paneBody)
    );
  }
}
