/**
 * Data.O.G 系统配置子面板：数据库连接与 JDBC 驱动管理 (ConnectionsPane & DriversPane)
 */

import { platformApi } from "../core/api.js";
import { badge, clearEl, el, formatBytes, pickLocalFile, statusDot } from "../core/dom.js";
import { store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";

const DEFAULT_TEMPLATES = [
  { db_type: "DWS", label: "GaussDB(DWS)", driver_track: "NATIVE", default_port: 8000, default_schema: "public" },
  { db_type: "PGSQL", label: "PostgreSQL / openGauss", driver_track: "NATIVE", default_port: 5432, default_schema: "public" },
  { db_type: "INCEPTOR", label: "Transwarp Inceptor", driver_track: "NATIVE", default_port: 10000, default_schema: "default" },
  { db_type: "MYSQL", label: "MySQL / OceanBase / StarRocks", driver_track: "NATIVE", default_port: 3306, default_schema: "" },
  { db_type: "SQLITE", label: "SQLite", driver_track: "NATIVE", default_port: 0, default_schema: "main" },
  { db_type: "DM8", label: "达梦 DM8 (JDBC)", driver_track: "JDBC", default_port: 5236, default_schema: "SYSDBA", jdbc_url_template: "jdbc:dm://{host}:{port}/{dbname}", driver_class: "dm.jdbc.driver.DmDriver" },
  { db_type: "KINGBASE", label: "人大金仓 KingbaseES (JDBC)", driver_track: "JDBC", default_port: 54321, default_schema: "public", jdbc_url_template: "jdbc:kingbase8://{host}:{port}/{dbname}", driver_class: "com.kingbase8.Driver" },
  { db_type: "ORACLE", label: "Oracle (JDBC)", driver_track: "JDBC", default_port: 1521, default_schema: "", jdbc_url_template: "jdbc:oracle:thin:@//{host}:{port}/{dbname}", driver_class: "oracle.jdbc.OracleDriver" },
  { db_type: "DB2", label: "IBM DB2 (JDBC)", driver_track: "JDBC", default_port: 50000, default_schema: "", jdbc_url_template: "jdbc:db2://{host}:{port}/{dbname}", driver_class: "com.ibm.db2.jcc.DB2Driver" },
  { db_type: "GBASE", label: "南大通用 GBase (JDBC)", driver_track: "JDBC", default_port: 5258, default_schema: "", jdbc_url_template: "jdbc:gbase://{host}:{port}/{dbname}", driver_class: "com.gbase.jdbc.Driver" },
  { db_type: "CUSTOM_JDBC", label: "自定义 JDBC (.jar)", driver_track: "JDBC", default_port: 0, default_schema: "", jdbc_url_template: "jdbc:vendor://{host}:{port}/{dbname}", driver_class: "" },
];

export class ConnectionsView {
  constructor({ onChange = null } = {}) {
    this.onChange = onChange;
    this.editingId = "";
    this.formState = this._emptyForm();
    this.root = el("div", { className: "mac-section" });
  }

  _emptyForm() {
    return {
      connection_id: "",
      name: "",
      db_type: "DWS",
      driver_track: "NATIVE",
      host: "127.0.0.1",
      port: "8000",
      dbname: "postgres",
      default_schema: "public",
      user: "dbadmin",
      password: "",
      sslmode: "prefer",
      auth_mode: "LDAP",
      sqlite_path: "",
      jdbc_driver_id: "",
      jdbc_url: "",
      jdbc_driver_class: "",
      auto_refresh_metadata: true,
    };
  }

  resetToNew() {
    this.editingId = "";
    this.formState = this._emptyForm();
  }

  _getTemplates() {
    return store.state.driverTemplates?.length ? store.state.driverTemplates : DEFAULT_TEMPLATES;
  }

  _applyDbTypeDefaults(dbType) {
    const tpl = this._getTemplates().find((t) => t.db_type === dbType) || DEFAULT_TEMPLATES[0];
    this.formState.db_type = dbType;
    this.formState.driver_track = tpl.driver_track || "NATIVE";
    if (tpl.default_port !== undefined) {
      this.formState.port = String(tpl.default_port || "");
    }
    if (tpl.default_schema !== undefined) {
      this.formState.default_schema = tpl.default_schema || "";
    }
    if (this.formState.driver_track === "JDBC") {
      this.formState.jdbc_driver_class =
        tpl.jdbc_driver_class || tpl.driver_class || this.formState.jdbc_driver_class || "";
      const urlTpl = tpl.jdbc_url_template || "jdbc:vendor://{host}:{port}/{dbname}";
      this.formState.jdbc_url = urlTpl
        .replace("{host}", this.formState.host || "127.0.0.1")
        .replace("{port}", this.formState.port || String(tpl.default_port || 5236))
        .replace("{dbname}", this.formState.dbname || "mydb");
      if (!this.formState.jdbc_driver_id && store.state.drivers.length > 0) {
        this.formState.jdbc_driver_id = store.state.drivers[0].driver_id;
      }
    }
  }

  loadProfile(prof) {
    this.editingId = prof.connection_id;
    this.formState = {
      connection_id: prof.connection_id,
      name: prof.name || "",
      db_type: prof.db_type || "DWS",
      driver_track: prof.driver_track || "NATIVE",
      host: prof.host || "",
      port: String(prof.port ?? ""),
      dbname: prof.dbname || "",
      default_schema: prof.default_schema || "",
      user: prof.user || "",
      password: "",
      sslmode: prof.sslmode || "prefer",
      auth_mode: prof.auth_mode || "LDAP",
      sqlite_path: prof.sqlite_path || "",
      jdbc_driver_id: prof.jdbc_driver_id || "",
      jdbc_url: prof.jdbc_url || "",
      jdbc_driver_class: prof.jdbc_driver_class || "",
      auto_refresh_metadata: false,
    };
    this.render();
  }

  _buildPayload() {
    const f = this.formState;
    const payload = {
      connection_id: this.editingId || undefined,
      name: f.name.trim() || `${f.db_type} @ ${f.db_type === "SQLITE" ? f.sqlite_path : f.host}`,
      driver_track: f.driver_track,
      db_type: f.db_type,
      host: f.host.trim(),
      port: f.port ? Number(f.port) : 0,
      dbname: f.dbname.trim(),
      default_schema: f.default_schema.trim(),
      user: f.user.trim(),
      sslmode: f.sslmode,
      auth_mode: f.auth_mode,
      sqlite_path: f.sqlite_path.trim(),
      jdbc_driver_id: f.jdbc_driver_id,
      jdbc_url: f.jdbc_url.trim(),
      jdbc_driver_class: f.jdbc_driver_class.trim(),
      auto_refresh_metadata: Boolean(f.auto_refresh_metadata),
    };
    if (f.password) {
      payload.password = f.password;
    }
    return payload;
  }

  async _handleTest() {
    try {
      const res = await platformApi.testUnsavedConnection(this._buildPayload());
      Toast.success(`连通正常 (${res.elapsed_ms ?? res.latency_ms ?? 0} ms)`);
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async _handleSave(e) {
    e.preventDefault();
    try {
      const payload = this._buildPayload();
      const res = this.editingId
        ? await platformApi.updateConnection(this.editingId, payload)
        : await platformApi.saveConnection(payload);
      store.updateConnections(res.connections, res.catalog_tree, res.connection?.connection_id);
      if (res.warning) Toast.warn(res.warning);
      else Toast.success("连接已保存");
      if (res.connection) {
        this.loadProfile(res.connection);
      } else {
        this.render();
      }
      this.onChange?.();
    } catch (err) {
      Toast.error(err.message);
    }
  }

  render() {
    const conns = store.state.connections || [];
    const drivers = store.state.drivers || [];
    const f = this.formState;
    const isSqlite = f.db_type === "SQLITE";
    const isInceptor = f.db_type === "INCEPTOR";
    const isJdbc = f.driver_track === "JDBC";

    const bindInput = (key, props = {}) =>
      el("input", {
        className: "mac-input",
        ...props,
        value: f[key] ?? "",
        onInput: (e) => {
          f[key] = e.target.value;
        },
      });

    const dbTypeSelect = el(
      "select",
      {
        className: "mac-input",
        onChange: (e) => {
          this._applyDbTypeDefaults(e.target.value);
          this.render();
        },
      },
      this._getTemplates().map((t) => {
        const o = el("option", { value: t.db_type, text: t.label || t.db_type });
        if (t.db_type === f.db_type) o.selected = true;
        return o;
      })
    );

    const dynamicFields = [];
    if (isSqlite) {
      dynamicFields.push(
        el(
          "div",
          { className: "form-field span-2" },
          el("label", { className: "form-label", text: "SQLite 文件路径" }),
          bindInput("sqlite_path", { placeholder: "/path/to/db.sqlite" })
        )
      );
    } else if (isJdbc) {
      const drvSelect = el(
        "select",
        {
          className: "mac-input",
          onChange: (e) => {
            f.jdbc_driver_id = e.target.value;
            const found = drivers.find((d) => d.driver_id === f.jdbc_driver_id);
            const clsHint = found?.driver_class_hint || found?.driver_classes?.[0] || "";
            if (clsHint && !f.jdbc_driver_class) {
              f.jdbc_driver_class = clsHint;
              this.render();
            }
          },
        },
        [
          el("option", { value: "", text: "选择已上传驱动 (.jar)" }),
          ...drivers.map((d) => {
            const o = el("option", { value: d.driver_id, text: d.name });
            if (d.driver_id === f.jdbc_driver_id) o.selected = true;
            return o;
          }),
        ]
      );

      dynamicFields.push(
        el("div", { className: "form-field span-2" }, el("label", { className: "form-label", text: "JDBC 驱动" }), drvSelect),
        el("div", { className: "form-field span-2" }, el("label", { className: "form-label", text: "JDBC URL" }), bindInput("jdbc_url", { placeholder: "jdbc:dm://127.0.0.1:5236/DAMENG" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "Driver 类名" }), bindInput("jdbc_driver_class", { placeholder: "dm.jdbc.driver.DmDriver" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "默认 Schema" }), bindInput("default_schema", { placeholder: "SYSDBA" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "用户名" }), bindInput("user", { autocomplete: "username" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "密码" }), bindInput("password", { type: "password", placeholder: this.editingId ? "留空保持原密码" : "", autocomplete: "new-password" }))
      );
    } else {
      dynamicFields.push(
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "主机" }), bindInput("host", { placeholder: "127.0.0.1" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "端口" }), bindInput("port", { type: "number" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "数据库" }), bindInput("dbname", { placeholder: "postgres" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "Schema" }), bindInput("default_schema", { placeholder: "public" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "用户名" }), bindInput("user", { autocomplete: "username" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "密码" }), bindInput("password", { type: "password", placeholder: this.editingId ? "留空保持原密码" : "", autocomplete: "new-password" }))
      );
      if (isInceptor) {
        const authSel = el(
          "select",
          { className: "mac-input", onChange: (e) => (f.auth_mode = e.target.value) },
          ["LDAP", "PLAIN", "NOSASL"].map((m) => {
            const o = el("option", { value: m, text: m });
            if (f.auth_mode === m) o.selected = true;
            return o;
          })
        );
        dynamicFields.push(el("div", { className: "form-field span-2" }, el("label", { className: "form-label", text: "认证模式" }), authSel));
      }
    }

    const formBox = el(
      "form",
      { className: "mac-group-box", onSubmit: (e) => this._handleSave(e) },
      el(
        "div",
        { className: "mac-section-header", style: { marginBottom: "12px" } },
        el("h3", { className: "mac-section-title", text: this.editingId ? "编辑连接" : "新建连接" }),
        this.editingId
          ? el("button", {
              type: "button",
              className: "mac-btn mac-btn-sm",
              text: "+ 新建",
              onClick: () => {
                this.resetToNew();
                this.render();
              },
            })
          : badge(isJdbc ? "JDBC" : "原生", isJdbc ? "warn" : "info")
      ),
      el(
        "div",
        { className: "form-grid" },
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "名称" }), bindInput("name", { placeholder: "生产 DWS" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "类型" }), dbTypeSelect),
        ...dynamicFields,
        el(
          "label",
          { className: "form-field span-2", style: { flexDirection: "row", alignItems: "center", gap: "7px", marginTop: "2px" } },
          el("input", {
            type: "checkbox",
            checked: Boolean(f.auto_refresh_metadata),
            onChange: (e) => (f.auto_refresh_metadata = e.target.checked),
          }),
          el("span", { style: { fontSize: "12px", color: "var(--os-text-secondary)" }, text: "保存后自动缓存库表目录" })
        )
      ),
      el(
        "div",
        { style: { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "14px" } },
        el("button", { type: "button", className: "mac-btn", text: "连通检测", onClick: () => this._handleTest() }),
        el("button", { type: "submit", className: "mac-btn mac-btn-primary", text: "保存连接" })
      )
    );

    const savedRows = conns.length
      ? conns.map((c) =>
          el(
            "div",
            { className: "list-row-card" },
            el(
              "div",
              { style: { minWidth: "0", flex: "1" } },
              el(
                "div",
                { style: { display: "flex", alignItems: "center", gap: "7px" } },
                statusDot(c.name, "success"),
                badge(c.db_type, c.driver_track === "JDBC" ? "warn" : "neutral")
              ),
              el("div", {
                className: "mono",
                style: { fontSize: "11px", color: "var(--os-muted)", marginTop: "3px" },
                text:
                  c.db_type === "SQLITE"
                    ? c.sqlite_path || c.dbname
                    : c.driver_track === "JDBC"
                    ? c.jdbc_url
                    : `${c.host}:${c.port}/${c.dbname}`,
              })
            ),
            el(
              "div",
              { style: { display: "flex", gap: "5px" } },
              el("button", {
                type: "button",
                className: "btn btn-xs",
                text: "测试",
                onClick: async () => {
                  try {
                    const r = await platformApi.testSavedConnection(c.connection_id);
                    Toast.success(`连通正常 (${r.elapsed_ms ?? r.latency_ms ?? 0} ms)`);
                  } catch (err) {
                    Toast.error(err.message);
                  }
                },
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs",
                text: "探查",
                onClick: () => store.openApp("explorer", { connectionId: c.connection_id }),
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs",
                text: "编辑",
                onClick: () => this.loadProfile(c),
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs btn-danger",
                text: "删除",
                onClick: async () => {
                  const ok = await Modal.confirm({
                    title: "删除连接",
                    message: `确定删除「${c.name}」吗？`,
                    confirmText: "删除",
                    danger: true,
                  });
                  if (!ok) return;
                  try {
                    const r = await platformApi.deleteConnection(c.connection_id);
                    store.updateConnections(r.connections);
                    if (this.editingId === c.connection_id) this.resetToNew();
                    Toast.success("已删除");
                    this.render();
                  } catch (err) {
                    Toast.error(err.message);
                  }
                },
              })
            )
          )
        )
      : [el("div", { className: "grid-empty", style: { padding: "18px" }, text: "尚未添加数据库连接" })];

    clearEl(
      this.root,
      formBox,
      el(
        "div",
        { className: "mac-section", style: { marginTop: "6px" } },
        el("h3", { className: "mac-section-title", text: `已保存连接 (${conns.length})` }),
        el("div", {}, savedRows)
      )
    );
  }
}

export async function uploadJdbcDriverFlow(defaultDbHint = "CUSTOM_JDBC") {
  const file = await pickLocalFile(".jar");
  if (!file) return null;
  const res = await platformApi.uploadDriver(file, {
    name: file.name.replace(/\.jar$/i, ""),
    dbTypeHint: defaultDbHint,
  });
  store.updateDrivers(res.drivers);
  Toast.success("驱动已上传");
  return res;
}

export function renderDriversSection(onChanged) {
  const drivers = store.state.drivers || [];
  const jreReady = Boolean(store.state.javaRuntime?.available);

  return el(
    "div",
    { className: "mac-section" },
    el(
      "div",
      { className: "mac-section-header" },
      el(
        "div",
        { style: { display: "flex", alignItems: "center", gap: "10px" } },
        el("h3", { className: "mac-section-title", text: "JDBC 驱动包" }),
        statusDot(jreReady ? "JRE 已就绪" : "未检测到 JRE（原生驱动不受影响）", jreReady ? "success" : "muted")
      ),
      el("button", {
        type: "button",
        className: "mac-btn mac-btn-primary",
        text: "上传 .jar",
        onClick: async () => {
          try {
            await uploadJdbcDriverFlow();
            onChanged?.();
          } catch (err) {
            Toast.error(err.message);
          }
        },
      })
    ),
    drivers.length
      ? el(
          "div",
          {},
          drivers.map((d) =>
            el(
              "div",
              { className: "list-row-card" },
              el(
                "div",
                { style: { minWidth: "0", flex: "1" } },
                el(
                  "div",
                  { style: { display: "flex", alignItems: "center", gap: "8px", fontWeight: "600" } },
                  el("span", { text: d.name }),
                  badge(formatBytes(d.file_size), "neutral")
                ),
                el("div", {
                  className: "mono",
                  style: { fontSize: "11px", color: "var(--os-muted)", marginTop: "3px" },
                  text: `${d.driver_class_hint || (d.driver_classes || [])[0] || "未声明 Driver 类"} · SHA256 ${(d.sha256 || "").slice(0, 12)}`,
                })
              ),
              el("button", {
                type: "button",
                className: "btn btn-xs btn-danger",
                text: "移除",
                onClick: async () => {
                  const ok = await Modal.confirm({
                    title: "移除驱动",
                    message: `确定移除驱动「${d.name}」吗？`,
                    confirmText: "移除",
                    danger: true,
                  });
                  if (!ok) return;
                  try {
                    const res = await platformApi.deleteDriver(d.driver_id);
                    store.updateDrivers(res.drivers);
                    Toast.success("已移除");
                    onChanged?.();
                  } catch (err) {
                    Toast.error(err.message);
                  }
                },
              })
            )
          )
        )
      : el("div", { className: "mac-group-box grid-empty", text: "暂无自定义 .jar 驱动（DWS / PG / Inceptor / MySQL / SQLite 内置免驱动直连）" })
  );
}
