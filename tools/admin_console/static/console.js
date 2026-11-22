/**
 * Data.O.G 管理员可视化总控台前端控制器 (原生 ES Module · 零 innerHTML · 零阻塞弹窗)
 */

const rootEl = document.getElementById("app-root");

const state = {
  auth: { initialized: true, authenticated: false, username: "" },
  activeTab: "overview",
  overview: null,
  sandboxDetail: null,
  dbeaver: [],
  prompts: [],
  licenseInfo: null,
  licenseHistory: [],
  activeJobId: "",
  activeJob: null,
  pollTimer: null,
  busyAction: false,
  licenseForm: {
    mode: "local",
    daysPreset: "90",
    customDays: "90",
    untilDate: "",
    requestCode: "",
    decodedDevice: null,
    machineCode: "",
    ipAddr: "",
    note: "",
    lastIssued: null,
  },
  sandboxOptions: {
    registryPrefix: "",
  },
};

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, val] of Object.entries(attrs)) {
      if (val === undefined || val === null || val === false) continue;
      if (key === "className") {
        node.className = val;
      } else if (key === "text") {
        node.textContent = String(val);
      } else if (key === "value" && (tag === "input" || tag === "textarea" || tag === "select")) {
        node.value = String(val);
      } else if (key.startsWith("on") && typeof val === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), val);
      } else if (key === "hidden" || key === "disabled" || key === "required" || key === "autofocus") {
        node[key] = Boolean(val);
      } else {
        node.setAttribute(key, String(val));
      }
    }
  }
  for (const child of children.flat()) {
    if (child === undefined || child === null || child === false) continue;
    if (child instanceof Node) {
      node.appendChild(child);
    } else {
      node.appendChild(document.createTextNode(String(child)));
    }
  }
  return node;
}

function clearEl(node) {
  while (node && node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

let toastStackEl = null;
function showToast(message, kind = "ok") {
  if (!toastStackEl) {
    toastStackEl = el("div", { className: "toast-stack" });
    document.body.appendChild(toastStackEl);
  }
  const item = el("div", {
    className: `toast-item ${kind === "err" ? "is-err" : "is-ok"}`,
    text: message,
  });
  toastStackEl.appendChild(item);
  setTimeout(() => {
    item.remove();
  }, 3600);
}

async function copyText(text, label = "内容") {
  const val = String(text ?? "");
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(val);
      showToast(`已复制${label}`);
      return;
    }
  } catch {
    // fallback below
  }
  const ta = el("textarea", { className: "sr-copy-area" });
  ta.value = val;
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    showToast(`已复制${label}`);
  } catch {
    showToast(`复制${label}失败，请手动选中复制`, "err");
  } finally {
    ta.remove();
  }
}

function confirmModal({ title, message, confirmLabel = "确认执行", danger = false }) {
  return new Promise((resolve) => {
    const cancelBtn = el("button", {
      type: "button",
      className: "btn",
      text: "取消",
      onClick: () => {
        dlg.close("cancel");
      },
    });
    const okBtn = el("button", {
      type: "button",
      className: `btn ${danger ? "btn-danger" : "btn-primary"}`,
      text: confirmLabel,
      onClick: () => {
        dlg.close("ok");
      },
    });
    const dlg = el(
      "dialog",
      { className: "console-dialog" },
      el("h3", { className: "dialog-title", text: title }),
      el("div", { className: "dialog-body", text: message }),
      el("div", { className: "btn-row" }, cancelBtn, okBtn)
    );
    dlg.addEventListener("close", () => {
      const accepted = dlg.returnValue === "ok";
      dlg.remove();
      resolve(accepted);
    });
    document.body.appendChild(dlg);
    dlg.showModal();
  });
}

function showLogModal(title, lines) {
  const closeBtn = el("button", {
    type: "button",
    className: "btn btn-primary",
    text: "关闭",
    onClick: () => dlg.close(),
  });
  const pre = el("pre", {
    className: "terminal-box",
    text: lines && lines.length ? lines.join("\n") : "（暂无日志输出）",
  });
  const dlg = el(
    "dialog",
    { className: "console-dialog" },
    el("h3", { className: "dialog-title", text: title }),
    pre,
    el("div", { className: "btn-row stack-gap" }, closeBtn)
  );
  dlg.addEventListener("close", () => dlg.remove());
  document.body.appendChild(dlg);
  dlg.showModal();
  pre.scrollTop = pre.scrollHeight;
}

async function apiFetch(path, options = {}) {
  const opts = {
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    ...options,
  };
  const resp = await fetch(path, opts);
  let data = null;
  try {
    data = await resp.json();
  } catch {
    data = { ok: false, error: `HTTP ${resp.status}` };
  }
  if (resp.status === 401 && path !== "/api/auth/login" && path !== "/api/auth/setup") {
    state.auth.authenticated = false;
    stopJobPolling();
    render();
    throw new Error(data?.error || "登录会话已过期，请重新登录");
  }
  if (!resp.ok || data?.ok === false) {
    throw new Error(data?.error || `请求失败 (${resp.status})`);
  }
  return data;
}

function stopJobPolling() {
  if (state.pollTimer) {
    clearInterval(state.pollTimer);
    state.pollTimer = null;
  }
}

function startJobPolling(jobId) {
  state.activeJobId = jobId;
  stopJobPolling();
  const pollOnce = async () => {
    try {
      const res = await apiFetch(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "GET" });
      state.activeJob = res.job;
      updateJobLogPanelInPlace();
      if (res.job.status !== "running") {
        stopJobPolling();
        if (res.job.status === "completed") {
          showToast(`${res.job.title} 完成`);
        } else {
          showToast(res.job.error || `${res.job.title} 执行失败`, "err");
        }
        await refreshAllData();
      }
    } catch {
      stopJobPolling();
    }
  };
  state.pollTimer = setInterval(pollOnce, 1000);
}

function updateJobLogPanelInPlace() {
  const logEl = document.getElementById("live-job-terminal");
  const stepEl = document.getElementById("live-job-step");
  const statusEl = document.getElementById("live-job-badge");
  const job = state.activeJob;
  if (!job || !logEl) {
    return;
  }
  logEl.textContent = (job.logs && job.logs.length)
    ? job.logs.join("\n")
    : "正在等待任务日志输出...";
  logEl.scrollTop = logEl.scrollHeight;
  if (stepEl) {
    stepEl.textContent = `${job.title} — ${job.step}`;
  }
  if (statusEl) {
    statusEl.className = `pill ${
      job.status === "completed"
        ? "pill-green"
        : job.status === "failed"
        ? "pill-red"
        : "pill-blue"
    }`;
    statusEl.textContent =
      job.status === "completed"
        ? "已完成"
        : job.status === "failed"
        ? "失败"
        : "执行中";
  }
}

async function refreshAllData({ verifyAnomalies = false } = {}) {
  const [ov, dbv, prm, lic, hist] = await Promise.all([
    apiFetch("/api/overview"),
    apiFetch("/api/sandbox/dbeaver"),
    apiFetch("/api/sandbox/prompts"),
    apiFetch("/api/license/info"),
    apiFetch("/api/license/history"),
  ]);
  state.overview = ov;
  state.dbeaver = dbv.connections || [];
  state.prompts = prm.prompts || [];
  state.licenseInfo = lic;
  state.licenseHistory = hist.items || [];

  const sb = await apiFetch(`/api/sandbox/status?verify=${verifyAnomalies ? "true" : "false"}`);
  state.sandboxDetail = sb.sandbox;

  const jobs = ov.jobs || [];
  if (state.activeJobId) {
    const matched = jobs.find((j) => j.job_id === state.activeJobId);
    if (matched) state.activeJob = matched;
  } else if (jobs.length) {
    state.activeJob = jobs[0];
    state.activeJobId = jobs[0].job_id;
    if (jobs[0].status === "running") {
      startJobPolling(jobs[0].job_id);
    }
  }
  render();
}

/* ==================== Auth View ==================== */

function renderAuthGate() {
  const isSetup = !state.auth.initialized;
  let errorText = "";
  const errBox = el("div", { className: "error-banner", hidden: true });
  const userInput = el("input", {
    type: "text",
    className: "input",
    placeholder: "请输入管理员账号",
    autocomplete: "username",
    required: true,
    autofocus: true,
  });
  const passInput = el("input", {
    type: "password",
    className: "input",
    placeholder: "请输入管理员密码",
    autocomplete: isSetup ? "new-password" : "current-password",
    required: true,
  });
  const confirmInput = el("input", {
    type: "password",
    className: "input",
    placeholder: "再次输入以确认密码",
    autocomplete: "new-password",
    required: isSetup,
  });

  const submitBtn = el("button", {
    type: "submit",
    className: "btn btn-primary full-width",
    text: isSetup ? "创建管理员账号并进入控制台" : "登录控制台",
  });

  const form = el(
    "form",
    {
      onSubmit: async (ev) => {
        ev.preventDefault();
        const username = userInput.value.trim();
        const password = passInput.value;
        if (isSetup && password !== confirmInput.value) {
          errBox.textContent = "两次输入的密码不一致";
          errBox.hidden = false;
          return;
        }
        errBox.hidden = true;
        submitBtn.disabled = true;
        try {
          const endpoint = isSetup ? "/api/auth/setup" : "/api/auth/login";
          const res = await apiFetch(endpoint, {
            method: "POST",
            body: JSON.stringify({ username, password }),
          });
          state.auth = {
            initialized: true,
            authenticated: true,
            username: res.username || username,
          };
          await refreshAllData();
        } catch (err) {
          errorText = err.message || "登录失败";
          errBox.textContent = errorText;
          errBox.hidden = false;
        } finally {
          submitBtn.disabled = false;
        }
      },
    },
    errBox,
    el("div", { className: "field" }, el("label", { className: "field-label", text: "管理员账号" }), userInput),
    el("div", { className: "field" }, el("label", { className: "field-label", text: "管理员密码" }), passInput),
    isSetup
      ? el("div", { className: "field" }, el("label", { className: "field-label", text: "确认密码" }), confirmInput)
      : null,
    submitBtn
  );

  const card = el(
    "div",
    { className: "auth-card" },
    el(
      "div",
      { className: "auth-logo-row" },
      el("span", { className: "brand-badge", text: "OG" }),
      el(
        "div",
        null,
        el("h1", {
          className: "auth-title",
          text: isSetup ? "初始化管理员总控台" : "Data.O.G 管理员总控台",
        }),
        el("p", {
          className: "auth-subtitle",
          text: isSetup
            ? "首次使用请先创建本地管理员凭据（仅本地加密保存）"
            : "演示编排 · 工作台启停 · 离线激活码签发中心",
        })
      )
    ),
    form
  );

  return el("div", { className: "auth-stage" }, card);
}

/* ==================== Tab 1: Overview ==================== */

function renderWorkbenchCard(wb) {
  if (!wb) return null;
  const lic = wb.license || {};
  const statusPill = wb.running
    ? el("span", { className: "pill pill-green" }, el("span", { className: "dot" }), `运行中 :${wb.port}`)
    : el("span", { className: "pill pill-muted" }, el("span", { className: "dot" }), "未运行");

  const licPill = lic.authorized
    ? el("span", { className: "pill pill-green", text: `已激活 · 至 ${lic.expires_date || "-"}` })
    : el("span", {
        className: lic.status === "NOT_INITIALIZED" ? "pill pill-muted" : "pill pill-amber",
        text: lic.status_label || lic.status || "未激活",
      });

  const actions = [];
  if (wb.running) {
    actions.push(
      el("button", {
        type: "button",
        className: "btn btn-primary",
        text: "打开工作台 ↗",
        onClick: () => window.open(wb.url, "_blank", "noopener,noreferrer"),
      })
    );
    if (wb.managed) {
      actions.push(
        el("button", {
          type: "button",
          className: "btn btn-danger",
          text: "停止服务",
          onClick: async () => {
            try {
              await apiFetch(`/api/workbenches/${wb.mode}/stop`, { method: "POST" });
              showToast(`已停止 ${wb.label}`);
              await refreshAllData();
            } catch (e) {
              showToast(e.message, "err");
            }
          },
        })
      );
    } else {
      actions.push(el("span", { className: "pill pill-blue", text: "外部手动启动进程" }));
    }
  } else {
    actions.push(
      el("button", {
        type: "button",
        className: "btn btn-success",
        text: "启动工作台",
        disabled: state.busyAction,
        onClick: async () => {
          state.busyAction = true;
          render();
          try {
            const res = await apiFetch(`/api/workbenches/${wb.mode}/start`, { method: "POST" });
            showToast(`${wb.label} 已就绪 (${res.workbench.url})`);
            await refreshAllData();
          } catch (e) {
            showToast(e.message, "err");
          } finally {
            state.busyAction = false;
            render();
          }
        },
      })
    );
  }
  actions.push(
    el("button", {
      type: "button",
      className: "btn",
      text: "查看运行日志",
      onClick: async () => {
        try {
          const res = await apiFetch(`/api/workbenches/${wb.mode}/logs`);
          showLogModal(`${wb.label} 运行日志`, res.logs);
        } catch (e) {
          showToast(e.message, "err");
        }
      },
    })
  );

  return el(
    "div",
    { className: "card" },
    el(
      "div",
      { className: "card-head" },
      el("h3", { className: "card-title", text: wb.label }),
      statusPill
    ),
    el(
      "div",
      { className: "kv-list" },
      el(
        "div",
        { className: "kv-row" },
        el("span", { className: "kv-key", text: "访问地址" }),
        el("span", { className: "kv-val mono", text: wb.url || `http://127.0.0.1:${wb.preferred_port} (默认)` })
      ),
      el(
        "div",
        { className: "kv-row" },
        el("span", { className: "kv-key", text: "授权状态" }),
        el("span", { className: "kv-val" }, licPill)
      ),
      el(
        "div",
        { className: "kv-row" },
        el("span", { className: "kv-key", text: "运行环境" }),
        el("span", {
          className: "kv-val muted-text",
          text: wb.mode === "demo" ? "隔离演示目录（含启明银行双工作区）" : "本机默认正式目录",
        })
      )
    ),
    el("div", { className: "btn-row" }, actions)
  );
}

function renderOverviewTab() {
  const ov = state.overview || {};
  const wbs = ov.workbenches || {};
  const sb = ov.sandbox || {};
  const job = state.activeJob;

  const prefixInput = el("input", {
    type: "text",
    className: "input input-compact",
    placeholder: "可选镜像加速前缀如 docker.m.daocloud.io/",
    value: state.sandboxOptions.registryPrefix,
    onInput: (e) => {
      state.sandboxOptions.registryPrefix = e.target.value;
    },
  });

  const launchDemoBtn = el("button", {
    type: "button",
    className: "btn btn-primary btn-hero",
    text: job?.status === "running" ? "正在一键启动中..." : "一键启动全套演示环境",
    disabled: job?.status === "running",
    onClick: async () => {
      try {
        const res = await apiFetch("/api/one-click-demo", {
          method: "POST",
          body: JSON.stringify({
            registry_prefix: state.sandboxOptions.registryPrefix.trim(),
          }),
        });
        state.activeJob = res.job;
        startJobPolling(res.job.job_id);
        render();
      } catch (e) {
        showToast(e.message, "err");
      }
    },
  });

  const hero = el(
    "div",
    { className: "hero-card" },
    el(
      "div",
      null,
      el("h2", { className: "hero-title", text: "向领导/客户汇报？点一次按钮全套自动就绪" }),
      el("p", {
        className: "hero-sub",
        text: "自动串联：拉起华为DWS(PG16) / 星环(Spark) / 核心MySQL 三容器并灌入3万+金融数据 → 装配双工作区 → 自动签发365天离线激活码 → 启动隔离演示版 Data.O.G。",
      })
    ),
    el("div", { className: "hero-controls" }, prefixInput, launchDemoBtn)
  );

  const engineStrip = el(
    "div",
    { className: "card stack-gap" },
    el(
      "div",
      { className: "card-head" },
      el("h3", { className: "card-title", text: "三引擎演示沙箱实时状态" }),
      el(
        "div",
        { className: "btn-row" },
        el("span", {
          className: `pill ${sb.all_up ? "pill-green" : "pill-amber"}`,
          text: sb.all_up ? "全部 3 引擎在线" : "部分引擎未启动",
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm",
          text: "查看 DBeaver 连接信息",
          onClick: () => {
            state.activeTab = "sandbox";
            render();
          },
        })
      )
    ),
    el(
      "div",
      { className: "grid-3" },
      (sb.engines || []).map((eng) =>
        el(
          "div",
          { className: "kv-row" },
          el("span", { text: eng.label }),
          el(
            "span",
            { className: `pill ${eng.up ? "pill-green" : "pill-red"}` },
            el("span", { className: "dot" }),
            eng.up ? `:${eng.port} 在线` : "离线"
          )
        )
      )
    )
  );

  const jobCard = el(
    "div",
    { className: "card stack-gap" },
    el(
      "div",
      { className: "card-head" },
      el("h3", {
        id: "live-job-step",
        className: "card-title",
        text: job ? `${job.title} — ${job.step}` : "实时任务执行控制台",
      }),
      el(
        "div",
        { className: "btn-row" },
        job?.result?.url
          ? el("button", {
              type: "button",
              className: "btn btn-sm btn-success",
              text: "打开已就绪演示工作台 ↗",
              onClick: () => window.open(job.result.url, "_blank", "noopener,noreferrer"),
            })
          : null,
        el("span", {
          id: "live-job-badge",
          className: `pill ${
            !job
              ? "pill-muted"
              : job.status === "completed"
              ? "pill-green"
              : job.status === "failed"
              ? "pill-red"
              : "pill-blue"
          }`,
          text: !job
            ? "空闲"
            : job.status === "completed"
            ? "已完成"
            : job.status === "failed"
            ? "失败"
            : "执行中",
        })
      )
    ),
    el("pre", {
      id: "live-job-terminal",
      className: "terminal-box",
      text: job && job.logs && job.logs.length
        ? job.logs.join("\n")
        : "尚未触发编排任务，点击上方「一键启动全套演示环境」或沙箱操作后，将在此实时滚动展示执行进度。",
    })
  );

  return el(
    "div",
    null,
    hero,
    el("div", { className: "grid-2" }, renderWorkbenchCard(wbs.demo), renderWorkbenchCard(wbs.main)),
    engineStrip,
    jobCard
  );
}

/* ==================== Tab 2: License Studio ==================== */

function renderLicenseTab() {
  const info = state.licenseInfo || {};
  const dev = info.local_device || {};
  const targets = info.targets || {};
  const f = state.licenseForm;

  const deviceCard = el(
    "div",
    { className: "card" },
    el(
      "div",
      { className: "card-head" },
      el("h3", { className: "card-title", text: "本机指纹与签发引擎状态" }),
      el(
        "div",
        { className: "btn-row" },
        el("span", {
          className: `pill ${info.keygen_ready ? "pill-green" : "pill-red"}`,
          text: info.keygen_ready ? "Keygen 签发器就绪" : "Keygen 未编译",
        }),
        info.can_build_keygen
          ? el("button", {
              type: "button",
              className: "btn btn-sm",
              text: info.keygen_ready ? "重新编译 Keygen" : "一键编译 Keygen",
              onClick: async () => {
                try {
                  const res = await apiFetch("/api/license/build-keygen", { method: "POST" });
                  state.activeJob = res.job;
                  startJobPolling(res.job.job_id);
                  state.activeTab = "overview";
                  render();
                } catch (e) {
                  showToast(e.message, "err");
                }
              },
            })
          : null
      )
    ),
    el(
      "div",
      { className: "kv-list" },
      el(
        "div",
        { className: "kv-row" },
        el("span", { className: "kv-key", text: "主机名称" }),
        el("span", { className: "kv-val mono", text: dev.hostname || "-" })
      ),
      el(
        "div",
        { className: "kv-row" },
        el("span", { className: "kv-key", text: "本机机器码" }),
        el(
          "span",
          { className: "kv-val" },
          el("code", { text: dev.machine_code || "-" }),
          el("button", {
            type: "button",
            className: "btn btn-sm",
            text: "复制",
            onClick: () => copyText(dev.machine_code, "机器码"),
          })
        )
      ),
      el(
        "div",
        { className: "kv-row" },
        el("span", { className: "kv-key", text: "本机主 IP" }),
        el(
          "span",
          { className: "kv-val" },
          el("code", { text: dev.primary_ip || "-" }),
          el("button", {
            type: "button",
            className: "btn btn-sm",
            text: "复制",
            onClick: () => copyText(dev.primary_ip, "IP"),
          })
        )
      ),
      el(
        "div",
        { className: "kv-row" },
        el("span", { className: "kv-key", text: "本机设备申请码" }),
        el(
          "span",
          { className: "kv-val" },
          el("button", {
            type: "button",
            className: "btn btn-sm",
            text: "复制 REQ1 申请码",
            onClick: () => copyText(dev.request_code, "设备申请码"),
          })
        )
      ),
      el(
        "div",
        { className: "kv-row" },
        el("span", { className: "kv-key", text: "演示版 / 正式版当前授权" }),
        el(
          "span",
          { className: "kv-val btn-row" },
          el("span", {
            className: `pill ${targets.demo?.license?.authorized ? "pill-green" : "pill-muted"}`,
            text: targets.demo?.license?.authorized
              ? `演示版至 ${targets.demo.license.expires_date}`
              : `演示版: ${targets.demo?.license?.status_label || "未激活"}`,
          }),
          el("span", {
            className: `pill ${targets.main?.license?.authorized ? "pill-green" : "pill-muted"}`,
            text: targets.main?.license?.authorized
              ? `正式版至 ${targets.main.license.expires_date}`
              : `正式版: ${targets.main?.license?.status_label || "未激活"}`,
          })
        )
      )
    )
  );

  const modes = [
    { id: "local", label: "本机专属" },
    { id: "request", label: "解析客户申请码 (REQ1)" },
    { id: "manual", label: "手工指定机器码 + IP" },
    { id: "global", label: "全局通用 (无视机器/IP)" },
  ];

  const durationPresets = [
    { id: "7", label: "7 天" },
    { id: "15", label: "15 天" },
    { id: "30", label: "30 天" },
    { id: "90", label: "90 天" },
    { id: "180", label: "180 天" },
    { id: "365", label: "365 天" },
    { id: "custom", label: "自定义天数" },
    { id: "until", label: "指定截止日期" },
  ];

  let extraModeFields = null;
  if (f.mode === "request") {
    const reqTa = el("textarea", {
      className: "textarea mono",
      placeholder: "粘贴客户在 Data.O.G 登录页复制的 REQ1-... 设备申请码",
      value: f.requestCode,
      onInput: (e) => {
        f.requestCode = e.target.value;
      },
    });
    extraModeFields = el(
      "div",
      { className: "field" },
      el("label", { className: "field-label", text: "客户设备申请码 (REQ1-...)" }),
      reqTa,
      el(
        "div",
        { className: "btn-row" },
        el("button", {
          type: "button",
          className: "btn btn-sm",
          text: "解析申请码信息",
          onClick: async () => {
            try {
              const res = await apiFetch("/api/license/decode-request", {
                method: "POST",
                body: JSON.stringify({ request_code: f.requestCode }),
              });
              f.decodedDevice = res.device;
              render();
            } catch (e) {
              showToast(e.message, "err");
            }
          },
        }),
        f.decodedDevice
          ? el("span", {
              className: "pill pill-green",
              text: `已识别: ${f.decodedDevice.hostname || "-"} | ${f.decodedDevice.machine_code} | ${f.decodedDevice.primary_ip}`,
            })
          : null
      )
    );
  } else if (f.mode === "manual") {
    extraModeFields = el(
      "div",
      { className: "grid-2" },
      el(
        "div",
        { className: "field" },
        el("label", { className: "field-label", text: "目标机器码 (如 MCH-XXXX-...)" }),
        el("input", {
          type: "text",
          className: "input mono",
          placeholder: "MCH-XXXX-XXXX-XXXX-XXXX",
          value: f.machineCode,
          onInput: (e) => {
            f.machineCode = e.target.value;
          },
        })
      ),
      el(
        "div",
        { className: "field" },
        el("label", { className: "field-label", text: "目标主机 IPv4 地址" }),
        el("input", {
          type: "text",
          className: "input mono",
          placeholder: "192.168.1.100",
          value: f.ipAddr,
          onInput: (e) => {
            f.ipAddr = e.target.value;
          },
        })
      )
    );
  }

  let durationInputEl = null;
  if (f.daysPreset === "custom") {
    durationInputEl = el(
      "div",
      { className: "field" },
      el("label", { className: "field-label", text: "有效天数 (1 ~ 36500)" }),
      el("input", {
        type: "number",
        min: "1",
        max: "36500",
        className: "input",
        value: f.customDays,
        onInput: (e) => {
          f.customDays = e.target.value;
        },
      })
    );
  } else if (f.daysPreset === "until") {
    durationInputEl = el(
      "div",
      { className: "field" },
      el("label", { className: "field-label", text: "截止日期 (含当日 23:59:59)" }),
      el("input", {
        type: "date",
        className: "input",
        value: f.untilDate,
        onInput: (e) => {
          f.untilDate = e.target.value;
        },
      })
    );
  }

  const issueCard = el(
    "div",
    { className: "card" },
    el("div", { className: "card-head" }, el("h3", { className: "card-title", text: "签发新离线激活码" })),
    el("label", { className: "field-label", text: "授权范围" }),
    el(
      "div",
      { className: "seg-group" },
      modes.map((m) =>
        el("button", {
          type: "button",
          className: `seg-btn ${f.mode === m.id ? "is-active" : ""}`,
          text: m.label,
          onClick: () => {
            f.mode = m.id;
            render();
          },
        })
      )
    ),
    extraModeFields,
    el("label", { className: "field-label", text: "有效期限" }),
    el(
      "div",
      { className: "seg-group" },
      durationPresets.map((d) =>
        el("button", {
          type: "button",
          className: `seg-btn ${f.daysPreset === d.id ? "is-active" : ""}`,
          text: d.label,
          onClick: () => {
            f.daysPreset = d.id;
            render();
          },
        })
      )
    ),
    durationInputEl,
    el(
      "div",
      { className: "field" },
      el("label", { className: "field-label", text: "备注说明（仅保存在本地台账）" }),
      el("input", {
        type: "text",
        className: "input",
        placeholder: "如：给张总演示笔记本 / 启明银行 PoC 测试",
        value: f.note,
        onInput: (e) => {
          f.note = e.target.value;
        },
      })
    ),
    el("button", {
      type: "button",
      className: "btn btn-primary",
      disabled: !info.keygen_ready || state.busyAction,
      text: "立即签发 Ed25519 离线激活码",
      onClick: async () => {
        state.busyAction = true;
        render();
        try {
          const payload = {
            mode: f.mode,
            request_code: f.requestCode,
            machine_code: f.machineCode,
            ip_addr: f.ipAddr,
            note: f.note,
            until_date: f.daysPreset === "until" ? f.untilDate : "",
            days:
              f.daysPreset === "until"
                ? null
                : Number(f.daysPreset === "custom" ? f.customDays : f.daysPreset),
          };
          const res = await apiFetch("/api/license/issue", {
            method: "POST",
            body: JSON.stringify(payload),
          });
          f.lastIssued = res.record;
          await copyText(res.record.activation_code, "新激活码");
          await refreshAllData();
        } catch (e) {
          showToast(e.message, "err");
        } finally {
          state.busyAction = false;
          render();
        }
      },
    }),
    f.lastIssued
      ? el(
          "div",
          { className: "result-banner" },
          el("div", {
            className: "card-title",
            text: `签发成功 · ${f.lastIssued.scope_label}（有效至 ${f.lastIssued.expires_date}，共 ${f.lastIssued.days} 天）`,
          }),
          el("div", { className: "code-box", text: f.lastIssued.activation_code }),
          el(
            "div",
            { className: "btn-row" },
            el("button", {
              type: "button",
              className: "btn btn-sm btn-primary",
              text: "复制激活码",
              onClick: () => copyText(f.lastIssued.activation_code, "激活码"),
            }),
            el("button", {
              type: "button",
              className: "btn btn-sm btn-success",
              text: "一键激活本机「演示版」",
              onClick: () => applyCodeToTarget(f.lastIssued.activation_code, "demo"),
            }),
            el("button", {
              type: "button",
              className: "btn btn-sm btn-success",
              text: "一键激活本机「正式版」",
              onClick: () => applyCodeToTarget(f.lastIssued.activation_code, "main"),
            })
          )
        )
      : null
  );

  const historyCard = el(
    "div",
    { className: "card stack-gap" },
    el("div", { className: "card-head" }, el("h3", { className: "card-title", text: "本地签发台账（最多保留 200 条）" })),
    state.licenseHistory.length === 0
      ? el("div", { className: "muted-text", text: "暂无签发记录。" })
      : el(
          "div",
          { className: "table-wrap" },
          el(
            "table",
            { className: "data-table" },
            el(
              "thead",
              null,
              el(
                "tr",
                null,
                el("th", { text: "签发时间" }),
                el("th", { text: "类型" }),
                el("th", { text: "绑定目标" }),
                el("th", { text: "到期日" }),
                el("th", { text: "备注" }),
                el("th", { text: "操作" })
              )
            ),
            el(
              "tbody",
              null,
              state.licenseHistory.map((item) =>
                el(
                  "tr",
                  null,
                  el("td", { className: "mono", text: item.issued_at }),
                  el("td", { text: item.scope_label || item.scope }),
                  el("td", {
                    className: "mono",
                    text:
                      item.scope === "GLOBAL"
                        ? "全机器通用"
                        : `${item.target_machine || "-"} (${item.target_ip || "-"})`,
                  }),
                  el("td", { className: "mono", text: `${item.expires_date} (${item.days}天)` }),
                  el("td", { text: item.note || "-" }),
                  el(
                    "td",
                    null,
                    el(
                      "div",
                      { className: "btn-row" },
                      el("button", {
                        type: "button",
                        className: "btn btn-sm",
                        text: "复制",
                        onClick: () => copyText(item.activation_code, "历史激活码"),
                      }),
                      el("button", {
                        type: "button",
                        className: "btn btn-sm btn-success",
                        text: "激活演示版",
                        onClick: () => applyCodeToTarget(item.activation_code, "demo"),
                      }),
                      el("button", {
                        type: "button",
                        className: "btn btn-sm btn-danger",
                        text: "删除",
                        onClick: async () => {
                          const ok = await confirmModal({
                            title: "删除签发记录",
                            message: "确定从本地台账删除这条激活码记录吗？已签发的激活码本身仍按有效期工作。",
                            danger: true,
                          });
                          if (!ok) return;
                          await apiFetch(`/api/license/history/${encodeURIComponent(item.id)}`, {
                            method: "DELETE",
                          });
                          await refreshAllData();
                        },
                      })
                    )
                  )
                )
              )
            )
          )
        )
  );

  return el("div", null, el("div", { className: "grid-2" }, deviceCard, issueCard), historyCard);
}

async function applyCodeToTarget(code, target) {
  try {
    await apiFetch("/api/license/apply", {
      method: "POST",
      body: JSON.stringify({ activation_code: code, target }),
    });
    showToast(`已成功激活本机 ${target === "demo" ? "演示版" : "正式版"} Data.O.G`);
    await refreshAllData();
  } catch (e) {
    showToast(e.message, "err");
  }
}

/* ==================== Tab 3: Sandbox & DBeaver ==================== */

function renderSandboxTab() {
  const sb = state.sandboxDetail || state.overview?.sandbox || {};
  const anomalies = sb.anomalies;

  async function runSandboxAction(action, confirmMsg = "") {
    if (confirmMsg) {
      const ok = await confirmModal({ title: "确认沙箱操作", message: confirmMsg, danger: action === "down" });
      if (!ok) return;
    }
    try {
      const res = await apiFetch("/api/sandbox/action", {
        method: "POST",
        body: JSON.stringify({
          action,
          registry_prefix: state.sandboxOptions.registryPrefix.trim(),
        }),
      });
      state.activeJob = res.job;
      startJobPolling(res.job.job_id);
      state.activeTab = "overview";
      render();
    } catch (e) {
      showToast(e.message, "err");
    }
  }

  const controlCard = el(
    "div",
    { className: "card" },
    el(
      "div",
      { className: "card-head" },
      el("h3", { className: "card-title", text: "启明商业银行三引擎 Docker 沙箱控制面板" }),
      el(
        "div",
        { className: "btn-row" },
        el("button", {
          type: "button",
          className: "btn btn-sm",
          text: "核验 5 大业务暗雷",
          onClick: async () => {
            try {
              await refreshAllData({ verifyAnomalies: true });
              showToast("已完成三库实时暗雷行数核验");
            } catch (e) {
              showToast(e.message, "err");
            }
          },
        })
      )
    ),
    !sb.docker_available && sb.docker_error
      ? el("div", { className: "error-banner", text: `Docker 探测提示：${sb.docker_error}` })
      : null,
    el(
      "div",
      { className: "grid-3 stack-gap" },
      (sb.engines || []).map((eng) =>
        el(
          "div",
          { className: "card" },
          el(
            "div",
            { className: "card-head" },
            el("strong", { text: eng.label }),
            el(
              "span",
              { className: `pill ${eng.up ? "pill-green" : "pill-red"}` },
              el("span", { className: "dot" }),
              eng.up ? "UP" : "DOWN"
            )
          ),
          el("div", { className: "muted-text mono", text: `${eng.host}:${eng.port}/${eng.database}` }),
          el("div", { className: "muted-text", text: eng.docker_status || "-" })
        )
      )
    ),
    el(
      "div",
      { className: "btn-row stack-gap" },
      el("button", {
        type: "button",
        className: "btn btn-primary",
        text: "启动并灌入数据 (up)",
        onClick: () => runSandboxAction("up"),
      }),
      el("button", {
        type: "button",
        className: "btn btn-success",
        text: "5 秒极速还原 DWS 靶表",
        onClick: () => runSandboxAction("reset_dws"),
      }),
      el("button", {
        type: "button",
        className: "btn",
        text: "全三库复原 (reset)",
        onClick: () => runSandboxAction("reset"),
      }),
      el("button", {
        type: "button",
        className: "btn",
        text: "离线生成标准 Excel 与双工作区",
        onClick: () => runSandboxAction("generate_assets"),
      }),
      el("button", {
        type: "button",
        className: "btn",
        text: "重建演示隔离目录",
        onClick: () =>
          runSandboxAction(
            "rebuild_demo_home",
            "将清空并重新导入演示隔离工作区与三引擎凭据（会先停止运行中的演示工作台），确认继续？"
          ),
      }),
      el("button", {
        type: "button",
        className: "btn btn-danger",
        text: "关闭容器释放内存 (down)",
        onClick: () =>
          runSandboxAction(
            "down",
            "将停止并移除本地三引擎演示容器以释放内存（随时可通过一键启动恢复），确认关闭？"
          ),
      })
    ),
    anomalies
      ? el(
          "div",
          { className: "result-banner" },
          el("div", { className: "card-title", text: "五大金融业务暗雷实时核验结果" }),
          el(
            "div",
            { className: "kv-list" },
            el("div", { className: "kv-row" }, el("span", { text: "暗雷1a 负余额不良借据" }), el("code", { text: `${anomalies.dws.neg_npl_count} 笔 / ${anomalies.dws.neg_npl_sum.toLocaleString()} 元` })),
            el("div", { className: "kv-row" }, el("span", { text: "暗雷1b 逾期>180天错标正常" }), el("code", { text: `${anomalies.dws.concealed_npl_count} 笔 / ${anomalies.dws.concealed_npl_sum.toLocaleString()} 元` })),
            el("div", { className: "kv-row" }, el("span", { text: "暗雷2 反洗钱拆单 (4.95~4.99万)" }), el("code", { text: `${anomalies.dws.aml_split_count} 笔 / ${anomalies.dws.aml_split_sum.toLocaleString()} 元` })),
            el("div", { className: "kv-row" }, el("span", { text: "暗雷3 核心MySQL vs DWS 授信差额" }), el("code", { text: `${anomalies.mysql.recon_gap_vs_dws.toLocaleString()} 元 (MySQL ${anomalies.mysql.core_credit_count} vs DWS ${anomalies.dws.dws_credit_count})` })),
            el("div", { className: "kv-row" }, el("span", { text: "暗雷4 >=1000万超低息无押品贷款" }), el("code", { text: `${anomalies.dws.zero_rate_unsecured_count} 笔 / ${anomalies.dws.zero_rate_unsecured_sum.toLocaleString()} 元` }))
          )
        )
      : null
  );

  const dbeaverHeader = el(
    "div",
    { className: "card-head stack-gap" },
    el("h3", { className: "card-title", text: "本机 DBeaver 直连配置与 JDBC URL 速查" }),
    el("button", {
      type: "button",
      className: "btn btn-sm",
      text: "一键复制全部三库连接摘要",
      onClick: () => {
        const text = state.dbeaver
          .map(
            (c) =>
              `【${c.name}】\n驱动: ${c.driver}\nJDBC URL: ${c.jdbc_url}\n用户名: ${c.username}\n密码: ${c.password || c.password_note || "(留空)"}\n备注: ${c.tips}`
          )
          .join("\n\n");
        copyText(text, "全部 DBeaver 连接信息");
      },
    })
  );

  const dbeaverCards = el(
    "div",
    { className: "grid-3" },
    state.dbeaver.map((conn) =>
      el(
        "div",
        { className: "card" },
        el(
          "div",
          { className: "card-head" },
          el("strong", { text: conn.name }),
          el("span", { className: "pill pill-blue", text: conn.driver })
        ),
        el(
          "div",
          { className: "kv-list" },
          el("div", { className: "kv-row" }, el("span", { className: "kv-key", text: "主机 : 端口" }), el("code", { text: `${conn.host}:${conn.port}` })),
          el("div", { className: "kv-row" }, el("span", { className: "kv-key", text: "数据库 / 库名" }), el("code", { text: conn.database })),
          el("div", { className: "kv-row" }, el("span", { className: "kv-key", text: "Schema 列表" }), el("code", { text: (conn.schemas || []).join(", ") })),
          el(
            "div",
            { className: "kv-row" },
            el("span", { className: "kv-key", text: "用户名" }),
            el(
              "span",
              { className: "kv-val" },
              el("code", { text: conn.username }),
              el("button", {
                type: "button",
                className: "btn btn-sm",
                text: "复制",
                onClick: () => copyText(conn.username, "用户名"),
              })
            )
          ),
          el(
            "div",
            { className: "kv-row" },
            el("span", { className: "kv-key", text: "密码" }),
            el(
              "span",
              { className: "kv-val" },
              el("code", { text: conn.password || conn.password_note || "留空" }),
              conn.password
                ? el("button", {
                    type: "button",
                    className: "btn btn-sm",
                    text: "复制",
                    onClick: () => copyText(conn.password, "密码"),
                  })
                : null
            )
          )
        ),
        el("div", { className: "field-label", text: "JDBC URL（在 DBeaver 选择「按 URL 连接」直接粘贴）" }),
        el("div", { className: "code-box", text: conn.jdbc_url }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-primary",
          text: "复制 JDBC URL",
          onClick: () => copyText(conn.jdbc_url, `${conn.name} JDBC URL`),
        }),
        el("div", { className: "tip-box", text: conn.tips })
      )
    )
  );

  return el("div", null, controlCard, el("div", { className: "card stack-gap" }, dbeaverHeader, dbeaverCards));
}

/* ==================== Tab 4: Playbook & Security ==================== */

function renderPlaybookTab() {
  const promptCards = state.prompts.map((item) =>
    el(
      "div",
      { className: "prompt-card stack-gap" },
      el(
        "div",
        { className: "card-head" },
        el("strong", { text: item.title }),
        el("span", { className: "pill pill-blue", text: item.target })
      ),
      el("div", { className: "code-box", text: item.prompt }),
      el(
        "div",
        { className: "btn-row" },
        el("button", {
          type: "button",
          className: "btn btn-sm btn-primary",
          text: "一键复制此 AI 提问",
          onClick: () => copyText(item.prompt, "AI 演示提问词"),
        })
      ),
      el("div", { className: "prompt-expected", text: `预期实时查出精确结果：${item.expected}` })
    )
  );

  const oldPw = el("input", { type: "password", className: "input", required: true });
  const newPw = el("input", { type: "password", className: "input", required: true });
  const confirmPw = el("input", { type: "password", className: "input", required: true });

  const pwCard = el(
    "div",
    { className: "card stack-gap" },
    el("div", { className: "card-head" }, el("h3", { className: "card-title", text: `管理员账号安全设置（当前登录：${state.auth.username}）` })),
    el(
      "form",
      {
        onSubmit: async (ev) => {
          ev.preventDefault();
          if (newPw.value !== confirmPw.value) {
            showToast("两次输入的新密码不一致", "err");
            return;
          }
          try {
            await apiFetch("/api/auth/change-password", {
              method: "POST",
              body: JSON.stringify({
                old_password: oldPw.value,
                new_password: newPw.value,
              }),
            });
            oldPw.value = "";
            newPw.value = "";
            confirmPw.value = "";
            showToast("管理员密码已修改，本机凭据文件已同步加密更新");
          } catch (e) {
            showToast(e.message, "err");
          }
        },
      },
      el(
        "div",
        { className: "grid-3" },
        el("div", { className: "field" }, el("label", { className: "field-label", text: "当前密码" }), oldPw),
        el("div", { className: "field" }, el("label", { className: "field-label", text: "新密码（至少 6 位）" }), newPw),
        el("div", { className: "field" }, el("label", { className: "field-label", text: "确认新密码" }), confirmPw)
      ),
      el("button", { type: "submit", className: "btn btn-primary", text: "更新管理员密码" })
    )
  );

  return el("div", null, ...promptCards, pwCard);
}

/* ==================== Main Shell ==================== */

function renderShell() {
  const navItems = [
    { id: "overview", label: "总览与一键启停" },
    { id: "license", label: "激活码签发中心" },
    { id: "sandbox", label: "演示沙箱与 DBeaver" },
    { id: "playbook", label: "演练提问与账号安全" },
  ];

  const pageHeaders = {
    overview: ["总览与一键启停", "无需记忆任何命令行，直接一键拉起全部容器、装配工作区、自动激活并启动 Data.O.G。"],
    license: ["离线激活码签发中心", "可视化解析客户申请码、签发本机/客户/全局 Ed25519 激活码，并可一键写入本地工作台。"],
    sandbox: ["启明银行三引擎沙箱与 DBeaver 速查", "集中管理华为DWS(PG16)、星环Inceptor(Spark)与核心MySQL，一键复制 DBeaver JDBC 连接信息。"],
    playbook: ["高层演练 AI 提问剧本与管理员安全", "一键复制 5 个暗雷 AI 探查提问词与 4 道只读钓鱼测试词，以及修改控制台管理员密码。"],
  };
  const [title, desc] = pageHeaders[state.activeTab] || pageHeaders.overview;

  const sidebar = el(
    "aside",
    { className: "sidebar" },
    el(
      "div",
      { className: "sidebar-brand" },
      el("span", { className: "brand-badge", text: "OG" }),
      el(
        "div",
        null,
        el("div", { className: "sidebar-brand-title", text: "Data.O.G 总控台" }),
        el("div", { className: "sidebar-brand-sub", text: "Admin & Demo Cockpit" })
      )
    ),
    el(
      "nav",
      { className: "nav-list" },
      navItems.map((n) =>
        el("button", {
          type: "button",
          className: `nav-btn ${state.activeTab === n.id ? "is-active" : ""}`,
          text: n.label,
          onClick: () => {
            state.activeTab = n.id;
            render();
          },
        })
      )
    ),
    el(
      "div",
      { className: "sidebar-footer" },
      el(
        "div",
        { className: "user-pill" },
        el("span", { text: `管理员: ${state.auth.username}` }),
        el("span", { className: "pill pill-green", text: "在线" })
      ),
      el(
        "div",
        { className: "btn-row" },
        el("button", {
          type: "button",
          className: "btn btn-sm full-width",
          text: "刷新全局状态",
          onClick: async () => {
            await refreshAllData();
            showToast("状态已刷新");
          },
        }),
        el("button", {
          type: "button",
          className: "btn btn-sm btn-danger full-width",
          text: "安全退出登录",
          onClick: async () => {
            await apiFetch("/api/auth/logout", { method: "POST" });
            stopJobPolling();
            state.auth.authenticated = false;
            render();
          },
        })
      )
    )
  );

  let tabBody = null;
  if (state.activeTab === "overview") tabBody = renderOverviewTab();
  else if (state.activeTab === "license") tabBody = renderLicenseTab();
  else if (state.activeTab === "sandbox") tabBody = renderSandboxTab();
  else tabBody = renderPlaybookTab();

  const mainArea = el(
    "main",
    { className: "main-area" },
    el(
      "div",
      { className: "page-header" },
      el(
        "div",
        null,
        el("h1", { className: "page-title", text: title }),
        el("p", { className: "page-desc", text: desc })
      )
    ),
    tabBody
  );

  return el("div", { className: "console-frame" }, sidebar, mainArea);
}

function render() {
  clearEl(rootEl);
  if (!state.auth.authenticated) {
    rootEl.appendChild(renderAuthGate());
  } else {
    rootEl.appendChild(renderShell());
  }
}

async function bootstrap() {
  try {
    const authRes = await apiFetch("/api/auth/status");
    state.auth = {
      initialized: Boolean(authRes.initialized),
      authenticated: Boolean(authRes.authenticated),
      username: authRes.username || "",
    };
    if (state.auth.authenticated) {
      await refreshAllData();
    } else {
      render();
    }
  } catch (err) {
    clearEl(rootEl);
    rootEl.appendChild(
      el("div", { className: "auth-stage" }, el("div", { className: "error-banner", text: err.message }))
    );
  }
}

bootstrap();
