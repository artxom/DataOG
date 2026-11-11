/**
 * DataOG 内置核心视图 1：数据源与驱动管理中心 (ConnectionHub & DriverManager)
 * 支持：
 * - 内置免 Java 原生通道：华为云 DWS、PostgreSQL、星环 Inceptor/Hive、MySQL/OceanBase/Doris/StarRocks/TiDB、SQLite
 * - 自定义 JDBC .jar 扩展通道：达梦 DM8、人大金仓 KingbaseES、Oracle、DB2、GBase 及任意标准 JDBC 驱动上传、魔数与 SHA-256 校验
 */

import { platformApi } from "../core/api.js";
import { badge, clearEl, el, formatBytes, pickLocalFile } from "../core/dom.js";
import { store } from "../core/state.js";
import { Modal } from "../ui/modal.js";
import { Toast } from "../ui/toast.js";

const DEFAULT_TEMPLATES = [
  { db_type: "DWS", label: "华为云 GaussDB(DWS) [原生免Java]", driver_track: "NATIVE", default_port: 8000, default_schema: "public" },
  { db_type: "PGSQL", label: "PostgreSQL / openGauss [原生免Java]", driver_track: "NATIVE", default_port: 5432, default_schema: "public" },
  { db_type: "INCEPTOR", label: "星环 Inceptor / HiveServer2 [原生纯Python]", driver_track: "NATIVE", default_port: 10000, default_schema: "default" },
  { db_type: "MYSQL", label: "MySQL / OceanBase / Doris / StarRocks / TiDB [原生免Java]", driver_track: "NATIVE", default_port: 3306, default_schema: "" },
  { db_type: "SQLITE", label: "SQLite 3 本地数据库 [标准库免安装]", driver_track: "NATIVE", default_port: 0, default_schema: "main" },
  { db_type: "DM8", label: "达梦数据库 DM8 [自定义 JDBC .jar]", driver_track: "JDBC", default_port: 5236, default_schema: "SYSDBA", jdbc_url_template: "jdbc:dm://{host}:{port}/{dbname}", driver_class: "dm.jdbc.driver.DmDriver" },
  { db_type: "KINGBASE", label: "人大金仓 KingbaseES [自定义 JDBC .jar]", driver_track: "JDBC", default_port: 54321, default_schema: "public", jdbc_url_template: "jdbc:kingbase8://{host}:{port}/{dbname}", driver_class: "com.kingbase8.Driver" },
  { db_type: "ORACLE", label: "Oracle Database [自定义 JDBC .jar]", driver_track: "JDBC", default_port: 1521, default_schema: "", jdbc_url_template: "jdbc:oracle:thin:@//{host}:{port}/{dbname}", driver_class: "oracle.jdbc.OracleDriver" },
  { db_type: "DB2", label: "IBM DB2 [自定义 JDBC .jar]", driver_track: "JDBC", default_port: 50000, default_schema: "", jdbc_url_template: "jdbc:db2://{host}:{port}/{dbname}", driver_class: "com.ibm.db2.jcc.DB2Driver" },
  { db_type: "GBASE", label: "南大通用 GBase [自定义 JDBC .jar]", driver_track: "JDBC", default_port: 5258, default_schema: "", jdbc_url_template: "jdbc:gbase://{host}:{port}/{dbname}", driver_class: "com.gbase.jdbc.Driver" },
  { db_type: "CUSTOM_JDBC", label: "通用自定义 JDBC 数据源 (.jar)", driver_track: "JDBC", default_port: 0, default_schema: "", jdbc_url_template: "jdbc:vendor://{host}:{port}/{dbname}", driver_class: "" },
];

export class ConnectionsView {
  constructor() {
    this.editingId = "";
    this.formState = this._emptyForm();
    this.root = el("div", { className: "module-page" });
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

  mount(container, context = null) {
    if (context?.createNew) {
      this.editingId = "";
      this.formState = this._emptyForm();
    }
    clearEl(container, this.root);
    this.render();
  }

  _getTemplates() {
    return (store.state.driverTemplates && store.state.driverTemplates.length)
      ? store.state.driverTemplates
      : DEFAULT_TEMPLATES;
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
      this.formState.jdbc_driver_class = tpl.driver_class || this.formState.jdbc_driver_class || "";
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

  _loadProfileToForm(prof) {
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

  _buildPayloadFromForm() {
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

  async _handleTestForm() {
    try {
      const payload = this._buildPayloadFromForm();
      Toast.info("正在执行驱动握手与连通性测试...", 2000);
      const res = await platformApi.testUnsavedConnection(payload);
      Toast.success(
        `${res.message || "连接成功"} ${res.server_version ? "— " + res.server_version : ""} (${res.latency_ms ?? 0}ms)`
      );
    } catch (err) {
      Toast.error(`连接测试失败: ${err.message}`);
    }
  }

  async _handleSaveForm(e) {
    e.preventDefault();
    try {
      const payload = this._buildPayloadFromForm();
      const res = this.editingId
        ? await platformApi.updateConnection(this.editingId, payload)
        : await platformApi.saveConnection(payload);
      store.updateConnections(res.connections, res.catalog_tree, res.connection?.connection_id);
      if (res.warning) {
        Toast.warn(res.warning);
      } else {
        Toast.success(res.message || "数据源档案已保存");
      }
      this.editingId = res.connection?.connection_id || "";
      if (res.connection) {
        this._loadProfileToForm(res.connection);
      } else {
        this.render();
      }
    } catch (err) {
      Toast.error(err.message);
    }
  }

  async _handleUploadJar() {
    const file = await pickLocalFile(".jar");
    if (!file) return;
    try {
      Toast.info(`正在校验并隔离存储驱动包 [${file.name}]...`);
      const res = await platformApi.uploadDriver(file, {
        name: file.name.replace(/\.jar$/i, ""),
        dbTypeHint: this.formState.driver_track === "JDBC" ? this.formState.db_type : "CUSTOM_JDBC",
      });
      store.updateDrivers(res.drivers);
      if (res.driver && !this.formState.jdbc_driver_id) {
        this.formState.jdbc_driver_id = res.driver.driver_id;
        if (res.driver.driver_classes?.length && !this.formState.jdbc_driver_class) {
          this.formState.jdbc_driver_class = res.driver.driver_classes[0];
        }
      }
      Toast.success(res.message || "驱动上传成功");
      this.render();
    } catch (err) {
      Toast.error(`上传驱动失败: ${err.message}`);
    }
  }

  render() {
    const conns = store.state.connections || [];
    const drivers = store.state.drivers || [];
    const jre = store.state.javaRuntime || { available: false, message: "" };
    const f = this.formState;
    const isSqlite = f.db_type === "SQLITE";
    const isInceptor = f.db_type === "INCEPTOR";
    const isJdbc = f.driver_track === "JDBC";

    const toolbar = el(
      "div",
      { className: "module-toolbar" },
      el(
        "div",
        { className: "module-toolbar-left" },
        el("h1", { className: "module-title", text: "全局数据源与双轨驱动中心" }),
        el("span", {
          className: "module-subtitle",
          text: "一次配置，全平台 SQL 控制台、单库贯标、三栏比对与 DDL 翻译零延迟共享",
        })
      ),
      el(
        "div",
        { className: "module-toolbar-right" },
        el("button", {
          type: "button",
          className: "btn btn-sm btn-secondary",
          text: "重置为新建连接",
          onClick: () => {
            this.editingId = "";
            this.formState = this._emptyForm();
            this.render();
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-primary",
          text: "上传 JDBC .jar 驱动包",
          onClick: () => this._handleUploadJar(),
        })
      )
    );

    // 左栏：已保存数据源列表 + 新建/编辑连接表单
    const savedListNodes = conns.length
      ? conns.map((c) =>
          el(
            "div",
            { className: "conn-item-card" },
            el(
              "div",
              { style: { minWidth: "0", flex: "1" } },
              el(
                "div",
                { style: { display: "flex", alignItems: "center", gap: "6px", fontWeight: "700" } },
                el("span", { text: c.name }),
                badge(c.db_type, c.driver_track === "JDBC" ? "warn" : "info"),
                badge(c.driver_track === "JDBC" ? "自定义JDBC" : "内置原生", "neutral")
              ),
              el("div", {
                style: { fontSize: "11.5px", color: "var(--text-secondary)", marginTop: "3px" },
                text:
                  c.db_type === "SQLITE"
                    ? `文件: ${c.sqlite_path || c.dbname} | 默认模式: ${c.default_schema || "main"}`
                    : c.driver_track === "JDBC"
                    ? `JDBC: ${c.jdbc_url} | 用户: ${c.user || "-"}`
                    : `地址: ${c.host}:${c.port}/${c.dbname} | 模式: ${c.default_schema || "-"} | 用户: ${c.user || "-"}`,
              })
            ),
            el(
              "div",
              { style: { display: "flex", gap: "5px", flexWrap: "wrap" } },
              el("button", {
                type: "button",
                className: "btn btn-xs btn-secondary",
                text: "测试",
                onClick: async () => {
                  try {
                    const r = await platformApi.testSavedConnection(c.connection_id);
                    Toast.success(`${r.message || "连通正常"} (${r.latency_ms ?? 0}ms)`);
                  } catch (err) {
                    Toast.error(err.message);
                  }
                },
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs btn-secondary",
                text: "SQL台",
                onClick: () => store.navigateTo("sql_console", { connectionId: c.connection_id }),
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs btn-ghost",
                text: "编辑",
                onClick: () => this._loadProfileToForm(c),
              }),
              el("button", {
                type: "button",
                className: "btn btn-xs btn-danger",
                text: "删除",
                onClick: async () => {
                  const ok = await Modal.confirm({
                    title: "删除数据源连接档案",
                    message: `确定删除数据源「${c.name}」及其本地缓存的目录树索引吗？`,
                    confirmText: "确认删除",
                    danger: true,
                  });
                  if (!ok) return;
                  try {
                    const r = await platformApi.deleteConnection(c.connection_id);
                    store.updateConnections(r.connections);
                    if (this.editingId === c.connection_id) {
                      this.editingId = "";
                      this.formState = this._emptyForm();
                    }
                    Toast.success("数据源连接已删除");
                    this.render();
                  } catch (err) {
                    Toast.error(err.message);
                  }
                },
              })
            )
          )
        )
      : [el("div", { className: "grid-empty", text: "暂无已保存的数据源档案，请在下方表单创建第一个连接" })];

    const dbTypeSelect = el(
      "select",
      {
        className: "input",
        onChange: (e) => {
          this._applyDbTypeDefaults(e.target.value);
          this.render();
        },
      },
      this._getTemplates().map((t) => {
        const o = el("option", { value: t.db_type, text: t.label || `${t.db_type} (${t.driver_track})` });
        if (t.db_type === f.db_type) o.selected = true;
        return o;
      })
    );

    const bindInput = (key, props = {}) =>
      el("input", {
        className: "input",
        ...props,
        value: f[key] ?? "",
        onInput: (e) => {
          f[key] = e.target.value;
        },
      });

    const dynamicFields = [];
    if (isSqlite) {
      dynamicFields.push(
        el(
          "div",
          { className: "form-field span-2" },
          el("label", { className: "form-label", text: "SQLite 数据库文件路径 (支持绝对路径或相对路径，如 :memory: 或 ./demo.db)" }),
          bindInput("sqlite_path", { placeholder: "例如: /path/to/database.sqlite3" })
        )
      );
    } else if (isJdbc) {
      const drvSelect = el(
        "select",
        {
          className: "input",
          onChange: (e) => {
            f.jdbc_driver_id = e.target.value;
            const found = drivers.find((d) => d.driver_id === f.jdbc_driver_id);
            if (found && found.driver_classes?.length && !f.jdbc_driver_class) {
              f.jdbc_driver_class = found.driver_classes[0];
              this.render();
            }
          },
        },
        [
          el("option", { value: "", text: "-- 请选择已上传的 JDBC .jar 驱动包 --" }),
          ...drivers.map((d) => {
            const o = el("option", { value: d.driver_id, text: `${d.name} (${d.filename})` });
            if (d.driver_id === f.jdbc_driver_id) o.selected = true;
            return o;
          }),
        ]
      );

      dynamicFields.push(
        el("div", { className: "form-field span-2" }, el("label", { className: "form-label", text: "绑定的 JDBC 驱动包 (.jar)" }), drvSelect),
        el("div", { className: "form-field span-2" }, el("label", { className: "form-label", text: "JDBC 连接 URL" }), bindInput("jdbc_url", { placeholder: "jdbc:dm://127.0.0.1:5236/DAMENG" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "JDBC Driver 类名" }), bindInput("jdbc_driver_class", { placeholder: "如 dm.jdbc.driver.DmDriver" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "默认模式 (Schema)" }), bindInput("default_schema", { placeholder: "如 SYSDBA / public" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "用户名" }), bindInput("user", { autocomplete: "username" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: this.editingId ? "密码 (留空则保持原密码)" : "密码" }), bindInput("password", { type: "password", autocomplete: "new-password" }))
      );
    } else {
      dynamicFields.push(
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "主机地址 (Host / IP)" }), bindInput("host", { placeholder: "127.0.0.1" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "端口 (Port)" }), bindInput("port", { type: "number" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "数据库名 (Database)" }), bindInput("dbname", { placeholder: "postgres / default" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "默认模式 (Schema)" }), bindInput("default_schema", { placeholder: "public / default" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "账号 (User)" }), bindInput("user", { autocomplete: "username" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: this.editingId ? "密码 (留空保持已存密码)" : "密码 (Password)" }), bindInput("password", { type: "password", autocomplete: "new-password" }))
      );
      if (isInceptor) {
        const authSel = el(
          "select",
          { className: "input", onChange: (e) => (f.auth_mode = e.target.value) },
          ["LDAP", "PLAIN", "NOSASL"].map((m) => {
            const o = el("option", { value: m, text: m });
            if (f.auth_mode === m) o.selected = true;
            return o;
          })
        );
        dynamicFields.push(el("div", { className: "form-field span-2" }, el("label", { className: "form-label", text: "星环 Inceptor Thrift 认证模式" }), authSel));
      }
    }

    const autoRefreshCheckbox = el("input", {
      type: "checkbox",
      checked: Boolean(f.auto_refresh_metadata),
      onChange: (e) => {
        f.auto_refresh_metadata = e.target.checked;
      },
    });

    const formEl = el(
      "form",
      { onSubmit: (e) => this._handleSaveForm(e) },
      el(
        "div",
        { className: "form-grid" },
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "数据源档案名称" }), bindInput("name", { placeholder: "例如：生产DWS数仓 / 星环贴源层" })),
        el("div", { className: "form-field" }, el("label", { className: "form-label", text: "数据库方言与驱动通道" }), dbTypeSelect),
        ...dynamicFields,
        el(
          "label",
          { className: "form-field span-2", style: { flexDirection: "row", alignItems: "center", gap: "8px", marginTop: "4px" } },
          autoRefreshCheckbox,
          el("span", { text: "保存连接后立即探查库表结构并写入本地 MetadataHub 索引缓存" })
        )
      ),
      el(
        "div",
        { style: { display: "flex", justifyContent: "flex-end", gap: "8px", marginTop: "14px" } },
        el("button", {
          type: "button",
          className: "btn btn-secondary",
          text: "测试连接连通性",
          onClick: () => this._handleTestForm(),
        }),
        el("button", {
          type: "submit",
          className: "btn btn-primary",
          text: this.editingId ? "保存修改" : "创建并保存数据源",
        })
      )
    );

    // 右栏：自定义 JDBC 驱动仓库与 JRE 状态诊断
    const driverCards = drivers.length
      ? drivers.map((d) =>
          el(
            "div",
            { className: "conn-item-card" },
            el(
              "div",
              { style: { minWidth: "0", flex: "1" } },
              el(
                "div",
                { style: { display: "flex", alignItems: "center", gap: "6px", fontWeight: "700" } },
                el("span", { text: d.name }),
                badge(formatBytes(d.file_size), "neutral")
              ),
              el("div", {
                style: { fontSize: "11px", color: "var(--text-secondary)", marginTop: "3px", fontFamily: "var(--font-mono)" },
                text: `Driver类: ${(d.driver_classes || []).join(", ") || "需手动填写类名"} | SHA-256: ${(d.sha256 || "").slice(0, 16)}...`,
              })
            ),
            el("button", {
              type: "button",
              className: "btn btn-xs btn-danger",
              text: "移除",
              onClick: async () => {
                const ok = await Modal.confirm({
                  title: "移除 JDBC 驱动包",
                  message: `确定从隔离目录 data/drivers/ 中删除驱动「${d.name}」吗？`,
                  confirmText: "移除驱动",
                  danger: true,
                });
                if (!ok) return;
                try {
                  const res = await platformApi.deleteDriver(d.driver_id);
                  store.updateDrivers(res.drivers);
                  Toast.success("驱动包已移除");
                  this.render();
                } catch (err) {
                  Toast.error(err.message);
                }
              },
            })
          )
        )
      : [
          el("div", {
            className: "grid-empty",
            text: "暂未上传自定义 JDBC .jar 驱动包。内置 DWS / PG / Inceptor / MySQL / SQLite 无需上传即可直接使用！",
          }),
        ];

    const content = el(
      "div",
      { className: "module-content" },
      el(
        "div",
        { className: "conn-layout" },
        el(
          "div",
          { style: { display: "flex", flexDirection: "column", gap: "12px" } },
          el(
            "div",
            { className: "card" },
            el(
              "div",
              { className: "card-header" },
              el("h2", { className: "card-title", text: this.editingId ? `编辑数据源档案 (${this.editingId})` : "新建全局数据源连接档案" }),
              badge(isJdbc ? "自定义 JDBC 扩展轨" : "内置免 Java 原生轨", isJdbc ? "warn" : "success")
            ),
            formEl
          ),
          el(
            "div",
            { className: "card" },
            el(
              "div",
              { className: "card-header" },
              el("h2", { className: "card-title", text: `已注册全局数据源 (${conns.length})` })
            ),
            ...savedListNodes
          )
        ),
        el(
          "div",
          { style: { display: "flex", flexDirection: "column", gap: "12px" } },
          el(
            "div",
            { className: "card" },
            el(
              "div",
              { className: "card-header" },
              el("h2", { className: "card-title", text: "双轨驱动引擎状态与自定义 JDBC 仓库" }),
              badge(jre.available ? "JRE / JPype 就绪" : "免 Java 原生模式", jre.available ? "success" : "info")
            ),
            el("p", {
              style: { margin: "0 0 10px 0", fontSize: "12px", color: "var(--text-secondary)" },
              text:
                jre.message ||
                "内置原生驱动（DWS/PG/Inceptor/MySQL/SQLite）纯 Python 直连免安装 Java；连接达梦 DM8 / 金仓 / Oracle / DB2 时可上传标准 JDBC .jar 包并通过本地 JRE 按需懒加载。",
            }),
            el(
              "div",
              { style: { marginBottom: "10px" } },
              el("button", {
                type: "button",
                className: "btn btn-sm btn-primary",
                text: "+ 上传本地 JDBC .jar 驱动包 (自动校验魔数与 SHA-256)",
                onClick: () => this._handleUploadJar(),
              })
            ),
            ...driverCards
          )
        )
      )
    );

    clearEl(this.root, toolbar, content);
  }
}
