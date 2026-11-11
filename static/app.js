/**
 * 华为 DWS 字段贯标与保序转换工作台 - 前端交互逻辑
 */

(function () {
  "use strict";

  const state = {
    connection: {
      configured: false,
      host: "",
      port: 8000,
      dbname: "",
      user: "",
      password: "",
      sslmode: "prefer",
      current_schema: "public",
      schemas: [],
      discovered_table_count: 0,
      discovered_tables: [],
    },
    excelMeta: {
      filename: "未导入",
      imported_at: "",
    },
    summary: {
      total: 0,
      completed: 0,
      pending: 0,
      partial: 0,
      failed: 0,
      missing: 0,
    },
    items: [],
    logs: [],
    statusFilter: "ALL",
    tableFilter: "ALL",
    searchQuery: "",
    selectedIds: new Set(),
    activeRebuildTarget: {
      schemaName: "",
      tableName: "",
    },
    catalogSchemaFilter: "",
    catalogSearchQuery: "",
    lastMaxLogId: 0,
    livePollTimer: null,
    pollInFlight: false,
  };

  const STATUS_TEXT = {
    COMPLETED: "已贯标完成",
    PENDING: "待贯标转换",
    PARTIAL: "部分完成",
    FAILED: "执行报错",
    MISSING_TABLE: "目标表不存在",
    MISSING_COLUMN: "字段不存在",
    UNCHECKED: "未核验",
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function setDialogAlert(alertId, message, type) {
    const node = document.getElementById(alertId);
    if (!node) return;
    if (!message) {
      node.textContent = "";
      node.className = "dialog-alert is-hidden";
      return;
    }
    const variant =
      type === "error"
        ? "dialog-alert-error"
        : type === "success"
          ? "dialog-alert-success"
          : "dialog-alert-info";
    node.className = "dialog-alert " + variant;
    node.textContent = String(message);
  }

  function showToast(message, type) {
    const container = document.getElementById("toastContainer");
    if (!container) return;
    const item = el(
      "div",
      "toast-item " + (type === "error" ? "toast-error" : "toast-success"),
      message
    );
    container.appendChild(item);
    window.setTimeout(function () {
      if (item.parentNode) {
        item.parentNode.removeChild(item);
      }
    }, 4500);
  }

  async function apiRequest(url, options) {
    const opts = Object.assign({ method: "GET", headers: {} }, options || {});
    const resp = await fetch(url, opts);
    let data = null;
    try {
      data = await resp.json();
    } catch (_err) {
      throw new Error("服务响应解析失败 (HTTP " + resp.status + ")");
    }
    if (!resp.ok) {
      throw new Error(
        (data && data.detail) || "请求执行失败 (HTTP " + resp.status + ")"
      );
    }
    return data;
  }

  function setupDialogs() {
    const dialogs = document.querySelectorAll("dialog.modal-dialog");
    dialogs.forEach(function (dialog) {
      if (!("closedBy" in HTMLDialogElement.prototype)) {
        dialog.addEventListener("click", function (event) {
          if (event.target !== dialog) return;
          const rect = dialog.getBoundingClientRect();
          const isDialogContent =
            rect.top <= event.clientY &&
            event.clientY <= rect.top + rect.height &&
            rect.left <= event.clientX &&
            event.clientX <= rect.left + rect.width;
          if (isDialogContent) return;
          dialog.close();
        });
      }
    });

    const closeBtns = document.querySelectorAll("[data-close-dialog]");
    closeBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        const targetId = btn.getAttribute("data-close-dialog");
        const dlg = document.getElementById(targetId);
        if (dlg && dlg.open) {
          dlg.close();
        }
      });
    });
  }

  function setupThemeToggle() {
    const metaScheme = document.querySelector('meta[name="color-scheme"]');
    const htmlRoot = document.documentElement;
    const toggleBtn = document.getElementById("btnThemeToggle");
    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");

    function applyTheme(scheme) {
      htmlRoot.classList.remove("theme-light", "theme-dark");
      if (scheme === "light" || scheme === "dark") {
        if (metaScheme) metaScheme.content = scheme;
        htmlRoot.classList.add("theme-" + scheme);
      } else {
        if (metaScheme) metaScheme.content = "light dark";
      }
    }

    const saved = window.localStorage.getItem("color-scheme");
    if (saved === "light" || saved === "dark") {
      applyTheme(saved);
    }

    mediaQuery.addEventListener("change", function () {
      const currentPinned = window.localStorage.getItem("color-scheme");
      if (!currentPinned) {
        applyTheme("light dark");
      }
    });

    if (toggleBtn) {
      toggleBtn.addEventListener("click", function () {
        const pinned = window.localStorage.getItem("color-scheme");
        const sysDark = mediaQuery.matches;
        const effectiveDark = pinned ? pinned === "dark" : sysDark;
        const nextScheme = effectiveDark ? "light" : "dark";
        window.localStorage.setItem("color-scheme", nextScheme);
        applyTheme(nextScheme);
      });
    }
  }

  function renderHeaderAndStats() {
    const connText = document.getElementById("connStatusText");
    const tblCount = state.connection.discovered_table_count || 0;
    const schemaCount = (state.connection.schemas || []).length;

    if (connText) {
      if (state.connection.configured) {
        connText.textContent =
          "华为 DWS: " +
          state.connection.host +
          ":" +
          state.connection.port +
          "/" +
          state.connection.dbname +
          " (" +
          schemaCount +
          "模式 / " +
          tblCount +
          "张表)";
      } else {
        connText.textContent = "未配置数据库连接 (点击右侧设置)";
      }
    }

    const fileMeta = document.getElementById("excelFileMeta");
    if (fileMeta) {
      fileMeta.textContent =
        "当前清单: " + (state.excelMeta.filename || "未导入");
    }

    const mapping = {
      statTotal: state.summary.total,
      statCompleted: state.summary.completed,
      statPending: state.summary.pending,
      statPartial: state.summary.partial,
      statFailed: state.summary.failed,
      statMissing: state.summary.missing,
    };
    Object.keys(mapping).forEach(function (id) {
      const node = document.getElementById(id);
      if (node) node.textContent = String(mapping[id] || 0);
    });
  }

  function renderTableSelectOptions() {
    const select = document.getElementById("tableFilterSelect");
    if (!select) return;
    const prevValue = state.tableFilter;
    const tables = [];
    const seen = new Set();

    state.items.forEach(function (item) {
      const full = item.schema_name + "." + item.table_name;
      if (!seen.has(full)) {
        seen.add(full);
        tables.push(full);
      }
    });

    select.replaceChildren();
    const allOpt = el("option", "", "全部数据表 (" + tables.length + " 张表)");
    allOpt.value = "ALL";
    select.appendChild(allOpt);

    tables.forEach(function (tbl) {
      const opt = el("option", "", tbl);
      opt.value = tbl;
      select.appendChild(opt);
    });

    if (seen.has(prevValue)) {
      select.value = prevValue;
    } else {
      state.tableFilter = "ALL";
      select.value = "ALL";
    }
  }

  function getFilteredItems() {
    const q = state.searchQuery.trim().toLowerCase();
    return state.items.filter(function (item) {
      if (state.statusFilter !== "ALL") {
        if (state.statusFilter === "MISSING") {
          if (
            item.status !== "MISSING_TABLE" &&
            item.status !== "MISSING_COLUMN"
          ) {
            return false;
          }
        } else if (item.status !== state.statusFilter) {
          return false;
        }
      }

      const fullTable = item.schema_name + "." + item.table_name;
      if (state.tableFilter !== "ALL" && fullTable !== state.tableFilter) {
        return false;
      }

      if (q) {
        const hay = [
          fullTable,
          item.old_col_name,
          item.old_col_type,
          item.old_col_comment,
          item.new_col_name,
          item.new_col_type,
          item.new_col_comment,
          item.status_detail,
          item.error_message,
        ]
          .join(" ")
          .toLowerCase();
        if (hay.indexOf(q) === -1) {
          return false;
        }
      }
      return true;
    });
  }

  function renderChecklistTable() {
    const tbody = document.getElementById("checklistTableBody");
    if (!tbody) return;
    tbody.replaceChildren();

    const filtered = getFilteredItems();
    if (filtered.length === 0) {
      const emptyRow = el("tr");
      const emptyMsg =
        state.items.length === 0
          ? "当前工作台为空。请点击顶部「数据库连接设置」配置华为 DWS 连接，然后点击「导入 Excel 贯标清单」或「浏览目标库表结构」。"
          : "当前筛选条件下没有匹配的贯标字段记录。";
      const emptyCell = el("td", "cell-sub", emptyMsg);
      emptyCell.setAttribute("colspan", "7");
      emptyRow.appendChild(emptyCell);
      tbody.appendChild(emptyRow);
      return;
    }

    filtered.forEach(function (item) {
      const tr = el("tr");

      // 1. 复选框列
      const tdCheck = el("td", "col-check");
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.checked = state.selectedIds.has(item.id);
      chk.disabled = item.status === "COMPLETED";
      chk.setAttribute(
        "aria-label",
        "选择字段 " + item.old_col_name + " -> " + item.new_col_name
      );
      chk.addEventListener("change", function () {
        if (chk.checked) {
          state.selectedIds.add(item.id);
        } else {
          state.selectedIds.delete(item.id);
        }
      });
      tdCheck.appendChild(chk);
      tr.appendChild(tdCheck);

      // 2. 序号与表内物理位置 (attnum)
      const tdPos = el("td");
      tdPos.appendChild(el("div", "cell-title", "#" + item.row_index));
      const posLabel = item.ordinal_position
        ? "第 " + item.ordinal_position + " / " + item.total_columns + " 列"
        : "未定位";
      tdPos.appendChild(el("span", "pill", posLabel));
      if (item.is_distribute_key) {
        tdPos.appendChild(el("span", "pill pill-dist", "分布列"));
      }
      if (item.table_orientation === "COLUMN") {
        tdPos.appendChild(el("span", "pill pill-orient", "列存表"));
      }
      tr.appendChild(tdPos);

      // 3. 所属模式与表名
      const tdTable = el("td");
      tdTable.appendChild(
        el("div", "cell-title", item.schema_name + "." + item.table_name)
      );
      const inspectBtn = el(
        "button",
        "btn btn-ghost btn-sm",
        "查看全表列顺序 & 重建脚本"
      );
      inspectBtn.type = "button";
      inspectBtn.addEventListener("click", function () {
        openTableRebuildModal(item.schema_name, item.table_name);
      });
      tdTable.appendChild(inspectBtn);
      tr.appendChild(tdTable);

      // 4. 修改前信息
      const tdOld = el("td");
      tdOld.appendChild(el("div", "cell-title", item.old_col_name));
      tdOld.appendChild(
        el("div", "cell-sub", "类型: " + (item.old_col_type || "-"))
      );
      tdOld.appendChild(
        el("div", "cell-sub", "注释: " + (item.old_col_comment || "无"))
      );
      tr.appendChild(tdOld);

      // 5. 修改后信息
      const tdNew = el("td");
      tdNew.appendChild(el("div", "cell-title", item.new_col_name));
      tdNew.appendChild(
        el("div", "cell-sub", "类型: " + (item.new_col_type || "-"))
      );
      tdNew.appendChild(
        el("div", "cell-sub", "注释: " + (item.new_col_comment || "无"))
      );
      tr.appendChild(tdNew);

      // 6. 数据库实时联动状态与报错信息
      const tdStatus = el("td");
      const statusBadge = el(
        "span",
        "status-tag status-" + item.status,
        STATUS_TEXT[item.status] || item.status
      );
      tdStatus.appendChild(statusBadge);
      if (item.db_current_col_name) {
        tdStatus.appendChild(
          el(
            "div",
            "cell-sub",
            "库内当前: " +
              item.db_current_col_name +
              " (" +
              item.db_current_type +
              ")"
          )
        );
      }
      if (item.status_detail) {
        tdStatus.appendChild(el("div", "cell-sub", item.status_detail));
      }
      if (item.error_message) {
        tdStatus.appendChild(el("div", "error-box", item.error_message));
      }
      tr.appendChild(tdStatus);

      // 7. 操作按钮列
      const tdActions = el("td", "col-actions");
      const actGroup = el("div", "action-group");

      if (item.status !== "COMPLETED") {
        const btnDirect = el("button", "btn btn-primary btn-sm", "执行 ALTER");
        btnDirect.type = "button";
        btnDirect.addEventListener("click", function () {
          executeDirectAlter([item.id]);
        });
        actGroup.appendChild(btnDirect);

        const btnRebuild = el(
          "button",
          item.status === "FAILED" || item.is_distribute_key
            ? "btn btn-accent btn-sm"
            : "btn btn-secondary btn-sm",
          "原序重建表"
        );
        btnRebuild.type = "button";
        btnRebuild.addEventListener("click", function () {
          openTableRebuildModal(item.schema_name, item.table_name);
        });
        actGroup.appendChild(btnRebuild);
      } else {
        const btnView = el("button", "btn btn-secondary btn-sm", "查看顺序核验");
        btnView.type = "button";
        btnView.addEventListener("click", function () {
          openTableRebuildModal(item.schema_name, item.table_name);
        });
        actGroup.appendChild(btnView);
      }

      tdActions.appendChild(actGroup);
      tr.appendChild(tdActions);
      tbody.appendChild(tr);
    });
  }

  function renderConsoleLogs() {
    const stream = document.getElementById("consoleLogStream");
    if (!stream) return;

    if (!state.logs || state.logs.length === 0) {
      stream.replaceChildren();
      stream.appendChild(el("div", "log-entry", "暂无操作日志。"));
      state.lastMaxLogId = 0;
      return;
    }

    const newestId = state.logs[0] && state.logs[0].id ? state.logs[0].id : state.logs.length;
    const hasNewLogs = newestId !== state.lastMaxLogId;

    // 按时间正序（从旧到新）渲染日志流，并在有新日志时自动平滑滚动到底部，与后台控制台一致
    const chronologicalLogs = state.logs.slice().reverse();
    stream.replaceChildren();

    chronologicalLogs.forEach(function (logItem) {
      const entry = el("div", "log-entry");
      const head = el(
        "div",
        "log-level-" + logItem.level,
        "[" +
          logItem.created_at +
          "] [" +
          logItem.level +
          "] [" +
          logItem.target +
          "]"
      );
      const msg = el("div", "", logItem.message);
      entry.appendChild(head);
      entry.appendChild(msg);
      if (logItem.sql_text) {
        const sqlNode = el("div", "log-sql-text", logItem.sql_text);
        entry.appendChild(sqlNode);
      }
      stream.appendChild(entry);
    });

    if (hasNewLogs) {
      state.lastMaxLogId = newestId;
      stream.scrollTop = stream.scrollHeight;
    }
  }

  function startLiveExecutionPolling() {
    stopLiveExecutionPolling();
    state.livePollTimer = window.setInterval(async function () {
      if (state.pollInFlight) return;
      state.pollInFlight = true;
      try {
        const snap = await apiRequest("/api/bootstrap");
        updateStateFromPayload(snap);
      } catch (_err) {
        // 忽略轮询期间的瞬时错误
      } finally {
        state.pollInFlight = false;
      }
    }, 450);
  }

  function stopLiveExecutionPolling() {
    if (state.livePollTimer) {
      window.clearInterval(state.livePollTimer);
      state.livePollTimer = null;
    }
    state.pollInFlight = false;
  }

  function renderCatalogModalContent() {
    const subTitle = document.getElementById("catalogSubTitle");
    const schemas = state.connection.schemas || [];
    const allTables = state.connection.discovered_tables || [];

    if (subTitle) {
      if (!state.connection.configured) {
        subTitle.textContent = "尚未配置华为 DWS 连接，请先配置数据库连接。";
      } else {
        subTitle.textContent =
          "当前连接: 华为 DWS [" +
          state.connection.host +
          ":" +
          state.connection.port +
          "/" +
          state.connection.dbname +
          "] | 默认模式: " +
          (state.connection.current_schema || "public") +
          " | 共读取到 " +
          schemas.length +
          " 个模式、" +
          (state.connection.discovered_table_count || allTables.length) +
          " 张数据表";
      }
    }

    const schemaSelect = document.getElementById("catalogSchemaSelect");
    if (schemaSelect) {
      const prevSchema = state.catalogSchemaFilter;
      schemaSelect.replaceChildren();
      const optAll = el(
        "option",
        "",
        "全部模式 (" + schemas.length + " 个 Schema)"
      );
      optAll.value = "";
      schemaSelect.appendChild(optAll);

      schemas.forEach(function (s) {
        const opt = el("option", "", s);
        opt.value = s;
        schemaSelect.appendChild(opt);
      });

      if (schemas.indexOf(prevSchema) !== -1) {
        schemaSelect.value = prevSchema;
      } else {
        state.catalogSchemaFilter = "";
        schemaSelect.value = "";
      }
    }

    const tbody = document.getElementById("catalogTableBody");
    if (!tbody) return;
    tbody.replaceChildren();

    const q = state.catalogSearchQuery.trim().toLowerCase();
    const filteredTables = allTables.filter(function (t) {
      if (
        state.catalogSchemaFilter &&
        t.schema_name !== state.catalogSchemaFilter
      ) {
        return false;
      }
      if (q) {
        const hay = (
          t.schema_name +
          "." +
          t.table_name +
          " " +
          (t.table_comment || "")
        ).toLowerCase();
        if (hay.indexOf(q) === -1) return false;
      }
      return true;
    });

    if (filteredTables.length === 0) {
      const tr = el("tr");
      const td = el(
        "td",
        "cell-sub",
        state.connection.configured
          ? "未找到匹配的数据表（可点击右上角「刷新目标库表清单」）。"
          : "请先配置并连接华为 DWS 数据库。"
      );
      td.setAttribute("colspan", "6");
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    filteredTables.forEach(function (tbl) {
      const tr = el("tr");
      tr.appendChild(el("td", "cell-title", tbl.schema_name));
      tr.appendChild(el("td", "cell-title", tbl.table_name));
      tr.appendChild(
        el(
          "td",
          "",
          tbl.orientation === "COLUMN" ? "列存表 (COLUMN)" : "行存表 (ROW)"
        )
      );
      tr.appendChild(el("td", "", String(tbl.column_count || 0) + " 列"));
      tr.appendChild(el("td", "cell-sub", tbl.table_comment || "-"));

      const tdAct = el("td");
      const group = el("div", "action-group");

      const btnLoadSingle = el(
        "button",
        "btn btn-primary btn-sm",
        "载入该表结构"
      );
      btnLoadSingle.type = "button";
      btnLoadSingle.addEventListener("click", function () {
        loadTablesFromDatabase(tbl.schema_name, tbl.table_name);
      });
      group.appendChild(btnLoadSingle);

      const btnInspect = el(
        "button",
        "btn btn-secondary btn-sm",
        "查看字段顺序"
      );
      btnInspect.type = "button";
      btnInspect.addEventListener("click", function () {
        const catDlg = document.getElementById("catalogDialog");
        if (catDlg && catDlg.open) catDlg.close();
        openTableRebuildModal(tbl.schema_name, tbl.table_name);
      });
      group.appendChild(btnInspect);

      tdAct.appendChild(group);
      tr.appendChild(tdAct);
      tbody.appendChild(tr);
    });
  }

  async function loadTablesFromDatabase(schemaName, tableName) {
    setDialogAlert(
      "catalogDialogAlert",
      "正在从华为 DWS 目标库读取表字段顺序与注释信息...",
      "info"
    );
    try {
      const res = await apiRequest("/api/catalog/load", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          schema_name: schemaName || "",
          table_name: tableName || "",
          max_tables: 30,
        }),
      });
      state.selectedIds.clear();
      updateStateFromPayload(res);
      setDialogAlert("catalogDialogAlert", "", "info");
      const dlg = document.getElementById("catalogDialog");
      if (dlg && dlg.open) dlg.close();
      showToast(res.message || "成功载入目标库表结构！", "success");
    } catch (err) {
      setDialogAlert("catalogDialogAlert", err.message, "error");
      showToast(err.message, "error");
    }
  }

  function updateStateFromPayload(payload) {
    if (!payload) return;
    if (payload.connection) state.connection = payload.connection;
    if (payload.excel_meta) state.excelMeta = payload.excel_meta;
    if (payload.summary) state.summary = payload.summary;
    if (payload.items) {
      state.items = payload.items;
      state.items.forEach(function (it) {
        if (it.status === "COMPLETED") {
          state.selectedIds.delete(it.id);
        }
      });
    }
    if (payload.logs) state.logs = payload.logs;

    renderHeaderAndStats();
    renderTableSelectOptions();
    renderChecklistTable();
    renderConsoleLogs();
    renderCatalogModalContent();
  }

  async function executeDirectAlter(itemIds) {
    if (!itemIds || itemIds.length === 0) {
      showToast("请先勾选需要执行的待贯标字段", "error");
      return;
    }
    const btnBatch = document.getElementById("btnBatchAlter");
    const origBtnText = btnBatch ? btnBatch.textContent : "";
    if (btnBatch) {
      btnBatch.disabled = true;
      btnBatch.textContent = "正在 5 线程按表并发执行中...";
    }
    startLiveExecutionPolling();
    try {
      const res = await apiRequest("/api/execute/direct", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ item_ids: itemIds, max_workers: 5 }),
      });
      updateStateFromPayload(res);
      if (res.failed_count > 0) {
        showToast(
          res.message ||
            "执行完成：成功 " +
              res.success_count +
              " 条，报错 " +
              res.failed_count +
              " 条",
          "error"
        );
      } else {
        showToast(
          res.message ||
            "执行成功！已完成 " +
              res.success_count +
              " 条字段贯标，字段顺序核验一致。",
          "success"
        );
      }
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      stopLiveExecutionPolling();
      if (btnBatch) {
        btnBatch.disabled = false;
        btnBatch.textContent = origBtnText;
      }
    }
  }

  async function openTableRebuildModal(schemaName, tableName) {
    state.activeRebuildTarget = {
      schemaName: schemaName,
      tableName: tableName,
    };
    setDialogAlert("rebuildDialogAlert", "", "info");
    const keepBackupChk = document.getElementById("chkKeepBackup");
    const keepBackup = keepBackupChk ? keepBackupChk.checked : true;

    try {
      const url =
        "/api/table/inspect?schema_name=" +
        encodeURIComponent(schemaName) +
        "&table_name=" +
        encodeURIComponent(tableName) +
        "&keep_backup=" +
        (keepBackup ? "true" : "false");
      const plan = await apiRequest(url);

      state.activeRebuildTarget = {
        schemaName: plan.schema_name || schemaName,
        tableName: plan.table_name || tableName,
      };

      const metaText = document.getElementById("rebuildTableMetaText");
      if (metaText) {
        metaText.textContent =
          "目标表: " +
          plan.schema_name +
          "." +
          plan.table_name +
          " | 共 " +
          plan.total_columns +
          " 列 | 存储属性: " +
          (plan.table_meta.with_clause || "默认") +
          " | " +
          (plan.table_meta.distribute_clause || "默认分布");
      }

      const compareBody = document.getElementById("orderCompareBody");
      if (compareBody) {
        compareBody.replaceChildren();
        plan.column_order_comparison.forEach(function (col) {
          const tr = el("tr", col.changed ? "row-changed" : "");
          tr.appendChild(
            el("td", "cell-title", "第 " + col.ordinal_position + " 列")
          );
          tr.appendChild(
            el(
              "td",
              "",
              col.before_name +
                " (" +
                col.before_type +
                ")" +
                (col.before_comment ? " [" + col.before_comment + "]" : "")
            )
          );
          tr.appendChild(
            el(
              "td",
              col.changed ? "cell-title" : "",
              col.after_name +
                " (" +
                col.after_type +
                ")" +
                (col.after_comment ? " [" + col.after_comment + "]" : "")
            )
          );
          tr.appendChild(
            el(
              "td",
              "",
              col.changed ? "原位转换 (顺序不变)" : "原样保留 (顺序不变)"
            )
          );
          compareBody.appendChild(tr);
        });
      }

      const sqlCode = document.getElementById("sqlPreviewCode");
      if (sqlCode) {
        sqlCode.textContent = plan.sql_preview;
      }

      const dlg = document.getElementById("rebuildDialog");
      if (dlg && !dlg.open) {
        dlg.showModal();
      }
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  function bindEvents() {
    // 1. 统计卡片点击筛选
    const statCards = document.querySelectorAll(".stat-card[data-filter]");
    statCards.forEach(function (card) {
      card.addEventListener("click", function () {
        statCards.forEach(function (c) {
          c.classList.remove("active");
        });
        card.classList.add("active");
        state.statusFilter = card.getAttribute("data-filter") || "ALL";
        renderChecklistTable();
      });
    });

    // 2. 搜索与表名筛选
    const searchInput = document.getElementById("searchInput");
    if (searchInput) {
      searchInput.addEventListener("input", function () {
        state.searchQuery = searchInput.value;
        renderChecklistTable();
      });
    }

    const tableSelect = document.getElementById("tableFilterSelect");
    if (tableSelect) {
      tableSelect.addEventListener("change", function () {
        state.tableFilter = tableSelect.value;
        renderChecklistTable();
      });
    }

    // 3. 全选复选框
    const checkAll = document.getElementById("checkAllRows");
    if (checkAll) {
      checkAll.addEventListener("change", function () {
        const visible = getFilteredItems();
        visible.forEach(function (item) {
          if (item.status !== "COMPLETED") {
            if (checkAll.checked) {
              state.selectedIds.add(item.id);
            } else {
              state.selectedIds.delete(item.id);
            }
          }
        });
        renderChecklistTable();
      });
    }

    // 4. 打开数据库连接弹窗并直接回填本地保存的账号密码
    const btnOpenConn = document.getElementById("btnOpenConnDialog");
    if (btnOpenConn) {
      btnOpenConn.addEventListener("click", function () {
        setDialogAlert("connDialogAlert", "", "info");
        document.getElementById("inputHost").value =
          state.connection.host || "";
        document.getElementById("inputPort").value = String(
          state.connection.port || 8000
        );
        document.getElementById("inputDbName").value =
          state.connection.dbname || "";
        document.getElementById("inputUser").value =
          state.connection.user || "";
        document.getElementById("inputPassword").value =
          state.connection.password || "";
        document.getElementById("inputSslMode").value =
          state.connection.sslmode || "prefer";

        const dlg = document.getElementById("connDialog");
        if (dlg) dlg.showModal();
      });
    }

    // 5. 提交数据库连接表单
    const connForm = document.getElementById("connForm");
    if (connForm) {
      connForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        const btnSave = document.getElementById("btnSaveConn");
        const origText = btnSave ? btnSave.textContent : "";
        if (btnSave) {
          btnSave.disabled = true;
          btnSave.textContent = "正在连接并读取目标库表结构...";
        }
        setDialogAlert(
          "connDialogAlert",
          "正在建立华为 DWS 连接并读取目标库 Schema 与表结构，请稍候...",
          "info"
        );

        const payload = {
          host: document.getElementById("inputHost").value,
          port:
            parseInt(document.getElementById("inputPort").value, 10) || 8000,
          dbname: document.getElementById("inputDbName").value,
          user: document.getElementById("inputUser").value,
          password: document.getElementById("inputPassword").value,
          sslmode: document.getElementById("inputSslMode").value,
        };
        try {
          const res = await apiRequest("/api/connection", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          updateStateFromPayload(res);
          setDialogAlert("connDialogAlert", "", "info");
          const dlg = document.getElementById("connDialog");
          if (dlg && dlg.open) dlg.close();
          showToast(res.connection_result.message, "success");
        } catch (err) {
          setDialogAlert("connDialogAlert", err.message, "error");
          showToast(err.message, "error");
        } finally {
          if (btnSave) {
            btnSave.disabled = false;
            btnSave.textContent = origText;
          }
        }
      });
    }

    // 6. 打开目标库表结构浏览器弹窗
    const btnOpenCatalog = document.getElementById("btnOpenCatalogDialog");
    if (btnOpenCatalog) {
      btnOpenCatalog.addEventListener("click", async function () {
        setDialogAlert("catalogDialogAlert", "", "info");
        renderCatalogModalContent();
        const dlg = document.getElementById("catalogDialog");
        if (dlg) dlg.showModal();
        if (state.connection.configured) {
          try {
            const res = await apiRequest("/api/catalog");
            updateStateFromPayload(res);
          } catch (err) {
            setDialogAlert("catalogDialogAlert", err.message, "error");
          }
        }
      });
    }

    const catalogSchemaSelect = document.getElementById("catalogSchemaSelect");
    if (catalogSchemaSelect) {
      catalogSchemaSelect.addEventListener("change", function () {
        state.catalogSchemaFilter = catalogSchemaSelect.value;
        renderCatalogModalContent();
      });
    }

    const catalogSearchInput = document.getElementById("catalogSearchInput");
    if (catalogSearchInput) {
      catalogSearchInput.addEventListener("input", function () {
        state.catalogSearchQuery = catalogSearchInput.value;
        renderCatalogModalContent();
      });
    }

    const btnRefreshCatalog = document.getElementById("btnRefreshCatalog");
    if (btnRefreshCatalog) {
      btnRefreshCatalog.addEventListener("click", async function () {
        setDialogAlert(
          "catalogDialogAlert",
          "正在刷新华为 DWS 目标库模式与表清单...",
          "info"
        );
        try {
          const res = await apiRequest("/api/catalog");
          updateStateFromPayload(res);
          setDialogAlert("catalogDialogAlert", "", "info");
          showToast("已刷新目标库表结构目录", "success");
        } catch (err) {
          setDialogAlert("catalogDialogAlert", err.message, "error");
        }
      });
    }

    const btnLoadSchemaTables = document.getElementById("btnLoadSchemaTables");
    if (btnLoadSchemaTables) {
      btnLoadSchemaTables.addEventListener("click", function () {
        loadTablesFromDatabase(state.catalogSchemaFilter, "");
      });
    }

    // 7. 打开 Excel 上传弹窗与提交上传
    const btnOpenUpload = document.getElementById("btnOpenUploadDialog");
    if (btnOpenUpload) {
      btnOpenUpload.addEventListener("click", function () {
        setDialogAlert("uploadDialogAlert", "", "info");
        const defSchemaInput = document.getElementById("inputDefaultSchema");
        if (defSchemaInput && !defSchemaInput.value) {
          defSchemaInput.placeholder =
            "留空自动匹配 (当前默认: " +
            (state.connection.current_schema || "public") +
            ")";
        }
        const dlg = document.getElementById("uploadDialog");
        if (dlg) dlg.showModal();
      });
    }

    const uploadForm = document.getElementById("uploadForm");
    if (uploadForm) {
      uploadForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        const fileInput = document.getElementById("inputExcelFile");
        if (!fileInput.files || fileInput.files.length === 0) {
          setDialogAlert(
            "uploadDialogAlert",
            "请先点击选择要上传的 .xlsx 贯标清单文件",
            "error"
          );
          showToast("请先选择 .xlsx 清单文件", "error");
          return;
        }

        const btnSubmit = document.getElementById("btnSubmitUpload");
        const origText = btnSubmit ? btnSubmit.textContent : "";
        if (btnSubmit) {
          btnSubmit.disabled = true;
          btnSubmit.textContent = "正在解析 Excel 并核验表结构...";
        }
        setDialogAlert(
          "uploadDialogAlert",
          "正在上传解析 Excel 清单并与数据库表结构实时比对，请稍候...",
          "info"
        );

        const formData = new FormData();
        formData.append("file", fileInput.files[0]);
        formData.append(
          "default_schema",
          document.getElementById("inputDefaultSchema").value || ""
        );
        formData.append(
          "default_table",
          document.getElementById("inputDefaultTable").value || ""
        );

        try {
          const res = await apiRequest("/api/excel/upload", {
            method: "POST",
            body: formData,
          });
          state.selectedIds.clear();
          state.statusFilter = "ALL";
          state.tableFilter = "ALL";
          state.searchQuery = "";
          const searchNode = document.getElementById("searchInput");
          if (searchNode) searchNode.value = "";
          document
            .querySelectorAll(".stat-card[data-filter]")
            .forEach(function (c) {
              c.classList.toggle(
                "active",
                c.getAttribute("data-filter") === "ALL"
              );
            });

          updateStateFromPayload(res);
          fileInput.value = "";
          setDialogAlert("uploadDialogAlert", "", "info");
          const dlg = document.getElementById("uploadDialog");
          if (dlg && dlg.open) dlg.close();
          showToast(
            res.message || "成功导入 Excel 清单并完成数据库实时核验！",
            "success"
          );
        } catch (err) {
          setDialogAlert("uploadDialogAlert", err.message, "error");
          showToast(err.message, "error");
        } finally {
          if (btnSubmit) {
            btnSubmit.disabled = false;
            btnSubmit.textContent = origText;
          }
        }
      });
    }

    // 8. 手动触发数据库元数据实时核验
    const btnSync = document.getElementById("btnSyncMetadata");
    if (btnSync) {
      btnSync.addEventListener("click", async function () {
        const origText = btnSync.textContent;
        btnSync.disabled = true;
        btnSync.textContent = "正在核验...";
        try {
          const res = await apiRequest("/api/sync", { method: "POST" });
          updateStateFromPayload(res);
          showToast(
            "已完成与华为 DWS 系统目录 (pg_attribute) 的实时比对",
            "success"
          );
        } catch (err) {
          showToast(err.message, "error");
        } finally {
          btnSync.disabled = false;
          btnSync.textContent = origText;
        }
      });
    }

    // 9. 清空当前清单
    const btnClearItems = document.getElementById("btnClearItems");
    if (btnClearItems) {
      btnClearItems.addEventListener("click", async function () {
        try {
          const res = await apiRequest("/api/items/clear", {
            method: "POST",
          });
          state.selectedIds.clear();
          updateStateFromPayload(res);
          showToast("已清空当前工作台清单", "success");
        } catch (err) {
          showToast(err.message, "error");
        }
      });
    }

    // 10. 批量执行（按表智能识别分布键 + 5线程并发）
    const btnBatchAlter = document.getElementById("btnBatchAlter");
    if (btnBatchAlter) {
      btnBatchAlter.addEventListener("click", function () {
        let ids = Array.from(state.selectedIds);
        if (ids.length === 0) {
          ids = getFilteredItems()
            .filter(function (it) {
              return (
                it.status === "PENDING" ||
                it.status === "PARTIAL" ||
                it.status === "FAILED"
              );
            })
            .map(function (it) {
              return it.id;
            });
        }
        executeDirectAlter(ids);
      });
    }

    // 11. 顶部工具栏「按原表顺序重建表迁移」按钮
    const btnRebuildWizard = document.getElementById("btnOpenRebuildWizard");
    if (btnRebuildWizard) {
      btnRebuildWizard.addEventListener("click", function () {
        const candidates = getFilteredItems();
        const target =
          candidates.find(function (it) {
            return it.status === "FAILED" || it.is_distribute_key;
          }) ||
          candidates[0] ||
          state.items[0];
        if (!target) {
          showToast("请先导入 Excel 贯标清单或载入目标库表结构", "error");
          return;
        }
        openTableRebuildModal(target.schema_name, target.table_name);
      });
    }

    // 12. 重建表弹窗内切换是否保留备份表、复制 SQL、确认执行重建
    const chkKeepBackup = document.getElementById("chkKeepBackup");
    if (chkKeepBackup) {
      chkKeepBackup.addEventListener("change", function () {
        if (state.activeRebuildTarget.schemaName) {
          openTableRebuildModal(
            state.activeRebuildTarget.schemaName,
            state.activeRebuildTarget.tableName
          );
        }
      });
    }

    const btnCopySql = document.getElementById("btnCopySql");
    if (btnCopySql) {
      btnCopySql.addEventListener("click", async function () {
        const codeNode = document.getElementById("sqlPreviewCode");
        if (!codeNode) return;
        try {
          await navigator.clipboard.writeText(codeNode.textContent || "");
          showToast("SQL 脚本已复制到剪贴板", "success");
        } catch (_err) {
          showToast("复制失败，请手动选中复制", "error");
        }
      });
    }

    const rebuildForm = document.getElementById("rebuildForm");
    if (rebuildForm) {
      rebuildForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        const t = state.activeRebuildTarget;
        if (!t.schemaName || !t.tableName) return;
        const keepBackup = document.getElementById("chkKeepBackup").checked;

        const btnConfirm = document.getElementById("btnConfirmRebuild");
        const origText = btnConfirm ? btnConfirm.textContent : "";
        if (btnConfirm) {
          btnConfirm.disabled = true;
          btnConfirm.textContent = "正在事务内按原序重建表并迁移数据...";
        }
        setDialogAlert(
          "rebuildDialogAlert",
          "正在事务中执行按原表物理顺序 (attnum) 重建表与数据迁移，请稍候...",
          "info"
        );

        startLiveExecutionPolling();
        try {
          const res = await apiRequest("/api/execute/rebuild", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              schema_name: t.schemaName,
              table_name: t.tableName,
              keep_backup: keepBackup,
            }),
          });
          updateStateFromPayload(res);
          setDialogAlert("rebuildDialogAlert", "", "info");
          const dlg = document.getElementById("rebuildDialog");
          if (dlg && dlg.open) dlg.close();
          showToast(res.message, "success");
        } catch (err) {
          setDialogAlert("rebuildDialogAlert", err.message, "error");
          showToast(err.message, "error");
        } finally {
          stopLiveExecutionPolling();
          if (btnConfirm) {
            btnConfirm.disabled = false;
            btnConfirm.textContent = origText;
          }
        }
      });
    }

    // 13. 清空日志
    const btnClearLogs = document.getElementById("btnClearLogs");
    if (btnClearLogs) {
      btnClearLogs.addEventListener("click", async function () {
        try {
          const res = await apiRequest("/api/logs/clear", { method: "POST" });
          updateStateFromPayload(res);
        } catch (err) {
          showToast(err.message, "error");
        }
      });
    }

    // 14. 绑定跨环境三栏数标比对中心事件
    bindCrossEnvEvents();
  }

  // ===========================================================================
  // 跨环境三栏数标比对中心 (16:9 三条纵贯线：来源库 × 数据标准 × 目标库)
  // ===========================================================================

  const crossState = {
    activeTab: "WORKBENCH",
    sourceConn: { configured: false, db_type: "DWS", host: "", port: 8000, dbname: "", schema_name: "public", user: "", auth_mode: "LDAP", tables: [] },
    targetConn: { configured: false, db_type: "DWS", host: "", port: 8000, dbname: "", schema_name: "public", user: "", auth_mode: "LDAP", tables: [] },
    standardMeta: { filename: "未导入", table_count: 0, column_count: 0 },
    tables: [],
    activeTableKey: "",
    activeComparison: null,
    tableSearchQuery: "",
    srcColFilter: "",
    stdColFilter: "",
    stdStatusFilter: "ALL",
    tgtColFilter: "",
  };

  function switchMainMode(mode) {
    crossState.activeTab = mode;
    const tabWb = document.getElementById("tabBtnWorkbench");
    const tabCross = document.getElementById("tabBtnCrossEnv");
    const viewWb = document.getElementById("viewWorkbench");
    const viewCross = document.getElementById("viewCrossEnv");
    if (!tabWb || !tabCross || !viewWb || !viewCross) return;

    if (mode === "CROSS_ENV") {
      tabWb.classList.remove("active");
      tabWb.setAttribute("aria-selected", "false");
      tabCross.classList.add("active");
      tabCross.setAttribute("aria-selected", "true");
      viewWb.classList.add("is-hidden");
      viewCross.classList.remove("is-hidden");
      loadCrossOverview(crossState.activeTableKey);
    } else {
      tabCross.classList.remove("active");
      tabCross.setAttribute("aria-selected", "false");
      tabWb.classList.add("active");
      tabWb.setAttribute("aria-selected", "true");
      viewCross.classList.add("is-hidden");
      viewWb.classList.remove("is-hidden");
    }
  }

  async function loadCrossOverview(tableKey) {
    try {
      const q = tableKey ? "?table_key=" + encodeURIComponent(tableKey) : "";
      const data = await apiRequest("/api/cross/overview" + q);
      updateCrossState(data);
    } catch (err) {
      showToast("加载跨环境比对视图失败: " + err.message, "error");
    }
  }

  function updateCrossState(payload) {
    if (!payload) return;
    if (payload.source_connection) crossState.sourceConn = payload.source_connection;
    if (payload.target_connection) crossState.targetConn = payload.target_connection;
    if (payload.standard_meta) crossState.standardMeta = payload.standard_meta;
    if (Array.isArray(payload.tables)) crossState.tables = payload.tables;
    if (payload.active_table_key !== undefined) crossState.activeTableKey = payload.active_table_key || "";
    if (payload.active_comparison !== undefined) crossState.activeComparison = payload.active_comparison;
    renderCrossHub();
  }

  function renderCrossHub() {
    // 1. 渲染顶部标准表下拉选择器
    const sel = document.getElementById("crossTableSelect");
    if (sel) {
      sel.replaceChildren();
      const q = (crossState.tableSearchQuery || "").trim().toLowerCase();
      const filteredTables = crossState.tables.filter(function (t) {
        if (!q) return true;
        return (
          (t.std_table_name || "").toLowerCase().includes(q) ||
          (t.source_table || "").toLowerCase().includes(q) ||
          (t.target_table || "").toLowerCase().includes(q)
        );
      });

      if (filteredTables.length === 0) {
        const opt = el("option", "", "-- 暂无匹配标准表，请先上传数标清单或同步 --");
        opt.value = "";
        sel.appendChild(opt);
      } else {
        filteredTables.forEach(function (t) {
          const label =
            t.std_table_name +
            " (标准 " +
            (t.column_count || 0) +
            " 列 | 源:" +
            (t.source_table || t.std_table_name) +
            " ↔ 目:" +
            (t.target_table || t.std_table_name) +
            ")";
          const opt = el("option", "", label);
          opt.value = t.std_table_key;
          if (t.std_table_key === crossState.activeTableKey) {
            opt.selected = true;
          }
          sel.appendChild(opt);
        });
      }
    }

    // 2. 更新左右两库连接摘要与 Datalist
    renderRoleConnHeader("source", crossState.sourceConn, "srcDbTypeBadge", "srcConnSummaryText", "srcTablesDatalist");
    renderRoleConnHeader("target", crossState.targetConn, "tgtDbTypeBadge", "tgtConnSummaryText", "tgtTablesDatalist");

    const comp = crossState.activeComparison;
    const kpiTotal = document.getElementById("crossKpiTotal");
    const kpiOk = document.getElementById("crossKpiCompliant");
    const kpiWarn = document.getElementById("crossKpiPending");
    const kpiDiff = document.getElementById("crossKpiEnvDiff");
    if (comp && comp.summary) {
      if (kpiTotal) kpiTotal.textContent = "本表标准: " + comp.summary.total + " 列";
      if (kpiOk) kpiOk.textContent = "目标已落标: " + comp.summary.compliant;
      if (kpiWarn) kpiWarn.textContent = "目标待改造: " + (comp.summary.target_pending + comp.summary.missing);
      if (kpiDiff) kpiDiff.textContent = "两库差异: " + comp.summary.source_diff;
    } else {
      if (kpiTotal) kpiTotal.textContent = "总标准字段: " + (crossState.standardMeta.column_count || 0);
      if (kpiOk) kpiOk.textContent = "目标已落标: 0";
      if (kpiWarn) kpiWarn.textContent = "目标待改造: 0";
      if (kpiDiff) kpiDiff.textContent = "两库差异: 0";
    }

    // 3. 填充三纵列顶部当前绑定表名输入框
    const stdBadge = document.getElementById("stdActiveTableBadge");
    const stdMetaText = document.getElementById("stdMetaSummaryText");
    if (stdMetaText) {
      stdMetaText.textContent =
        "当前数标文件: " +
        (crossState.standardMeta.filename || "未导入") +
        " · 共纳管 " +
        (crossState.standardMeta.table_count || 0) +
        " 张表 / " +
        (crossState.standardMeta.column_count || 0) +
        " 项标准";
    }

    if (comp && comp.table_binding) {
      const tb = comp.table_binding;
      if (stdBadge) stdBadge.textContent = (tb.std_schema_name || "public") + "." + tb.std_table_name;
      const srcSchemaNode = document.getElementById("srcSchemaInput");
      const srcTableNode = document.getElementById("srcTableInput");
      const tgtSchemaNode = document.getElementById("tgtSchemaInput");
      const tgtTableNode = document.getElementById("tgtTableInput");
      if (srcSchemaNode) srcSchemaNode.value = tb.source_schema || "";
      if (srcTableNode) srcTableNode.value = tb.source_table || "";
      if (tgtSchemaNode) tgtSchemaNode.value = tb.target_schema || "";
      if (tgtTableNode) tgtTableNode.value = tb.target_table || "";
    } else {
      if (stdBadge) stdBadge.textContent = "未选择表";
      const srcSchemaNode = document.getElementById("srcSchemaInput");
      const tgtSchemaNode = document.getElementById("tgtSchemaInput");
      if (srcSchemaNode && !srcSchemaNode.value && crossState.sourceConn && crossState.sourceConn.schema_name) {
        srcSchemaNode.value = crossState.sourceConn.schema_name;
      }
      if (tgtSchemaNode && !tgtSchemaNode.value && crossState.targetConn && crossState.targetConn.schema_name) {
        tgtSchemaNode.value = crossState.targetConn.schema_name;
      }
    }

    // 4. 渲染三条纵贯线主体
    renderSourceSpine(comp ? comp.source_panel : null);
    renderStandardSpine(comp);
    renderTargetSpine(comp ? comp.target_panel : null);
  }

  function renderRoleConnHeader(role, conn, badgeId, summaryId, datalistId) {
    const badge = document.getElementById(badgeId);
    const summary = document.getElementById(summaryId);
    const dl = document.getElementById(datalistId);
    if (badge) {
      badge.textContent = conn.db_type || "DWS";
    }
    if (summary) {
      if (conn.configured && conn.host) {
        const tblCnt = Array.isArray(conn.tables) ? conn.tables.length : 0;
        summary.textContent =
          "已连接 [" +
          conn.db_type +
          "] " +
          conn.host +
          ":" +
          conn.port +
          "/" +
          conn.dbname +
          " (默认模式: " +
          (conn.schema_name || "public") +
          "，已探测 " +
          tblCnt +
          " 张表/视图)";
      } else {
        summary.textContent =
          role === "source"
            ? "未连接来源数据库（点击右上角配置 PGSQL / 华为 DWS / 星环 Inceptor）"
            : "未连接目标数据库（点击右上角配置 PGSQL / 华为 DWS / 星环 Inceptor）";
      }
    }
    if (dl) {
      dl.replaceChildren();
      (conn.tables || []).slice(0, 2000).forEach(function (t) {
        const opt = document.createElement("option");
        opt.value = t.table_name;
        opt.label = t.schema_name + "." + t.table_name + (t.table_comment ? " - " + t.table_comment : "");
        dl.appendChild(opt);
      });
    }
  }

  function renderSourceSpine(sourcePanel) {
    const container = document.getElementById("srcLaneBody");
    if (!container) return;
    container.replaceChildren();

    if (!sourcePanel) {
      container.appendChild(
        el("div", "lane-empty-box", "请在上方输入或选择表名/视图名后点击「读取表」，或在中间列上传数标清单")
      );
      return;
    }
    if (!sourcePanel.configured) {
      container.appendChild(
        el("div", "lane-empty-box", "来源库尚未连接。请点击本列右上角「配置来源库连接」接入 PGSQL / 华为 DWS / 星环 Inceptor。")
      );
      return;
    }
    if (sourcePanel.error) {
      container.appendChild(
        el("div", "lane-empty-box", "读取来源库表结构报错: " + sourcePanel.error)
      );
      return;
    }
    if (!sourcePanel.exists) {
      container.appendChild(
        el(
          "div",
          "lane-empty-box",
          "来源库中未找到表 " +
            sourcePanel.schema_name +
            "." +
            sourcePanel.table_name +
            "（可在上方修改绑定的来源表名后点击「读取表」）"
        )
      );
      return;
    }

    const q = (crossState.srcColFilter || "").trim().toLowerCase();
    const cols = (sourcePanel.columns || []).filter(function (c) {
      if (!q) return true;
      return (
        c.column_name.toLowerCase().includes(q) ||
        (c.data_type || "").toLowerCase().includes(q) ||
        (c.column_comment || "").toLowerCase().includes(q)
      );
    });

    if (cols.length === 0) {
      container.appendChild(el("div", "lane-empty-box", "无匹配的来源库字段"));
      return;
    }

    if (sourcePanel.object_type === "VIEW") {
      const baseList = Array.isArray(sourcePanel.penetrated_base_tables) && sourcePanel.penetrated_base_tables.length > 0
        ? " · 已穿透基表: " + sourcePanel.penetrated_base_tables.join(", ")
        : " · 已通过零行元数据探测与血缘解析补全类型";
      container.appendChild(
        el("div", "view-penetrate-badge", "视图对象 (VIEW)" + baseList)
      );
    }

    cols.forEach(function (c) {
      const boundList = c.bound_std_cols || [];
      const card = el(
        "div",
        "spine-node-card " + (boundList.length > 0 ? "node-compliant" : "")
      );
      const top = el("div", "spine-row-top");
      const leftTitle = el(
        "span",
        "spine-col-title",
        "#" + c.ordinal_position + " " + c.column_name
      );
      const rightType = el("span", "spine-col-type", c.data_type);
      top.appendChild(leftTitle);
      top.appendChild(rightType);
      card.appendChild(top);

      if (c.column_comment || c.is_key) {
        const commLine = el(
          "div",
          "spine-col-comment",
          (c.is_key ? "[分布/分区键] " : "") + (c.column_comment || "无字段注释")
        );
        card.appendChild(commLine);
      }
      if (c.lineage_source) {
        card.appendChild(el("div", "lineage-tag", c.lineage_source));
      }

      const diag = el("div", "spine-diag-line");
      diag.appendChild(
        el(
          "span",
          "",
          boundList.length > 0
            ? "已绑定数标: ⟷ " + boundList.join(", ")
            : "未绑定中间数标项"
        )
      );
      card.appendChild(diag);
      container.appendChild(card);
    });
  }

  function renderStandardSpine(comp) {
    const container = document.getElementById("stdLaneBody");
    if (!container) return;
    container.replaceChildren();

    if (!comp || !comp.standard_panel) {
      container.appendChild(
        el(
          "div",
          "lane-empty-box",
          "尚未载入数据标准。请点击上方「上传数标 / 导入绑定包」或「从执行台同步清单」。"
        )
      );
      return;
    }

    const q = (crossState.stdColFilter || "").trim().toLowerCase();
    const stFilter = crossState.stdStatusFilter || "ALL";
    const cols = (comp.standard_panel.columns || []).filter(function (sc) {
      if (stFilter === "COMPLIANT" && sc.target_status !== "COMPLIANT") return false;
      if (stFilter === "PENDING_MIGRATION" && sc.target_status === "COMPLIANT") return false;
      if (stFilter === "ENV_DIFF" && (!sc.env_diff_text || sc.env_diff_text === "两库结构一致")) return false;
      if (!q) return true;
      return (
        (sc.std_col_name || "").toLowerCase().includes(q) ||
        (sc.orig_col_name || "").toLowerCase().includes(q) ||
        (sc.std_col_comment || "").toLowerCase().includes(q) ||
        (sc.target_detail || "").toLowerCase().includes(q)
      );
    });

    if (cols.length === 0) {
      container.appendChild(el("div", "lane-empty-box", "当前筛选条件下无数据标准记录"));
      return;
    }

    cols.forEach(function (sc) {
      let stateClass = "node-pending";
      if (sc.target_status === "COMPLIANT") stateClass = "node-compliant";
      else if (sc.target_status === "MISSING_COLUMN" || sc.target_status === "MISSING_TABLE") {
        stateClass = "node-missing";
      }

      const card = el("div", "spine-node-card " + stateClass);
      card.dataset.bindingId = String(sc.id);

      const top = el("div", "spine-row-top");
      top.appendChild(
        el("span", "spine-col-title", "#" + sc.row_index + " " + sc.std_col_name)
      );
      top.appendChild(el("span", "spine-col-type", sc.std_col_type || "未指定类型"));
      card.appendChild(top);

      const commentText =
        (sc.std_col_comment || "无标准注释") +
        (sc.orig_col_name && sc.orig_col_name.toLowerCase() !== sc.std_col_name.toLowerCase()
          ? "  (清单原名: " + sc.orig_col_name + (sc.orig_col_type ? " " + sc.orig_col_type : "") + ")"
          : "");
      card.appendChild(el("div", "spine-col-comment", commentText));

      // 左右绑定桥接器：左绑来源库字段 ⟷ 右绑目标库字段
      const bridge = el("div", "spine-bind-bridge");
      const srcInput = document.createElement("input");
      srcInput.type = "text";
      srcInput.className = "spine-bind-input js-bind-src-col";
      srcInput.dataset.bindingId = String(sc.id);
      srcInput.value = sc.effective_source_col || sc.orig_col_name || sc.std_col_name;
      srcInput.title = "绑定的来源库字段名（可手工修改后点击顶部『持久保存当前绑定』）";
      srcInput.placeholder = "绑定来源列";

      const arrow = el("span", "spine-bridge-arrow", "⟵ 数标绑定 ⟶");

      const tgtInput = document.createElement("input");
      tgtInput.type = "text";
      tgtInput.className = "spine-bind-input js-bind-tgt-col";
      tgtInput.dataset.bindingId = String(sc.id);
      tgtInput.value = sc.effective_target_col || sc.std_col_name;
      tgtInput.title = "绑定的目标库字段名（可手工修改后点击顶部『持久保存当前绑定』）";
      tgtInput.placeholder = "绑定目标列";

      bridge.appendChild(srcInput);
      bridge.appendChild(arrow);
      bridge.appendChild(tgtInput);
      card.appendChild(bridge);

      const diag = el("div", "spine-diag-line");
      diag.appendChild(el("span", "", "目标库诊断: " + (sc.target_detail || "等待连接核验")));
      if (sc.env_diff_text) {
        diag.appendChild(el("span", "", sc.env_diff_text));
      }
      card.appendChild(diag);

      container.appendChild(card);
    });
  }

  function renderTargetSpine(targetPanel) {
    const container = document.getElementById("tgtLaneBody");
    if (!container) return;
    container.replaceChildren();

    if (!targetPanel) {
      container.appendChild(
        el("div", "lane-empty-box", "请在上方输入或选择表名/视图名后点击「读取表」，或在中间列上传数标清单")
      );
      return;
    }
    if (!targetPanel.configured) {
      container.appendChild(
        el("div", "lane-empty-box", "目标库尚未连接。请点击本列右上角「配置目标库连接」接入 PGSQL / 华为 DWS / 星环 Inceptor。")
      );
      return;
    }
    if (targetPanel.error) {
      container.appendChild(
        el("div", "lane-empty-box", "读取目标库表结构报错: " + targetPanel.error)
      );
      return;
    }
    if (!targetPanel.exists) {
      container.appendChild(
        el(
          "div",
          "lane-empty-box",
          "目标库中未找到表 " +
            targetPanel.schema_name +
            "." +
            targetPanel.table_name +
            "（可在上方修改绑定的目标表名后点击「读取表」）"
        )
      );
      return;
    }

    const q = (crossState.tgtColFilter || "").trim().toLowerCase();
    const cols = (targetPanel.columns || []).filter(function (c) {
      if (!q) return true;
      return (
        c.column_name.toLowerCase().includes(q) ||
        (c.data_type || "").toLowerCase().includes(q) ||
        (c.column_comment || "").toLowerCase().includes(q) ||
        (c.compliance_detail || "").toLowerCase().includes(q)
      );
    });

    if (cols.length === 0) {
      container.appendChild(el("div", "lane-empty-box", "无匹配的目标库字段"));
      return;
    }

    if (targetPanel.object_type === "VIEW") {
      const baseList = Array.isArray(targetPanel.penetrated_base_tables) && targetPanel.penetrated_base_tables.length > 0
        ? " · 已穿透基表: " + targetPanel.penetrated_base_tables.join(", ")
        : " · 已通过零行元数据探测与血缘解析补全类型";
      container.appendChild(
        el("div", "view-penetrate-badge", "视图对象 (VIEW)" + baseList)
      );
    }

    cols.forEach(function (c) {
      let cls = "";
      if (c.compliance_status === "COMPLIANT") cls = "node-compliant";
      else if (c.compliance_status === "PENDING_MIGRATION") cls = "node-pending";

      const card = el("div", "spine-node-card " + cls);
      const top = el("div", "spine-row-top");
      top.appendChild(
        el("span", "spine-col-title", "#" + c.ordinal_position + " " + c.column_name)
      );
      top.appendChild(el("span", "spine-col-type", c.data_type));
      card.appendChild(top);

      if (c.column_comment || c.is_key) {
        card.appendChild(
          el(
            "div",
            "spine-col-comment",
            (c.is_key ? "[分布/分区键] " : "") + (c.column_comment || "无字段注释")
          )
        );
      }
      if (c.lineage_source) {
        card.appendChild(el("div", "lineage-tag", c.lineage_source));
      }

      const diag = el("div", "spine-diag-line");
      const boundList = c.bound_std_cols || [];
      diag.appendChild(
        el(
          "span",
          "",
          boundList.length > 0
            ? "对应标准: " + boundList.join(", ") + " · " + c.compliance_detail
            : c.compliance_detail
        )
      );
      card.appendChild(diag);
      container.appendChild(card);
    });
  }

  function openCrossConnModal(role) {
    const dlg = document.getElementById("crossConnDialog");
    if (!dlg) return;
    const conn = role === "source" ? crossState.sourceConn : crossState.targetConn;
    const roleTitle = role === "source" ? "左列 · 来源数据库 (Source DB)" : "右列 · 目标数据库 (Target DB)";
    document.getElementById("crossConnRoleInput").value = role;
    document.getElementById("crossConnDialogTitle").textContent =
      "配置 " + roleTitle + " 连接 (PGSQL / 华为 DWS / 星环 Inceptor)";

    const dbTypeSel = document.getElementById("crossDbTypeSelect");
    dbTypeSel.value = conn.db_type || "DWS";
    document.getElementById("crossAuthModeSelect").value = conn.auth_mode || "LDAP";
    document.getElementById("crossHostInput").value = conn.host || "";
    document.getElementById("crossPortInput").value = conn.port || (conn.db_type === "INCEPTOR" ? 10000 : 8000);
    document.getElementById("crossDbNameInput").value = conn.dbname || "";
    document.getElementById("crossSchemaInput").value = conn.schema_name || "public";
    document.getElementById("crossUserInput").value = conn.user || "";
    document.getElementById("crossPasswordInput").value = "";
    setDialogAlert("crossConnDialogAlert", "", "info");
    dlg.showModal();
  }

  async function saveCurrentCrossBindings(autoResetNames, triggerRole) {
    const srcSchema =
      (document.getElementById("srcSchemaInput").value || "").trim() ||
      (crossState.sourceConn ? crossState.sourceConn.schema_name : "") ||
      "public";
    const srcTable = (document.getElementById("srcTableInput").value || "").trim();
    const tgtSchema =
      (document.getElementById("tgtSchemaInput").value || "").trim() ||
      (crossState.targetConn ? crossState.targetConn.schema_name : "") ||
      "public";
    const tgtTable = (document.getElementById("tgtTableInput").value || "").trim();

    let effectiveTableKey = (crossState.activeTableKey || "").trim();
    const activeTbObj = (crossState.tables || []).find(function (t) {
      return t.std_table_key === effectiveTableKey;
    });
    const isTempTable = !activeTbObj || !activeTbObj.column_count;

    if (!effectiveTableKey || (isTempTable && triggerRole === "source" && srcTable)) {
      effectiveTableKey = srcTable || tgtTable;
    } else if (!effectiveTableKey || (isTempTable && triggerRole === "target" && tgtTable)) {
      effectiveTableKey = tgtTable || srcTable;
    }

    if (!effectiveTableKey) {
      showToast("请先在来源库或目标库中选择/输入要读取的表名或视图名", "error");
      return;
    }

    const colBindings = [];
    if (autoResetNames && crossState.activeComparison && crossState.activeComparison.standard_panel) {
      (crossState.activeComparison.standard_panel.columns || []).forEach(function (sc) {
        if (Number(sc.id) > 0) {
          colBindings.push({
            id: sc.id,
            bound_source_col: sc.orig_col_name || sc.std_col_name,
            bound_target_col: sc.std_col_name || sc.orig_col_name,
          });
        }
      });
    } else {
      const srcInputs = document.querySelectorAll(".js-bind-src-col");
      const tgtMap = {};
      document.querySelectorAll(".js-bind-tgt-col").forEach(function (inp) {
        tgtMap[inp.dataset.bindingId] = inp.value.trim();
      });
      srcInputs.forEach(function (inp) {
        const bid = Number(inp.dataset.bindingId);
        if (bid > 0) {
          colBindings.push({
            id: bid,
            bound_source_col: inp.value.trim(),
            bound_target_col: tgtMap[String(bid)] || "",
          });
        }
      });
    }

    try {
      const res = await apiRequest("/api/cross/bindings/save", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          std_table_key: effectiveTableKey,
          source_schema: srcSchema,
          source_table: srcTable || effectiveTableKey,
          target_schema: tgtSchema,
          target_table: tgtTable || effectiveTableKey,
          column_bindings: colBindings,
        }),
      });
      updateCrossState(res);
      showToast(
        triggerRole
          ? "已成功读取表/视图 [" + (triggerRole === "source" ? srcTable || effectiveTableKey : tgtTable || effectiveTableKey) + "] 结构并完成核验！"
          : "已持久保存当前表的三方绑定关系并重新核验！",
        "success"
      );
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  function populateDdlTablesDatalist(role) {
    const dl = document.getElementById("ddlAllTablesDatalist");
    if (!dl) return;
    dl.replaceChildren();
    const seen = new Set();

    function addOpt(schemaName, tableName, desc) {
      if (!tableName || seen.has(tableName)) return;
      seen.add(tableName);
      const opt = document.createElement("option");
      opt.value = tableName;
      opt.label = (schemaName ? schemaName + "." : "") + tableName + (desc ? " (" + desc + ")" : "");
      dl.appendChild(opt);
    }

    if (role === "source" && crossState.sourceConn && Array.isArray(crossState.sourceConn.tables)) {
      crossState.sourceConn.tables.slice(0, 2000).forEach(function (t) {
        addOpt(t.schema_name, t.table_name, t.table_comment || "来源库表");
      });
    } else if (role === "target" && crossState.targetConn && Array.isArray(crossState.targetConn.tables)) {
      crossState.targetConn.tables.slice(0, 2000).forEach(function (t) {
        addOpt(t.schema_name, t.table_name, t.table_comment || "目标库表");
      });
    }

    (crossState.tables || []).forEach(function (tb) {
      const stdTbl = tb.std_table_name || tb.std_table || tb.std_table_key;
      if (role === "source") {
        addOpt(tb.source_schema, tb.source_table || stdTbl, "数标绑定来源表");
      } else if (role === "target") {
        addOpt(tb.target_schema, tb.target_table || stdTbl, "数标绑定目标表");
      } else {
        addOpt(tb.std_schema, stdTbl, "数据标准表");
      }
    });
  }

  function syncDdlRoleDefaults(role) {
    populateDdlTablesDatalist(role);
    const srcSchemaInp = document.getElementById("ddlSourceSchemaInput");
    const srcTableInp = document.getElementById("ddlSourceTableInput");
    const tgtDialectSel = document.getElementById("ddlTargetDialectSelect");
    const tgtSchemaInp = document.getElementById("ddlTargetSchemaInput");
    const tgtTableInp = document.getElementById("ddlTargetTableInput");
    const viewSqlGrp = document.getElementById("ddlViewSqlGroup");

    if (viewSqlGrp) {
      if (role === "view_sql") {
        viewSqlGrp.classList.remove("is-hidden");
      } else {
        viewSqlGrp.classList.add("is-hidden");
      }
    }

    const activeTb = (crossState.tables || []).find(function (t) {
      return (t.std_table_key || t.table_key) === crossState.activeTableKey;
    });
    const activeStdTbl = activeTb ? (activeTb.std_table_name || activeTb.std_table || activeTb.std_table_key) : "";

    if (role === "source") {
      const uiSchema = (document.getElementById("srcSchemaInput").value || "").trim();
      const uiTable = (document.getElementById("srcTableInput").value || "").trim();
      srcSchemaInp.value =
        uiSchema ||
        (activeTb ? activeTb.source_schema : "") ||
        (crossState.sourceConn ? crossState.sourceConn.schema_name : "public") ||
        "public";
      srcTableInp.value = uiTable || (activeTb ? activeTb.source_table || activeStdTbl : "");
      if (tgtDialectSel && crossState.targetConn && crossState.targetConn.db_type) {
        tgtDialectSel.value = crossState.targetConn.db_type;
      }
      if (tgtSchemaInp) {
        tgtSchemaInp.value =
          (document.getElementById("tgtSchemaInput").value || "").trim() ||
          (activeTb ? activeTb.target_schema : "");
      }
      if (tgtTableInp) {
        tgtTableInp.value =
          (document.getElementById("tgtTableInput").value || "").trim() ||
          (activeTb ? activeTb.target_table : "");
      }
    } else if (role === "target") {
      const uiSchema = (document.getElementById("tgtSchemaInput").value || "").trim();
      const uiTable = (document.getElementById("tgtTableInput").value || "").trim();
      srcSchemaInp.value =
        uiSchema ||
        (activeTb ? activeTb.target_schema : "") ||
        (crossState.targetConn ? crossState.targetConn.schema_name : "public") ||
        "public";
      srcTableInp.value = uiTable || (activeTb ? activeTb.target_table || activeStdTbl : "");
      if (tgtDialectSel && crossState.sourceConn && crossState.sourceConn.db_type) {
        tgtDialectSel.value = crossState.sourceConn.db_type;
      }
      if (tgtSchemaInp) {
        tgtSchemaInp.value =
          (document.getElementById("srcSchemaInput").value || "").trim() ||
          (activeTb ? activeTb.source_schema : "");
      }
      if (tgtTableInp) {
        tgtTableInp.value =
          (document.getElementById("srcTableInput").value || "").trim() ||
          (activeTb ? activeTb.source_table : "");
      }
    } else {
      srcSchemaInp.value = activeTb ? activeTb.std_schema : "public";
      srcTableInp.value = activeStdTbl;
      if (tgtDialectSel && crossState.targetConn && crossState.targetConn.db_type) {
        tgtDialectSel.value = crossState.targetConn.db_type;
      }
      if (tgtSchemaInp) {
        tgtSchemaInp.value = activeTb ? activeTb.target_schema : "";
      }
      if (tgtTableInp) {
        tgtTableInp.value = activeTb ? activeTb.target_table : "";
      }
    }
  }

  async function runDdlTranslation() {
    const role = document.getElementById("ddlSourceRoleSelect").value;
    const sourceSchema = document.getElementById("ddlSourceSchemaInput").value.trim();
    const sourceTable = document.getElementById("ddlSourceTableInput").value.trim();
    const targetDialect = document.getElementById("ddlTargetDialectSelect").value;
    const storageMode = document.getElementById("ddlStorageModeSelect").value;
    const targetSchema = document.getElementById("ddlTargetSchemaInput").value.trim();
    const targetTable = document.getElementById("ddlTargetTableInput").value.trim();
    const applyStd = document.getElementById("chkDdlApplyStandard").checked;
    const viewSqlNode = document.getElementById("ddlViewSqlInput");
    const viewSql = viewSqlNode ? viewSqlNode.value.trim() : "";

    if (role === "view_sql" && !viewSql) {
      setDialogAlert("ddlTranslateAlert", "请先在下方的视图定义 SQL 框中粘贴 CREATE VIEW ... AS SELECT ... 或 SELECT 语句", "error");
      return;
    }
    if (role !== "view_sql" && !sourceTable && !viewSql) {
      setDialogAlert("ddlTranslateAlert", "请先输入或选择要导出转换的来源表名/视图名", "error");
      return;
    }

    const btn = document.getElementById("btnGenerateTranslatedDdl");
    const origText = btn ? btn.textContent : "";
    if (btn) {
      btn.disabled = true;
      btn.textContent = "正在提取并翻译...";
    }
    setDialogAlert("ddlTranslateAlert", "", "info");

    try {
      const res = await apiRequest("/api/cross/ddl/translate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_role: role,
          source_schema: sourceSchema,
          source_table: sourceTable,
          target_dialect: targetDialect,
          storage_mode: storageMode,
          target_schema: targetSchema,
          target_table: targetTable,
          apply_standard_mapping: applyStd,
          std_table_key: crossState.activeTableKey || "",
          view_sql: role === "view_sql" ? viewSql : "",
        }),
      });

      const srcPre = document.getElementById("ddlSourcePreviewCode");
      const tgtPre = document.getElementById("ddlTargetPreviewCode");
      const originBadge = document.getElementById("ddlSourceOriginBadge");
      const tgtTitle = document.getElementById("ddlTargetBoxTitle");
      const mapBody = document.getElementById("ddlTypeMappingBody");

      if (srcPre) srcPre.textContent = res.source_ddl || "";
      if (tgtPre) tgtPre.textContent = res.target_ddl || "";
      if (originBadge) {
        const isView = res.source_object_type === "VIEW";
        const baseTablesText =
          isView && Array.isArray(res.penetrated_base_tables) && res.penetrated_base_tables.length > 0
            ? " · 已穿透基表[" + res.penetrated_base_tables.join(",") + "]"
            : "";
        originBadge.textContent =
          res.source_dialect +
          (isView ? " · 视图穿透逆向建表" + baseTablesText : res.from_live_db ? " · 实时数据库元数据" : " · 数标清单结构") +
          " (" +
          res.column_count +
          " 列)";
      }
      if (tgtTitle) {
        tgtTitle.textContent =
          "生成的目标环境 (" +
          res.target_dialect +
          " · " +
          res.storage_mode +
          ") 可执行建表语句 [" +
          res.target_schema +
          "." +
          res.target_table +
          "]";
      }

      if (mapBody) {
        mapBody.replaceChildren();
        (res.column_mappings || []).forEach(function (m, idx) {
          const tr = document.createElement("tr");
          tr.appendChild(el("td", "mono-cell", String(idx + 1)));
          const nameTd = el("td", "mono-cell", m.column_name);
          if (m.is_key) {
            nameTd.appendChild(el("span", "badge badge-partial", " 键"));
          }
          tr.appendChild(nameTd);
          tr.appendChild(el("td", "mono-cell", m.source_type));
          tr.appendChild(el("td", "mono-cell", m.target_type));
          const noteText =
            (m.note ? "[" + m.note + "] " : "") + (m.comment || "无注释");
          tr.appendChild(el("td", "", noteText));
          mapBody.appendChild(tr);
        });
      }
    } catch (err) {
      setDialogAlert("ddlTranslateAlert", err.message, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = origText;
      }
    }
  }

  function openDdlTranslatorModal(defaultRole) {
    const dlg = document.getElementById("ddlTranslateDialog");
    if (!dlg) return;
    const role = defaultRole || "source";
    const roleSel = document.getElementById("ddlSourceRoleSelect");
    if (roleSel) roleSel.value = role;
    syncDdlRoleDefaults(role);
    setDialogAlert("ddlTranslateAlert", "", "info");
    dlg.showModal();
    const srcTable = document.getElementById("ddlSourceTableInput").value.trim();
    if (srcTable) {
      runDdlTranslation();
    }
  }

  function bindCrossEnvEvents() {
    const tabWb = document.getElementById("tabBtnWorkbench");
    const tabCross = document.getElementById("tabBtnCrossEnv");
    if (tabWb) {
      tabWb.addEventListener("click", function () {
        switchMainMode("WORKBENCH");
      });
    }
    if (tabCross) {
      tabCross.addEventListener("click", function () {
        switchMainMode("CROSS_ENV");
      });
    }

    const crossTableSel = document.getElementById("crossTableSelect");
    if (crossTableSel) {
      crossTableSel.addEventListener("change", function () {
        crossState.activeTableKey = crossTableSel.value;
        loadCrossOverview(crossState.activeTableKey);
      });
    }

    const crossTableSearch = document.getElementById("crossTableSearchInput");
    if (crossTableSearch) {
      crossTableSearch.addEventListener("input", function () {
        crossState.tableSearchQuery = crossTableSearch.value;
        renderCrossHub();
      });
    }

    // 切换数据库类型时自动填入默认端口 (DWS:8000, PGSQL:5432, INCEPTOR:10000)
    const dbTypeSel = document.getElementById("crossDbTypeSelect");
    if (dbTypeSel) {
      dbTypeSel.addEventListener("change", function () {
        const portInp = document.getElementById("crossPortInput");
        if (!portInp) return;
        if (dbTypeSel.value === "INCEPTOR") portInp.value = "10000";
        else if (dbTypeSel.value === "PGSQL") portInp.value = "5432";
        else portInp.value = "8000";
      });
    }

    const btnSrcConn = document.getElementById("btnOpenSrcConnDialog");
    if (btnSrcConn) {
      btnSrcConn.addEventListener("click", function () {
        openCrossConnModal("source");
      });
    }

    const btnTgtConn = document.getElementById("btnOpenTgtConnDialog");
    if (btnTgtConn) {
      btnTgtConn.addEventListener("click", function () {
        openCrossConnModal("target");
      });
    }

    const crossConnForm = document.getElementById("crossConnForm");
    if (crossConnForm) {
      crossConnForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        const btn = document.getElementById("btnSaveCrossConn");
        const origText = btn ? btn.textContent : "";
        if (btn) {
          btn.disabled = true;
          btn.textContent = "正在连接并探测表目录...";
        }
        setDialogAlert("crossConnDialogAlert", "正在连接数据库并读取表目录，请稍候...", "info");
        try {
          const payload = {
            role: document.getElementById("crossConnRoleInput").value,
            db_type: document.getElementById("crossDbTypeSelect").value,
            auth_mode: document.getElementById("crossAuthModeSelect").value,
            sslmode: document.getElementById("crossAuthModeSelect").value === "NONE" ? "disable" : "prefer",
            host: document.getElementById("crossHostInput").value.trim(),
            port: Number(document.getElementById("crossPortInput").value || 8000),
            dbname: document.getElementById("crossDbNameInput").value.trim(),
            schema_name: document.getElementById("crossSchemaInput").value.trim(),
            user: document.getElementById("crossUserInput").value.trim(),
            password: document.getElementById("crossPasswordInput").value,
            active_table_key: crossState.activeTableKey,
          };
          const res = await apiRequest("/api/cross/connection", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          updateCrossState(res);
          const dlg = document.getElementById("crossConnDialog");
          if (dlg && dlg.open) dlg.close();
          showToast("跨环境数据库连接成功！", "success");
        } catch (err) {
          setDialogAlert("crossConnDialogAlert", err.message, "error");
          showToast(err.message, "error");
        } finally {
          if (btn) {
            btn.disabled = false;
            btn.textContent = origText;
          }
        }
      });
    }

    // 上传数标或绑定包弹窗
    const btnOpenCrossUpload = document.getElementById("btnOpenCrossUploadDialog");
    if (btnOpenCrossUpload) {
      btnOpenCrossUpload.addEventListener("click", function () {
        setDialogAlert("crossUploadDialogAlert", "", "info");
        const dlg = document.getElementById("crossUploadDialog");
        if (dlg) dlg.showModal();
      });
    }

    const crossUploadForm = document.getElementById("crossUploadForm");
    if (crossUploadForm) {
      crossUploadForm.addEventListener("submit", async function (ev) {
        ev.preventDefault();
        const fileInp = document.getElementById("inputCrossFile");
        if (!fileInp || !fileInp.files || fileInp.files.length === 0) {
          setDialogAlert("crossUploadDialogAlert", "请选择要上传的 .xlsx 或 .json 文件", "error");
          return;
        }
        const formData = new FormData();
        formData.append("file", fileInp.files[0]);
        const btn = document.getElementById("btnSubmitCrossUpload");
        const origText = btn ? btn.textContent : "";
        if (btn) {
          btn.disabled = true;
          btn.textContent = "正在解析并绑定...";
        }
        try {
          const res = await apiRequest("/api/cross/standard/upload", {
            method: "POST",
            body: formData,
          });
          updateCrossState(res);
          const dlg = document.getElementById("crossUploadDialog");
          if (dlg && dlg.open) dlg.close();
          crossUploadForm.reset();
          showToast("成功导入数据标准与三方绑定关系！", "success");
        } catch (err) {
          setDialogAlert("crossUploadDialogAlert", err.message, "error");
          showToast(err.message, "error");
        } finally {
          if (btn) {
            btn.disabled = false;
            btn.textContent = origText;
          }
        }
      });
    }

    const btnSyncWb = document.getElementById("btnSyncFromWorkbench");
    if (btnSyncWb) {
      btnSyncWb.addEventListener("click", async function () {
        try {
          const res = await apiRequest("/api/cross/standard/sync-from-workbench", {
            method: "POST",
          });
          updateCrossState(res);
          showToast("已从单库执行台同步落标清单至三栏比对中心！", "success");
        } catch (err) {
          showToast(err.message, "error");
        }
      });
    }

    const btnSaveBindings = document.getElementById("btnSaveCrossBindings");
    if (btnSaveBindings) {
      btnSaveBindings.addEventListener("click", function () {
        saveCurrentCrossBindings(false);
      });
    }

    const btnAutoBind = document.getElementById("btnAutoBindCols");
    if (btnAutoBind) {
      btnAutoBind.addEventListener("click", function () {
        saveCurrentCrossBindings(true);
      });
    }

    const btnReloadSrc = document.getElementById("btnReloadSrcTable");
    if (btnReloadSrc) {
      btnReloadSrc.addEventListener("click", function () {
        saveCurrentCrossBindings(false, "source");
      });
    }

    const srcTableInpNode = document.getElementById("srcTableInput");
    if (srcTableInpNode) {
      srcTableInpNode.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          saveCurrentCrossBindings(false, "source");
        }
      });
    }

    const btnReloadTgt = document.getElementById("btnReloadTgtTable");
    if (btnReloadTgt) {
      btnReloadTgt.addEventListener("click", function () {
        saveCurrentCrossBindings(false, "target");
      });
    }

    const tgtTableInpNode = document.getElementById("tgtTableInput");
    if (tgtTableInpNode) {
      tgtTableInpNode.addEventListener("keydown", function (ev) {
        if (ev.key === "Enter") {
          ev.preventDefault();
          saveCurrentCrossBindings(false, "target");
        }
      });
    }

    const btnExportCrossExcel = document.getElementById("btnExportCrossExcel");
    if (btnExportCrossExcel) {
      btnExportCrossExcel.addEventListener("click", function () {
        window.location.href = "/api/cross/bindings/export.xlsx";
      });
    }

    const btnPushWb = document.getElementById("btnPushToWorkbench");
    if (btnPushWb) {
      btnPushWb.addEventListener("click", async function () {
        try {
          const res = await apiRequest("/api/cross/push-to-workbench", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ std_table_key: "" }),
          });
          updateStateFromPayload(res);
          showToast(res.message || "已推送至贯标执行台", "success");
          switchMainMode("WORKBENCH");
        } catch (err) {
          showToast(err.message, "error");
        }
      });
    }

    // 三纵列字段实时过滤框
    const srcColSearch = document.getElementById("srcColFilterInput");
    if (srcColSearch) {
      srcColSearch.addEventListener("input", function () {
        crossState.srcColFilter = srcColSearch.value;
        renderSourceSpine(crossState.activeComparison ? crossState.activeComparison.source_panel : null);
      });
    }
    const stdColSearch = document.getElementById("stdColFilterInput");
    if (stdColSearch) {
      stdColSearch.addEventListener("input", function () {
        crossState.stdColFilter = stdColSearch.value;
        renderStandardSpine(crossState.activeComparison);
      });
    }
    const stdStatusSel = document.getElementById("stdStatusFilterSelect");
    if (stdStatusSel) {
      stdStatusSel.addEventListener("change", function () {
        crossState.stdStatusFilter = stdStatusSel.value;
        renderStandardSpine(crossState.activeComparison);
      });
    }
    const tgtColSearch = document.getElementById("tgtColFilterInput");
    if (tgtColSearch) {
      tgtColSearch.addEventListener("input", function () {
        crossState.tgtColFilter = tgtColSearch.value;
        renderTargetSpine(crossState.activeComparison ? crossState.activeComparison.target_panel : null);
      });
    }

    // 跨环境建表 DDL 导出与多方言翻译器事件绑定
    const btnOpenDdl = document.getElementById("btnOpenDdlTranslator");
    if (btnOpenDdl) {
      btnOpenDdl.addEventListener("click", function () {
        openDdlTranslatorModal("source");
      });
    }
    const btnTranslateSrc = document.getElementById("btnTranslateFromSource");
    if (btnTranslateSrc) {
      btnTranslateSrc.addEventListener("click", function () {
        openDdlTranslatorModal("source");
      });
    }
    const btnTranslateTgt = document.getElementById("btnTranslateFromTarget");
    if (btnTranslateTgt) {
      btnTranslateTgt.addEventListener("click", function () {
        openDdlTranslatorModal("target");
      });
    }

    const ddlRoleSel = document.getElementById("ddlSourceRoleSelect");
    if (ddlRoleSel) {
      ddlRoleSel.addEventListener("change", function () {
        syncDdlRoleDefaults(ddlRoleSel.value);
        if (document.getElementById("ddlSourceTableInput").value.trim()) {
          runDdlTranslation();
        }
      });
    }

    ["ddlTargetDialectSelect", "ddlStorageModeSelect", "chkDdlApplyStandard"].forEach(function (id) {
      const node = document.getElementById(id);
      if (node) {
        node.addEventListener("change", function () {
          if (document.getElementById("ddlSourceTableInput").value.trim()) {
            runDdlTranslation();
          }
        });
      }
    });

    const ddlForm = document.getElementById("ddlTranslateForm");
    if (ddlForm) {
      ddlForm.addEventListener("submit", function (ev) {
        ev.preventDefault();
        runDdlTranslation();
      });
    }

    const btnCopyDdl = document.getElementById("btnCopyTranslatedDdl");
    if (btnCopyDdl) {
      btnCopyDdl.addEventListener("click", async function () {
        const codeNode = document.getElementById("ddlTargetPreviewCode");
        const sqlText = codeNode ? (codeNode.textContent || "").trim() : "";
        if (!sqlText || sqlText.startsWith("-- 生成的目标方言建表语句")) {
          showToast("请先生成目标环境建表 DDL", "error");
          return;
        }
        try {
          await navigator.clipboard.writeText(sqlText);
          showToast("目标环境建表 DDL 已复制到剪贴板，可直接粘贴执行！", "success");
        } catch (_err) {
          showToast("复制失败，请手动选中代码块复制", "error");
        }
      });
    }

    const btnDownloadDdl = document.getElementById("btnDownloadTranslatedDdl");
    if (btnDownloadDdl) {
      btnDownloadDdl.addEventListener("click", function () {
        const codeNode = document.getElementById("ddlTargetPreviewCode");
        const sqlText = codeNode ? (codeNode.textContent || "").trim() : "";
        if (!sqlText || sqlText.startsWith("-- 生成的目标方言建表语句")) {
          showToast("请先生成目标环境建表 DDL", "error");
          return;
        }
        const dialect = document.getElementById("ddlTargetDialectSelect").value || "DDL";
        const tbl =
          document.getElementById("ddlTargetTableInput").value.trim() ||
          document.getElementById("ddlSourceTableInput").value.trim() ||
          "table";
        const blob = new Blob([sqlText + "\n"], { type: "text/sql;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = tbl + "_" + dialect.toLowerCase() + "_create.sql";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast("已下载建表脚本文件: " + a.download, "success");
      });
    }
  }

  async function bootstrap() {
    setupDialogs();
    setupThemeToggle();
    bindEvents();
    try {
      const data = await apiRequest("/api/bootstrap");
      updateStateFromPayload(data);
    } catch (err) {
      showToast("初始化加载失败: " + err.message, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", bootstrap);
})();
