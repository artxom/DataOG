/**
 * 纯安全 DOM Markdown 渲染器（零 innerHTML / 严格防 XSS）
 */

import { badge, copyText, el, svgIcon } from "../../core/dom.js";
import { Toast } from "../../ui/toast.js";

function renderInlineNodes(text) {
  const src = String(text || "");
  const out = [];
  const tokenRe = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\*[^*\n]+\*)/g;
  let lastIdx = 0;
  let m;

  while ((m = tokenRe.exec(src)) !== null) {
    if (m.index > lastIdx) {
      out.push(document.createTextNode(src.slice(lastIdx, m.index)));
    }
    const tok = m[0];
    if (tok.startsWith("`") && tok.endsWith("`")) {
      out.push(el("code", { className: "ai-asst-inline-code", text: tok.slice(1, -1) }));
    } else if (tok.startsWith("**") && tok.endsWith("**")) {
      out.push(el("strong", { text: tok.slice(2, -2) }));
    } else if (tok.startsWith("*") && tok.endsWith("*")) {
      out.push(el("em", { text: tok.slice(1, -1) }));
    }
    lastIdx = tokenRe.lastIndex;
  }
  if (lastIdx < src.length) {
    out.push(document.createTextNode(src.slice(lastIdx)));
  }
  return out;
}

function renderCodeBlock(lang, codeText) {
  const cleanCode = String(codeText || "").replace(/\n$/, "");
  const langLabel = (lang || "sql").trim().toUpperCase();
  return el(
    "div",
    { className: "ai-asst-code-card" },
    el(
      "div",
      { className: "ai-asst-code-head" },
      badge(langLabel || "CODE", "neutral"),
      el(
        "button",
        {
          type: "button",
          className: "btn btn-xs",
          onClick: async () => {
            try {
              await copyText(cleanCode);
              Toast.success("代码已复制");
            } catch (_e) {
              Toast.info("复制失败");
            }
          },
        },
        svgIcon("copy", 11),
        "复制"
      )
    ),
    el("pre", { className: "ai-asst-code-pre mono", text: cleanCode })
  );
}

function isTableSeparatorLine(line) {
  const s = line.trim();
  return /^\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?$/.test(s);
}

function parseTableRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

export function renderSafeMarkdown(markdownText) {
  const root = el("div", { className: "ai-asst-md" });
  const lines = String(markdownText || "").replace(/\r\n/g, "\n").split("\n");
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    const trimmed = line.trim();

    if (!trimmed) {
      i += 1;
      continue;
    }

    // 1. Fenced code block ```
    if (trimmed.startsWith("```")) {
      const lang = trimmed.slice(3).trim();
      const codeLines = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) {
        codeLines.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      root.appendChild(renderCodeBlock(lang, codeLines.join("\n")));
      continue;
    }

    // 2. Headings #..####
    const hMatch = trimmed.match(/^(#{1,4})\s+(.+)$/);
    if (hMatch) {
      const level = Math.min(4, hMatch[1].length);
      root.appendChild(
        el(`h${level + 1}`, { className: `ai-asst-md-h${level}` }, renderInlineNodes(hMatch[2]))
      );
      i += 1;
      continue;
    }

    // 3. Markdown Table
    if (trimmed.includes("|") && i + 1 < lines.length && isTableSeparatorLine(lines[i + 1])) {
      const headers = parseTableRow(trimmed);
      i += 2;
      const bodyRows = [];
      while (i < lines.length && lines[i].trim().includes("|") && !lines[i].trim().startsWith("```")) {
        bodyRows.push(parseTableRow(lines[i]));
        i += 1;
      }
      const tbl = el(
        "div",
        { className: "ai-asst-md-table-wrap" },
        el(
          "table",
          { className: "ai-asst-md-table" },
          el(
            "thead",
            {},
            el(
              "tr",
              {},
              headers.map((h) => el("th", {}, renderInlineNodes(h)))
            )
          ),
          el(
            "tbody",
            {},
            bodyRows.map((r) =>
              el(
                "tr",
                {},
                headers.map((_, idx) => el("td", {}, renderInlineNodes(r[idx] ?? "")))
              )
            )
          )
        )
      );
      root.appendChild(tbl);
      continue;
    }

    // 4. Blockquote >
    if (trimmed.startsWith(">")) {
      const qLines = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        qLines.push(lines[i].trim().replace(/^>\s?/, ""));
        i += 1;
      }
      root.appendChild(
        el("blockquote", { className: "ai-asst-md-quote" }, renderInlineNodes(qLines.join(" ")))
      );
      continue;
    }

    // 5. Unordered or ordered list
    if (/^[-*•]\s+/.test(trimmed) || /^\d+\.\s+/.test(trimmed)) {
      const isOrdered = /^\d+\.\s+/.test(trimmed);
      const listTag = isOrdered ? "ol" : "ul";
      const items = [];
      while (i < lines.length) {
        const cur = lines[i].trim();
        const itemMatch = isOrdered ? cur.match(/^\d+\.\s+(.+)$/) : cur.match(/^[-*•]\s+(.+)$/);
        if (!itemMatch) break;
        items.push(el("li", {}, renderInlineNodes(itemMatch[1])));
        i += 1;
      }
      root.appendChild(el(listTag, { className: "ai-asst-md-list" }, items));
      continue;
    }

    // 6. Paragraph
    const pLines = [];
    while (i < lines.length) {
      const cur = lines[i].trim();
      if (
        !cur ||
        cur.startsWith("```") ||
        cur.startsWith(">") ||
        /^(#{1,4})\s+/.test(cur) ||
        /^[-*•]\s+/.test(cur) ||
        /^\d+\.\s+/.test(cur)
      ) {
        break;
      }
      pLines.push(lines[i].trim());
      i += 1;
    }
    root.appendChild(el("p", { className: "ai-asst-md-p" }, renderInlineNodes(pLines.join(" "))));
  }

  return root;
}
