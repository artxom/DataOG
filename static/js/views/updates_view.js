/**
 * Data.O.G 系统配置子面板：版本与增量升级管理 (UpdatesView)
 *
 * 架构红线：
 * 1. 严格禁止在前端渲染或推断任何本地私有保险箱物理路径；
 * 2. 100% 使用安全 DOM 工厂 (el / clearEl)，零 innerHTML，零阻塞弹窗；
 * 3. 双通道统一：系统目录 updates/ 覆盖投放与应用内直接上传同一份 DataOG-Update-vX.Y.Z.zip 均走 Ed25519 + SHA-256 双校验。
 */

import {
  badge,
  clearEl,
  el,
  pickLocalFile,
  statusDot,
  svgIcon,
} from "../core/dom.js";
import { platformApi } from "../core/platform_api.js";
import { store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";

export class UpdatesView {
  constructor({ onChange = null } = {}) {
    this.onChange = onChange;
    this.status = null;
    this.loading = false;
    this.busyAction = "";
    this.lastScanSkipped = [];

    this.root = el("div", { className: "ws-view-root" });
  }

  destroy() {
    // 无常驻定时器，随 SettingsView 一并销毁
  }

  async refreshFromServer() {
    this.loading = true;
    this.render();
    try {
      const res = await platformApi.getUpdateStatus();
      if (res?.update) {
        this.status = res.update;
      }
    } catch (err) {
      Toast.error(err.message || "获取版本与升级状态失败");
    } finally {
      this.loading = false;
      this.render();
      this.onChange?.();
    }
  }

  async uploadPackage() {
    if (this.busyAction) return;
    const file = await pickLocalFile(".zip,.dgp,.dat");
    if (!file) return;
    this.busyAction = "upload";
    this.render();
    try {
      const res = await platformApi.uploadUpdatePackage(file);
      if (res?.update) {
        this.status = res.update;
      }
      Toast.success(res?.message || "增量更新包已校验安装，重启 DataOG 后生效");
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "增量更新包校验或安装失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  async scanInstallDir() {
    if (this.busyAction) return;
    this.busyAction = "scan";
    this.lastScanSkipped = [];
    this.render();
    try {
      const res = await platformApi.scanUpdatePackages();
      if (res?.update) {
        this.status = res.update;
      }
      this.lastScanSkipped = Array.isArray(res?.skipped) ? res.skipped : [];
      Toast.success(res?.message || "目录扫描完成");
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "扫描 updates 目录失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  async handleRollback() {
    if (this.busyAction || !this.status?.can_rollback) return;
    const prevVer = this.status.previous_version;
    const ok = await Modal.confirm({
      title: "回退增量补丁版本",
      message: `确定将下次启动目标切换回上一稳定增量补丁 v${prevVer} 吗？切换后需重启 DataOG 生效。`,
      confirmText: "确认回退",
    });
    if (!ok) return;
    this.busyAction = "rollback";
    this.render();
    try {
      const res = await platformApi.rollbackUpdate();
      if (res?.update) this.status = res.update;
      Toast.success(res?.message || `已回退至 v${prevVer}，重启后生效`);
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "回退版本失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  async handleResetToBase() {
    if (this.busyAction || !this.status?.can_reset_to_base) return;
    const baseVer = this.status.base_version || "";
    const ok = await Modal.confirm({
      title: "恢复内置基础版本",
      message: `确定停用增量补丁并在下次启动时恢复内置基础客户端版本 v${baseVer} 吗？所有数据源、工作区、标准与 AI 配置均完整保留。`,
      confirmText: "恢复基础版",
    });
    if (!ok) return;
    this.busyAction = "reset";
    this.render();
    try {
      const res = await platformApi.resetUpdateToBase();
      if (res?.update) this.status = res.update;
      Toast.success(res?.message || "已恢复内置基础版本，重启后生效");
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "恢复内置基础版本失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  async handleDeleteRelease(rel) {
    if (this.busyAction) return;
    const ok = await Modal.confirm({
      title: "清理缓存补丁",
      message: `确定删除本地缓存的增量补丁 v${rel.version} 吗？删除后如再次需要该版本可重新导入更新包。`,
      confirmText: "删除",
      danger: true,
    });
    if (!ok) return;
    this.busyAction = `del:${rel.version}`;
    this.render();
    try {
      const res = await platformApi.deleteCachedUpdate(rel.version);
      if (res?.update) this.status = res.update;
      Toast.success(res?.message || `已清理缓存补丁 v${rel.version}`);
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message || "删除缓存补丁失败");
    } finally {
      this.busyAction = "";
      this.render();
    }
  }

  render() {
    const bootSummary = store.state.bootstrap?.update_summary || {};
    const st = this.status || {
      running_version: store.state.bootstrap?.version || bootSummary.running_version || "3.5.0",
      base_version: bootSummary.base_version || store.state.bootstrap?.version || "3.5.0",
      series: bootSummary.series || "3.5",
      min_compatible_base: `${bootSummary.series || "3.5"}.0`,
      patch_active: Boolean(bootSummary.patch_active),
      running_patch_version: bootSummary.running_patch_version || null,
      active_patch_version: bootSummary.active_patch_version || null,
      previous_version: null,
      restart_required: Boolean(bootSummary.restart_required),
      pending_target_version: bootSummary.active_patch_version || null,
      can_rollback: false,
      can_reset_to_base: Boolean(bootSummary.active_patch_version),
      retired_reason: null,
      last_rollback_reason: null,
      install_dir_pending_count: 0,
      installed_releases: [],
    };

    const releases = Array.isArray(st.installed_releases) ? st.installed_releases : [];
    const busy = Boolean(this.busyAction);

    // 1. 当前版本状态总览卡片
    const statusHeaderBadges = [
      st.patch_active ? badge("增量补丁已激活", "info") : badge("内置底座版", "neutral"),
      badge(`适用 ${st.min_compatible_base ? `v${st.min_compatible_base}+` : `${st.series}.x`} 同系列增量包`, "success"),
      st.restart_required ? badge("待重启生效", "warn") : null,
    ].filter(Boolean);

    const kpiGrid = el(
      "div",
      { className: "ws-arch-grid", style: { marginBottom: "10px" } },
      el(
        "div",
        { className: "ws-arch-card" },
        el("div", { className: "ws-kpi-lbl", text: "当前运行版本" }),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
          el("span", { className: "mono", style: { fontSize: "17px", fontWeight: "700" }, text: `v${st.running_version}` }),
          ...statusHeaderBadges
        )
      ),
      el(
        "div",
        { className: "ws-arch-card" },
        el("div", { className: "ws-kpi-lbl", text: "本地基础客户端底座" }),
        el(
          "div",
          { style: { display: "flex", alignItems: "center", gap: "8px", flexWrap: "wrap" } },
          el("span", { className: "mono", style: { fontSize: "15px", fontWeight: "600" }, text: `DataOG.exe v${st.base_version}` }),
          badge(`系列 ${st.series}.x`, "neutral")
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
            el("div", {
              className: "ws-security-banner-title",
              text: st.active_patch_version
                ? `待生效目标：增量补丁 v${st.active_patch_version}（请关闭并重新启动 DataOG 以应用）`
                : `待生效目标：恢复内置基础版本 v${st.base_version}（请关闭并重新启动 DataOG 以应用）`,
            }),
            el("div", {
              className: "ws-security-banner-sub",
              text: "新版本已验签落盘至本机私有保险箱，重启程序后将在服务极早期自动切换，原有连接、标准与工作区进度 100% 保留。",
            })
          )
        )
      : null;

    const noticeText = st.last_rollback_reason || st.retired_reason || "";
    const noticeBanner = noticeText
      ? el(
          "div",
          { className: "ws-security-banner", style: { marginBottom: "10px" } },
          svgIcon("shield", 16),
          el(
            "div",
            {},
            el("div", { className: "ws-security-banner-title", text: st.last_rollback_reason ? "安全自愈提示" : "底座版本同步提示" }),
            el("div", { className: "ws-security-banner-sub", text: noticeText })
          )
        )
      : null;

    const statusCard = el(
      "div",
      { className: "mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: "版本与升级状态" }),
        el("button", {
          type: "button",
          className: "btn btn-xs",
          disabled: this.loading || busy,
          text: this.loading ? "刷新中..." : "刷新状态",
          onClick: () => this.refreshFromServer(),
        })
      ),
      kpiGrid,
      restartBanner,
      noticeBanner
    );

    // 2. 双渠道升级操作区 + 指南
    const actionBar = el(
      "div",
      { className: "ws-action-bar", style: { marginBottom: "12px" } },
      el(
        "button",
        {
          type: "button",
          className: "mac-btn mac-btn-primary",
          disabled: busy,
          onClick: () => this.uploadPackage(),
        },
        svgIcon("upload", 14),
        el("span", { text: this.busyAction === "upload" ? "正在验签解密安装..." : "上传增量更新包 (.zip / .dgp / .dat)" })
      ),
      el(
        "button",
        {
          type: "button",
          className: "mac-btn",
          disabled: busy,
          onClick: () => this.scanInstallDir(),
        },
        svgIcon("search", 14),
        el("span", { text: this.busyAction === "scan" ? "扫描中..." : "扫描安装目录 updates/" }),
        st.install_dir_pending_count > 0 ? badge(st.install_dir_pending_count, "info") : null
      ),
      el(
        "button",
        {
          type: "button",
          className: "mac-btn",
          disabled: busy || !st.can_rollback,
          onClick: () => this.handleRollback(),
        },
        svgIcon("swap", 14),
        el("span", { text: st.previous_version ? `回退至 v${st.previous_version}` : "回退至上一版本" })
      ),
      el(
        "button",
        {
          type: "button",
          className: "mac-btn",
          disabled: busy || !st.can_reset_to_base,
          onClick: () => this.handleResetToBase(),
        },
        svgIcon("shield", 14),
        el("span", { text: "恢复内置基础版本" })
      )
    );

    const skippedList = this.lastScanSkipped.length
      ? el(
          "div",
          {
            className: "ws-arch-card",
            style: { marginBottom: "12px" },
          },
          el("div", { className: "ws-arch-card-head", text: `本次扫描跳过的候选包 (${this.lastScanSkipped.length})` }),
          ...this.lastScanSkipped.map((item) =>
            el("div", {
              style: { fontSize: "11.5px", color: "var(--os-text-secondary)" },
              text: `${item.filename}: ${item.reason}`,
            })
          )
        )
      : null;

    const guideGrid = el(
      "div",
      { className: "ws-arch-grid" },
      el(
        "div",
        { className: "ws-arch-card" },
        el("div", { className: "ws-arch-card-head" }, svgIcon("folder", 14), el("span", { text: "方式一：系统目录解压覆盖升级（邮件网关友好）" })),
        el("div", {
          className: "ws-security-banner-sub",
          text: "下载 DataOG-Update-vX.Y.Z.zip（约 1 MB），内部载荷已采用 DGP2 加密混淆信封封装，不含任何明文代码、脚本或可执行特征头，可安全通过银行/企业邮件网关传输；解压覆盖到 DataOG 目录后，下次启动自动完成 Ed25519 + HMAC 验签解密并生效。",
        })
      ),
      el(
        "div",
        { className: "ws-arch-card" },
        el("div", { className: "ws-arch-card-head" }, svgIcon("shield", 14), el("span", { text: "方式二：应用内上传 & 同系列累积规则" })),
        el("div", {
          className: "ws-security-banner-sub",
          text: `点击上方「上传增量更新包」直接上传 .zip、.dgp 或改名 .dat 文件即可自动解密校验并就地更新。同二级版本 (v${st.series}.0+) 累积通用；跨二级版本（如 ${st.series}.x → 下一特性版）请下载完整客户端。`,
        })
      )
    );

    const upgradeCard = el(
      "div",
      { className: "mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: "导入与回退控制" })
      ),
      actionBar,
      skippedList,
      guideGrid
    );

    // 3. 本地缓存补丁历史列表
    const releaseRows = releases.length
      ? releases.map((rel) => {
          const isHighlight = rel.is_active || rel.is_running;
          const statusBadges = [
            rel.is_running ? badge("当前运行", "success") : null,
            rel.is_active && !rel.is_running ? badge("下次启动生效", "info") : null,
            rel.is_active && rel.is_running ? badge("已激活", "success") : null,
            rel.is_previous ? badge("上一版本", "neutral") : null,
            rel.encrypted || Number(rel.format_version || 1) >= 2 ? badge("加密混淆包", "info") : null,
            rel.verified ? badge("签名通过", "success") : badge("校验异常", "danger"),
          ].filter(Boolean);

          const metaLine = [
            `适用 v${rel.min_base_version}+`,
            `代码 ${rel.code_file_count || 0} 文件`,
            `前端 ${rel.static_file_count || 0} 文件`,
            rel.built_at ? `构建 ${rel.built_at}` : "",
            rel.source === "install_dir" ? "来源 updates 目录" : rel.source === "upload" ? "来源 界面上传" : "",
          ]
            .filter(Boolean)
            .join(" · ");

          return el(
            "div",
            { className: `list-row-card ${isHighlight ? "ws-row-active" : ""}` },
            el(
              "div",
              { style: { minWidth: "0", flex: "1" } },
              el(
                "div",
                { style: { display: "flex", alignItems: "center", gap: "7px", flexWrap: "wrap" } },
                isHighlight
                  ? statusDot(`v${rel.version}`, rel.is_running ? "success" : "info")
                  : el("span", { className: "mono", style: { fontWeight: "600", fontSize: "13px" }, text: `v${rel.version}` }),
                ...statusBadges
              ),
              el("div", {
                className: "mono",
                style: { fontSize: "11px", color: "var(--os-muted)", marginTop: "3px" },
                text: metaLine,
              }),
              rel.summary
                ? el("div", { style: { fontSize: "11.5px", color: "var(--os-text-secondary)", marginTop: "3px" }, text: rel.summary })
                : null,
              rel.verify_error
                ? el("div", { style: { fontSize: "11.5px", color: "var(--status-danger, #dc2626)", marginTop: "2px" }, text: rel.verify_error })
                : null
            ),
            el(
              "div",
              { style: { display: "flex", gap: "6px", flexShrink: "0" } },
              !rel.is_active && !rel.is_running
                ? el("button", {
                    type: "button",
                    className: "btn btn-xs btn-danger",
                    disabled: busy,
                    text: "删除缓存",
                    onClick: () => this.handleDeleteRelease(rel),
                  })
                : null
            )
          );
        })
      : [
          el("div", {
            className: "grid-empty",
            text: `当前尚未安装任何增量补丁，系统正直接运行 DataOG.exe v${st.base_version} 内置底座代码。`,
          }),
        ];

    const historyCard = el(
      "div",
      { className: "mac-section" },
      el(
        "div",
        { className: "mac-section-header" },
        el("h3", { className: "mac-section-title", text: `已缓存增量补丁版本 (${releases.length})` })
      ),
      el("div", {}, ...releaseRows)
    );

    clearEl(this.root, statusCard, upgradeCard, historyCard);
  }
}
