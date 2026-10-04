// A small Markdown renderer that builds DOM nodes (never HTML strings), so nothing in an answer can become
// markup or script. Covers what a chat needs: headings, paragraphs, lists, quotes, code, tables, rules, links,
// bold, italic, strikethrough and inline code. Links open only http(s) and mailto addresses.

import { copy, h, icon } from "./ui.js";

const SAFE_LINK = /^(https?:\/\/|mailto:)/i;

export function renderMarkdown(source) {
  const root = h("div", { class: "md" });
  blocks(source.replace(/\r\n?/g, "\n").split("\n"), root);
  return root;
}

function blocks(lines, parent) {
  let i = 0;
  const paragraph = [];
  const flush = () => {
    if (paragraph.length) {
      const p = h("p", {});
      paragraph.forEach((line, index) => {
        if (index) p.append(document.createElement("br"));
        inline(line.trim(), p);
      });
      parent.append(p);
      paragraph.length = 0;
    }
  };
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if ((m = /^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)[^\n]*$/.exec(line))) {
      flush();
      const fence = m[2];
      const body = [];
      i++;
      while (i < lines.length && !new RegExp(`^\\s*${fence[0]}{${fence.length},}\\s*$`).test(lines[i])) body.push(lines[i++]);
      i++; // the closing fence (a missing one ends at the end: an answer still being written)
      parent.append(codeBlock(body.join("\n"), m[3]));
    } else if (!line.trim()) {
      flush();
      i++;
    } else if ((m = /^(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line))) {
      flush();
      const level = Math.min(m[1].length + 1, 4); // an answer's # is a section, not the page's title
      const heading = h("h" + level, {});
      inline(m[2], heading);
      parent.append(heading);
      i++;
    } else if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      flush();
      parent.append(document.createElement("hr"));
      i++;
    } else if (/^\s*>/.test(line)) {
      flush();
      const quote = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) quote.push(lines[i++].replace(/^\s*>\s?/, ""));
      const box = h("blockquote", {});
      blocks(quote, box);
      parent.append(box);
    } else if (isTable(lines, i)) {
      flush();
      i = table(lines, i, parent);
    } else if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      flush();
      i = list(lines, i, parent);
    } else {
      paragraph.push(line);
      i++;
    }
  }
  flush();
}

function list(lines, start, parent) {
  const first = /^(\s*)([-*+]|\d+[.)])\s+/.exec(lines[start]);
  const ordered = /\d/.test(first[2]);
  const indent = first[1].length;
  const node = h(ordered ? "ol" : "ul", {});
  if (ordered && parseInt(first[2], 10) !== 1) node.start = parseInt(first[2], 10);
  let i = start;
  while (i < lines.length) {
    const item = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
    if (!item || item[1].length !== indent || /\d/.test(item[2]) !== ordered) break;
    const text = [item[3]];
    const nested = [];
    i++;
    while (i < lines.length && lines[i].trim()) {
      const next = /^(\s*)([-*+]|\d+[.)])\s+/.exec(lines[i]);
      if (next && next[1].length <= indent) break; // a sibling, or an item of the list around this one
      if (next || nested.length) nested.push(lines[i]); // a nested list, and what follows it
      else text.push(lines[i].trim()); // the item's text goes on
      i++;
    }
    const li = h("li", {});
    inline(text.join(" ").trim(), li);
    if (nested.length) {
      const margin = Math.min(...nested.filter((line) => line.trim()).map((line) => line.length - line.trimStart().length));
      blocks(nested.map((line) => line.slice(margin)), li);
    }
    node.append(li);
    while (i < lines.length && !lines[i].trim() && /^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i + 1] || "")) i++; // loose list
  }
  parent.append(node);
  return i;
}

const split = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());

function isTable(lines, i) {
  return i + 1 < lines.length && lines[i].includes("|") && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1]);
}

function table(lines, start, parent) {
  const head = split(lines[start]);
  const align = split(lines[start + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
  const node = h("table", {}, h("thead", {}, h("tr", {}, head.map((cell, n) => cellNode("th", cell, align[n])))));
  const body = h("tbody", {});
  let i = start + 2;
  while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
    body.append(h("tr", {}, split(lines[i]).map((cell, n) => cellNode("td", cell, align[n]))));
    i++;
  }
  node.append(body);
  parent.append(node);
  return i;
}

function cellNode(tag, text, align) {
  const cell = h(tag, {});
  if (align) cell.align = align;
  inline(text, cell);
  return cell;
}

function codeBlock(code, language) {
  const pre = h("pre", {}, h("code", {}, code));
  return h("div", { class: "codeblock" },
    h("div", { class: "lang" }, h("span", {}, language || "text"),
      h("button", { class: "ghost", onclick: () => copy(code), "aria-label": "Copy the code" }, icon("copy", { size: 14 }), "Copy")), pre);
}

// ---- inline ---------------------------------------------------------------------------------------------

const INLINE = new RegExp(
  [
    "(`+)([\\s\\S]*?[^`])\\1(?!`)", // 1,2 code
    "\\*\\*(?=\\S)([\\s\\S]*?\\S)\\*\\*", // 3 bold
    "__(?=\\S)([\\s\\S]*?\\S)__", // 4 bold
    "~~(?=\\S)([\\s\\S]*?\\S)~~", // 5 strike
    "\\*(?=[^\\s*])([\\s\\S]*?[^\\s*])\\*", // 6 italic
    "(?<![\\w])_(?=[^\\s_])([\\s\\S]*?[^\\s_])_(?![\\w])", // 7 italic
    "\\[([^\\]\\n]+)\\]\\(([^)\\s]+)(?:\\s+\"[^\"]*\")?\\)", // 8,9 link
    "<(https?:\\/\\/[^>\\s]+)>", // 10 <autolink>
    "(https?:\\/\\/[^\\s<>()\\[\\]]+[^\\s<>()\\[\\].,;:!?'\"])", // 11 bare url
  ].join("|"),
);

export function inline(text, parent) {
  let rest = text;
  while (rest) {
    const m = INLINE.exec(rest);
    if (!m) break;
    if (m.index) parent.append(rest.slice(0, m.index));
    if (m[2] !== undefined) parent.append(h("code", {}, m[2].trim()));
    else if (m[3] !== undefined || m[4] !== undefined) wrap("strong", m[3] ?? m[4], parent);
    else if (m[5] !== undefined) wrap("del", m[5], parent);
    else if (m[6] !== undefined || m[7] !== undefined) wrap("em", m[6] ?? m[7], parent);
    else if (m[8] !== undefined) {
      if (SAFE_LINK.test(m[9])) wrap("a", m[8], parent, { href: m[9], target: "_blank", rel: "noopener noreferrer nofollow" });
      else parent.append(m[0]);
    } else link(m[10] ?? m[11], parent);
    rest = rest.slice(m.index + m[0].length);
  }
  if (rest) parent.append(rest);
}

function wrap(tag, text, parent, props) {
  const node = h(tag, props);
  inline(text, node);
  parent.append(node);
}

function link(url, parent) {
  parent.append(h("a", { href: url, target: "_blank", rel: "noopener noreferrer nofollow" }, url));
}
