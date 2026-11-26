/**
 * Data.O.G 4.0 系统配置子面板：应用插件中心 (PluginsView)
 *
 * 架构红线：
 * 1. 100% 使用安全 DOM 工厂 (el / clearEl)，零 innerHTML，零阻塞弹窗；
 * 2. 严禁展示或推断本机私有保险箱物理路径；
 * 3. 安装先 inspect 验签并弹 <dialog> 确认，卸载默认保留插件业务表并可选主动清理。
 */

import {
  badge,
  clearEl,
  el,
  pickLocalFile,
  svgIcon,
} from "../core/dom.js";
import { platformApi } from "../core/platform_api.js";
import { store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";

export class PluginsView {
  constructor({ onChange = null } = {}) {
    this.onChange = onChange;
    this.status = null;
    this.loading = false;
    this.busyAction = "";
    this.lastScanSkipped = [];
    this.showBuiltins = false;

    this.root = el("div", { className: "ws-view-root" });
  }

  destroy() {}

  async refreshFromServer() {
    this.loading = true;
    this.render();
    try {
      const res = await platformApi.getPluginStatus();
      if (res?.plugin_hub) {
        this.status = res.plugin_hub;
      }
    } catch (err) {
      Toast.error(err.message || "获取应用插件状态失败");
    } finally {
      this.loading = false;
      this.render();
      this.onChange?.();
    }
  }

  async handleUploadPlugin() {
    if (this.busyAction) return;
    const file = await pickLocalFile(".dogapp,.zip,.dat");
    if (!file) return;

    this.busyAction = "inspect";
    this.render();
    let preview = null;
    try {
      preview = await platformApi.inspectPluginPackage(file);
    } catch (err) {
      this.busyAction = "";
      this.render();
      Toast.error(err.message || "插件包签名或格式校验失败");
      return;
    } finally {
      this.busyAction = "";
      this.render();
    }

    const confirmed = await new Promise((resolve) => {
      const body = el(
        "div",
        { style: { display: "grid", gap: "8px", fontSize: "12.5px" } },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
          el("strong", { text: `${preview.name || preview.plugin_id}` }),
          badge(`v${preview.version}`, "info"),
          badge(`ABI v${preview.api_version}`, "neutral"),
          preview.compatible ? badge("数字签名验签通过", "success") : badge("版本不兼容", "danger")
        ),
        el("div", { className: "muted mono", text: `plugin_id: ${preview.plugin_id} | 适用平台: v${preview.min_platform_version}+` }),
        preview.summary ? el("p", { style: { margin: "0" }, text: preview.summary }) : null,
        preview.compatibility_message
          ? el("div", { style: { color: "var(--status-danger)" }, text: preview.compatibility_message })
          : null,
        el("div", {
          className: "muted",
          text: `加密载荷: ${preview.cipher || "DGAP"} · 包含 ${preview.file_count || 0} 个已签名文件。确认后将安装至本机保险箱，重启 Data.O.G 后生效。`,
        })
      );
      const cancelBtn = el("button", {
        type: "button",
        className: "mac-btn",
        text: "取消",
        onClick: () => Modal.close("cancel"),
      });
      const okBtn = el("button", {
        type: "button",
        className: "mac-btn mac-btn-primary",
        disabled: !preview.compatible,
        text: "确认安装",
        onClick: () => Modal.close("confirm"),
      });
      Modal.open({
        title: "安装 Data.O.G 4.0 应用插件",
        body,
        footer: [cancelBtn, okBtn],
        width: "520px",
        onClose: (val) => resolve(val === "confirm"),
      });
    });

    if (!confirmed) return;

    this.busyAction = "upload";
    this.render();
    try {
      const res = await platformApi.uploadPluginPackage(file);
      if (res?.plugin_hub) {
        this.status = res.plugin_hub;
      }
      Toast.success(res?.message || "插件安装成功，重启 Data.O.G 后生效");
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "安装插件包失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  async handleScanDir() {
    if (this.busyAction) return;
    this.busyAction = "scan";
    this.lastScanSkipped = [];
    this.render();
    try {
      const res = await platformApi.scanPluginPackages();
      if (res?.plugin_hub) {
        this.status = res.plugin_hub;
      }
      this.lastScanSkipped = Array.isArray(res?.skipped) ? res.skipped : [];
      Toast.success(res?.message || "扫描完成");
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "扫描 plugins 目录失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  async handleToggleEnabled(plug, nextEnabled) {
    if (this.busyAction) return;
    this.busyAction = `toggle:${plug.plugin_id}`;
    this.render();
    try {
      const res = nextEnabled
        ? await platformApi.enablePlugin(plug.plugin_id)
        : await platformApi.disablePlugin(plug.plugin_id);
      if (res?.plugin_hub) this.status = res.plugin_hub;
      Toast.success(res?.message || "操作成功，重启后生效");
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "更新插件启停状态失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  async handleRollback(plug) {
    if (this.busyAction || !plug.can_rollback) return;
    const ok = await Modal.confirm({
      title: "回退插件版本",
      message: `确定将插件「${plug.name}」回退至上一版本 v${plug.previous_version} 吗？重启 Data.O.G 后生效。`,
      confirmText: "确认回退",
    });
    if (!ok) return;
    this.busyAction = `rollback:${plug.plugin_id}`;
    this.render();
    try {
      const res = await platformApi.rollbackPlugin(plug.plugin_id);
      if (res?.plugin_hub) this.status = res.plugin_hub;
      Toast.success(res?.message || "已切换回上一版本");
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "回退插件版本失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  async handleUninstall(plug) {
    if (this.busyAction) return;
    const result = await new Promise((resolve) => {
      const purgeChk = el("input", { type: "checkbox", checked: false });
      const body = el(
        "div",
        { style: { display: "grid", gap: "10px", fontSize: "12.5px" } },
        el("p", {
          style: { margin: "0" },
          text: `确定要卸载外置应用插件「${plug.name}」(${plug.plugin_id}) 吗？`,
        }),
        el(
          "label",
          {
            style: {
              display: "flex",
              alignItems: "center",
              gap: "8px",
              padding: "8px 10px",
              borderRadius: "8px",
              border: "1px solid var(--os-border)",
              background: "var(--os-subtle-bg)",
              cursor: "pointer",
            },
          },
          purgeChk,
          el("span", {
            text: "同时清理该插件在工作区与本机私有库中创建的业务数据表（不可恢复）",
          })
        )
      );
      const cancelBtn = el("button", {
        type: "button",
        className: "mac-btn",
        autofocus: true,
        text: "取消",
        onClick: () => Modal.close("cancel"),
      });
      const delBtn = el("button", {
        type: "button",
        className: "mac-btn btn-danger",
        text: "确认卸载",
        onClick: () => Modal.close("confirm"),
      });
      Modal.open({
        title: "卸载应用插件",
        body,
        footer: [cancelBtn, delBtn],
        width: "480px",
        onClose: (val) => resolve({ confirmed: val === "confirm", purgeData: Boolean(purgeChk.checked) }),
      });
    });

    if (!result.confirmed) return;

    this.busyAction = `uninstall:${plug.plugin_id}`;
    this.render();
    try {
      const res = await platformApi.uninstallPlugin(plug.plugin_id, {
        purgeData: result.purgeData,
      });
      if (res?.plugin_hub) this.status = res.plugin_hub;
      Toast.success(res?.message || "插件卸载已登记");
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "卸载插件失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  _renderPluginCard(plug, busy) {
    const badges = [
      badge(`v${plug.running_version || plug.active_version || "0.0.0"}`, "neutral"),
    ];
    if (plug.dev_mode) {
      badges.push(badge("开发热挂 (免签)", "info"));
    } else if (plug.pending_action === "uninstall") {
      badges.push(badge("待重启卸载", "warn"));
    } else if (plug.quarantined) {
      badges.push(badge("启动异常已隔离", "danger"));
    } else if (plug.is_running) {
      badges.push(badge("运行中", "success"));
    } else if (!plug.enabled) {
      badges.push(badge("已停用", "neutral"));
    } else {
      badges.push(badge("待重启生效", "warn"));
    }

    const actions = [];
    if (!plug.dev_mode) {
      if (plug.quarantined) {
        actions.push(
          el("button", {
            type: "button",
            className: "btn btn-xs btn-primary",
            disabled: busy,
            text: "解除隔离重试",
            onClick: () => this.handleToggleEnabled(plug, true),
          })
        );
      } else if (plug.enabled) {
        actions.push(
          el("button", {
            type: "button",
            className: "btn btn-xs",
            disabled: busy || plug.pending_action === "uninstall",
            text: "停用",
            onClick: () => this.handleToggleEnabled(plug, false),
          })
        );
      } else {
        actions.push(
          el("button", {
            type: "button",
            className: "btn btn-xs btn-primary",
            disabled: busy,
            text: "启用",
            onClick: () => this.handleToggleEnabled(plug, true),
          })
        );
      }
      if (plug.can_rollback) {
        actions.push(
          el("button", {
            type: "button",
            className: "btn btn-xs",
            disabled: busy,
            text: `回退 v${plug.previous_version}`,
            onClick: () => this.handleRollback(plug),
          })
        );
      }
      actions.push(
        el("button", {
          type: "button",
          className: "btn btn-xs btn-danger",
          disabled: busy,
          text: "卸载",
          onClick: () => this.handleUninstall(plug),
        })
      );
    }

    return el(
      "div",
      { className: "ws-arch-card", style: { display: "grid", gap: "6px" } },
      el(
        "div",
        { style: { display: "flex", alignItems: "center", justifyContent: "space-between", gap: "8px", flexWrap: "wrap" } },
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
          el("strong", { text: plug.name || plug.plugin_id }),
          el("span", { className: "mono muted", style: { fontSize: "11.5px" }, text: plug.plugin_id }),
          ...badges
        ),
        el("div", { style: { display: "flex", gap: "6px", flexWrap: "wrap" } }, ...actions)
      ),
      plug.summary ? el("div", { className: "muted", style: { fontSize: "12px" }, text: plug.summary }) : null,
      plug.quarantined && plug.quarantine_reason
        ? el("div", {
            style: {
              fontSize: "11.5px",
              color: "var(--status-danger)",
              padding: "6px 8px",
              borderRadius: "6px",
              background: "color-mix(in srgb, var(--status-danger) 8%, transparent)",
            },
            text: `隔离原因: ${plug.quarantine_reason}`,
          })
        : null
    );
  }

  render() {
    const bootSum = store.state.pluginSummary || {};
    const st = this.status || {
      platform_abi: store.state.platformAbi || bootSum.platform_abi || "4.0",
      platform_version: store.state.version || "4.0.0",
      installed_count: bootSum.installed_count || 0,
      active_count: bootSum.active_count || 0,
      quarantined_count: bootSum.quarantined_count || 0,
      dev_count: bootSum.dev_count || 0,
      restart_required: Boolean(bootSum.restart_required),
      plugins: [],
    };
    const busy = Boolean(this.busyAction);
    const plugins = Array.isArray(st.plugins) ? st.plugins : [];

    const kpiGrid = el(
      "div",
      { className: "ws-arch-grid", style: { marginBottom: "10px" } },
      el(
        "div",
        { className: "ws-arch-card" },
        el("div", { className: "ws-kpi-lbl", text: "平台插件契约 (ABI)" }),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px" } },
          el("span", { className: "mono", style: { fontSize: "16px", fontWeight: "700" }, text: `v${st.platform_abi}` }),
          badge(`运行中 ${st.active_count} 个`, "success"),
          st.quarantined_count > 0 ? badge(`隔离 ${st.quarantined_count}`, "danger") : null
        )
      ),
      el(
        "div",
        { className: "ws-arch-card" },
        el("div", { className: "ws-kpi-lbl", text: "已安装外置应用插件" }),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px" } },
          el("span", { className: "mono", style: { fontSize: "16px", fontWeight: "700" }, text: `${st.installed_count} 个` }),
          st.dev_count > 0 ? badge(`开发热挂 ${st.dev_count}`, "info") : null,
          st.restart_required ? badge("待重启生效", "warn") : null
        )
      )
    );

    const restartBanner = st.restart_required
      ? el(
          "div",
          {
            className: "ws-security-banner",
            style: {
              marginBottom: "10px",
              borderColor: "color-mix(in srgb, var(--status-warn, #f59e0b) 45%, var(--os-border))",
              background: "color-mix(in srgb, var(--status-warn, #f59e0b) 10%, var(--os-subtle-bg))",
              color: "var(--status-warn, #b45309)",
            },
          },
          svgIcon("refresh", 16),
          el(
            "div",
            {},
            el("div", { className: "ws-security-banner-title", text: "检测到外置插件变更待生效，请关闭并重启 Data.O.G" }),
            el("div", {
              className: "ws-security-banner-sub",
              text: "已验签插件将在下次启动时隔离装载接入桌面 Dock 与平台路由；若插件初始化报错会自动隔离而不影响系统主功能。",
            })
          )
        )
      : null;

    const actionBar = el(
      "div",
      { className: "ws-action-bar", style: { marginBottom: "12px" } },
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-primary",
          disabled: busy,
          onClick: () => this.handleUploadPlugin(),
        },
        svgIcon("upload", 14),
        el("span", {
          text:
            this.busyAction === "upload"
              ? "正在安装..."
              : this.busyAction === "inspect"
              ? "正在验签..."
              : "安装插件包 (.dogapp / .zip / .dat)",
        })
      ),
      el(
        "button",
        {
          type: "button",
          className: "mac-btn",
          disabled: busy,
          onClick: () => this.handleScanDir(),
        },
        svgIcon("search", 14),
        el("span", { text: this.busyAction === "scan" ? "扫描中..." : "扫描安装目录 plugins/" })
      )
    );

    const skippedBox = this.lastScanSkipped.length
      ? el(
          "div",
          { className: "ws-arch-card", style: { marginBottom: "12px" } },
          el("div", { className: "ws-arch-card-head", text: `扫描跳过的候选包 (${this.lastScanSkipped.length})` }),
          ...this.lastScanSkipped.map((item) =>
            el("div", {
              className: "muted",
              style: { fontSize: "12px" },
              text: `${item.filename}: ${item.reason}`,
            })
          )
        )
      : null;

    const listBox = el(
      "div",
      { className: "mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: "已安装外置应用插件" }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          disabled: this.loading || busy,
          text: this.loading ? "刷新中..." : "刷新状态",
          onClick: () => this.refreshFromServer(),
        })
      ),
      plugins.length
        ? el("div", { style: { display: "grid", gap: "8px" } }, ...plugins.map((p) => this._renderPluginCard(p, busy)))
        : el("div", {
            className: "ws-arch-card muted",
            style: { fontSize: "12.5px" },
            text: "暂未安装任何 4.0 外置应用插件。可通过独立 Antigravity Init SDK 开发并打包签名后在此安装，或将插件包放入安装目录 plugins/ 下自动吸纳。",
          })
    );

    const builtinMods = (store.state.modules || []).filter(
      (m) => !plugins.some((p) => p.plugin_id === m.module_id)
    );
    const builtinBox = el(
      "div",
      { className: "mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: `系统预装内置应用 (${builtinMods.length})` }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          text: this.showBuiltins ? "收起" : "展开查看",
          onClick: () => {
            this.showBuiltins = !this.showBuiltins;
            this.render();
          },
        })
      ),
      this.showBuiltins
        ? el(
            "div",
            { style: { display: "grid", gap: "6px" } },
            ...builtinMods.map((m) =>
              el(
                "div",
                { className: "ws-arch-card", style: { display: "flex", justifyContent: "space-between", alignItems: "center" } },
                el(
                  "div",
                  {},
                  el("strong", { text: m.title }),
                  el("span", { className: "mono muted", style: { marginLeft: "8px", fontSize: "11.5px" }, text: m.module_id })
                ),
                badge("系统预装", "neutral")
              )
            )
          )
        : null
    );

    clearEl(
      this.root,
      el(
        "div",
        { className: "mac-section" },
        el("div", { className: "mac-section-header" }, el("h3", { className: "mac-section-title", text: "应用插件平台 (v4.0)" })),
        kpiGrid,
        restartBanner,
        actionBar,
        skippedBox
      ),
      listBox,
      builtinBox
    );
  }
}
