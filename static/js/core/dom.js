/**
 * Data.O.G 前端安全 DOM 构造工具 (零 innerHTML / 严格防 XSS)
 */

const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * 安全创建 HTML 元素，所有字符串子节点均通过 createTextNode / textContent 注入
 * @param {string} tag
 * @param {Record<string, any>} [props]
 * @param {...any} children
 * @returns {HTMLElement}
 */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  if (props && typeof props === "object") {
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "className" || k === "class") {
        node.className = String(v);
      } else if (k === "text") {
        node.textContent = String(v);
      } else if (k === "indeterminate") {
        node.indeterminate = Boolean(v);
      } else if (k === "dataset" && typeof v === "object") {
        for (const [dk, dv] of Object.entries(v)) {
          if (dv !== undefined && dv !== null) {
            node.dataset[dk] = String(dv);
          }
        }
      } else if (k === "style" && typeof v === "object") {
        for (const [sk, sv] of Object.entries(v)) {
          if (sv !== undefined && sv !== null) {
            node.style[sk] = String(sv);
          }
        }
      } else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2).toLowerCase(), v);
      } else if (v === true) {
        node.setAttribute(k, "");
        if (k in node) {
          try {
            node[k] = true;
          } catch (_e) {
            // 忽略只读属性
          }
        }
      } else {
        node.setAttribute(k, String(v));
        if (k === "value" && "value" in node) {
          node.value = String(v);
        }
      }
    }
  }

  const appendChildSafe = (child) => {
    if (child === undefined || child === null || child === false) return;
    if (Array.isArray(child)) {
      child.forEach(appendChildSafe);
    } else if (child instanceof Node) {
      node.appendChild(child);
    } else {
      node.appendChild(document.createTextNode(String(child)));
    }
  };

  children.forEach(appendChildSafe);
  return node;
}

/**
 * 清空容器并可选挂载新子节点
 * @param {HTMLElement} container
 * @param {...any} children
 */
export function clearEl(container, ...children) {
  if (!container) return;
  container.replaceChildren();
  for (const ch of children) {
    if (ch === undefined || ch === null || ch === false) continue;
    if (Array.isArray(ch)) {
      ch.forEach((item) => {
        if (item instanceof Node) container.appendChild(item);
        else if (item !== undefined && item !== null && item !== false) {
          container.appendChild(document.createTextNode(String(item)));
        }
      });
    } else if (ch instanceof Node) {
      container.appendChild(ch);
    } else {
      container.appendChild(document.createTextNode(String(ch)));
    }
  }
}

const ICON_PATHS = {
  settings:
    "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7zm7.4 2.5l1.6 1-1.6 1 .2 1.9-1.8.8-.8 1.8-1.9-.2-1 1.6-1-1.6-1.9.2-.8-1.8-1.8-.8.2-1.9-1.6-1 1.6-1-.2-1.9 1.8-.8.8-1.8 1.9.2 1-1.6 1 1.6 1.9-.2.8 1.8 1.8.8-.2 1.9z",
  explorer:
    "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm3.8 5.2l-2.3 5.3-5.3 2.3 2.3-5.3 5.3-2.3z",
  standard:
    "M12 3l8 4.5-8 4.5-8-4.5L12 3zm-8 9l8 4.5 8-4.5m-16 4.5L12 21l8-4.5",
  polyglot:
    "M4 5h8M8 3v2m-2.5 0c.5 3.5 2.5 6 5.5 7.5M5.5 12.5C8 11 9.5 8.5 10 5m3 15l3.5-9 3.5 9m-5.8-3h4.6",
  sparkles:
    "M12 3l1.8 4.6L18.5 9l-4.7 1.4L12 15l-1.8-4.6L5.5 9l4.7-1.4L12 3zm6.5 11l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9.9-2.1z",
  search: "M11 4a7 7 0 1 0 4.9 12l4.1 4.1M16 16l4 4",
  plug: "M12 2v4m-4 0h8a2 2 0 0 1 2 2v3a6 6 0 0 1-5 5.91V22h-2v-5.09A6 6 0 0 1 6 11V8a2 2 0 0 1 2-2z",
  terminal: "M4 6l6 5-6 5m8 0h8",
  shield: "M12 3l7 3v5c0 5-3.5 8.5-7 10-3.5-1.5-7-5-7-10V6l7-3zm-2 9l2 2 4-4",
  columns: "M3 5h18v14H3V5zm6 0v14m6-14v14",
  translate: "M4 5h8M8 3v2m-2.5 0c.5 3.5 2.5 6 5.5 7.5M5.5 12.5C8 11 9.5 8.5 10 5m3 15l3.5-9 3.5 9m-5.8-3h4.6",
  sidebar: "M3 4h18v16H3V4zm6 0v16",
  logs: "M4 6h16M4 12h16M4 18h10",
  refresh: "M20 11a8 8 0 1 0-2.3 5.6M20 4v7h-7",
  table: "M3 5h18v14H3V5zm0 5h18M9 10v9",
  view: "M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6zm10 3a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  folder: "M3 6h6l2 2h10v10H3V6z",
  upload: "M12 16V4m0 0l-4 4m4-4l4 4M4 18v2h16v-2",
  copy: "M9 9h10v11H9V9zm-4 6H4V4h10v1",
  plus: "M12 5v14M5 12h14",
  trash: "M4 7h16M9 7V4h6v3m-7 3v7m4-7v7m4-7v7M6 7l1 13h10l1-13",
  send: "M21 3L10 14M21 3l-7 18-4-7-7-4 18-7z",
};

/**
 * 使用 createElementNS 安全绘制矢量图标
 * @param {string} name
 * @param {number} [size]
 */
export function svgIcon(name, size = 18) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", ICON_PATHS[name] || ICON_PATHS.table);
  svg.appendChild(path);
  return svg;
}

export function badge(text, tone = "neutral") {
  return el("span", { className: `badge badge-${tone}`, text: String(text ?? "") });
}

export function statusDot(text, tone = "muted") {
  const dotTone =
    tone === "success"
      ? "dot-success"
      : tone === "warn"
      ? "dot-warn"
      : tone === "danger"
      ? "dot-danger"
      : tone === "info"
      ? "dot-info"
      : "dot-muted";
  return el(
    "span",
    { className: "status-dot-wrap" },
    el("span", { className: `dot ${dotTone}` }),
    el("span", { text: String(text ?? "") })
  );
}

/**
 * 渲染软件品牌主图标（支持本地自定义上传图片覆盖）
 */
export function renderBrandMark(appearance, size = "lg") {
  const customUrl = appearance?.brand_icon_url || "";
  const isSmall = size === "sm";
  const isMed = size === "md";
  const box = el("span", {
    className: `squircle-icon squircle-brand ${isSmall ? "squircle-sm" : isMed ? "squircle-md" : ""}`,
    style: isSmall
      ? { width: "16px", height: "16px", borderRadius: "4px", boxShadow: "none" }
      : isMed
      ? { width: "40px", height: "40px", borderRadius: "10px" }
      : { width: "54px", height: "54px", borderRadius: "14px" },
  });

  if (customUrl) {
    box.appendChild(
      el("img", {
        className: "squircle-custom-img",
        src: customUrl,
        alt: "Data.O.G",
      })
    );
  } else {
    box.appendChild(
      el("span", {
        className: "brand-monogram",
        style: { fontSize: isSmall ? "8.5px" : isMed ? "15px" : "20px" },
        text: "OG",
      })
    );
  }
  return box;
}

/**
 * 渲染四大桌面模块超椭圆图标（支持本地自定义上传图片覆盖）
 */
export function renderAppSquircle(appId, appearance, size = "lg") {
  const customUrl = appearance?.app_icons?.[appId] || "";
  const sizeCls = size === "sm" ? "squircle-sm" : size === "md" ? "squircle-md" : "";
  const iconSize = size === "sm" ? 15 : size === "md" ? 20 : 28;
  const box = el("span", {
    className: `squircle-icon squircle-${appId} ${sizeCls}`.trim(),
  });

  if (customUrl) {
    box.appendChild(
      el("img", {
        className: "squircle-custom-img",
        src: customUrl,
        alt: appId,
      })
    );
  } else {
    box.appendChild(svgIcon(appId, iconSize));
  }
  return box;
}

export function formatBytes(bytes) {
  const num = Number(bytes || 0);
  if (num < 1024) return `${num} B`;
  if (num < 1024 * 1024) return `${(num / 1024).toFixed(1)} KB`;
  return `${(num / (1024 * 1024)).toFixed(2)} MB`;
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename || "download.bin" });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

export async function copyText(text) {
  const val = String(text || "");
  if (!val.trim()) return false;
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(val);
    return true;
  }
  const ta = el("textarea", { value: val, style: { position: "fixed", opacity: "0" } });
  document.body.appendChild(ta);
  ta.select();
  document.execCommand("copy");
  ta.remove();
  return true;
}

/**
 * 唤起本地文件选择框并返回选中的 File 对象
 * @param {string} accept
 * @returns {Promise<File | null>}
 */
export function pickLocalFile(accept = "*/*") {
  return new Promise((resolve) => {
    const input = el("input", { type: "file", accept, style: { display: "none" } });
    input.addEventListener("change", () => {
      const f = input.files && input.files[0] ? input.files[0] : null;
      input.remove();
      resolve(f);
    });
    document.body.appendChild(input);
    input.click();
  });
}
