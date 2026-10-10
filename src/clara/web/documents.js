// Attached documents: read in the browser (text and code) or on the server (PDF, Word), then put in the message the
// way the desktop app does, so a conversation reads the same on every surface.

import { request } from "./api.js";

export const MAX_TOTAL_CHARS = 150_000; // all the documents of one message
const MAX_TEXT_BYTES = 2_000_000;
const MAX_IMAGE_BYTES = 8_000_000; // a picture (the server refuses a bigger one too)
const MAX_ANIMATION_BYTES = 30_000_000; // a GIF or WebP: the server reads some of its frames, not all of them
const IMAGE_TYPES = { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp" };
const DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
// Read by the server: the route, the most bytes it takes (webapi.MAX_PDF_BYTES, MAX_DOCX_BYTES) and the note on the chip
const SERVER_READERS = {
  pdf: { path: "/v1/documents/extract", maxBytes: 30_000_000, note: (body) => `${body.pages} page${body.pages === 1 ? "" : "s"}` },
  docx: { path: "/v1/documents/docx", maxBytes: 30_000_000, note: () => "Word document" },
};

const LANGUAGES = {
  py: "python", pyw: "python", pyi: "python", c: "c", h: "c", cpp: "cpp", cc: "cpp", cxx: "cpp", hpp: "cpp",
  md: "markdown", markdown: "markdown", txt: "", log: "", rst: "rst", tex: "latex", json: "json", yaml: "yaml",
  yml: "yaml", toml: "toml", ini: "ini", cfg: "ini", csv: "csv", xml: "xml", html: "html", htm: "html",
  css: "css", sql: "sql", js: "javascript", mjs: "javascript", ts: "typescript", tsx: "tsx", jsx: "jsx",
  java: "java", kt: "kotlin", go: "go", rs: "rust", rb: "ruby", php: "php", sh: "bash", bash: "bash",
  ps1: "powershell", bat: "batch", cs: "csharp", swift: "swift", lua: "lua", r: "r", scala: "scala", vue: "vue",
};

export class DocumentError extends Error {}

function extension(name) {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot + 1).toLowerCase();
}

/** `{name, kind, text, size}` for a File, or throws DocumentError. */
export async function readDocument(file) {
  const ext = extension(file.name);
  const serverKind = ext === "pdf" || file.type === "application/pdf" ? "pdf" : ext === "docx" || file.type === DOCX_TYPE ? "docx" : "";
  if (serverKind) {
    const reader = SERVER_READERS[serverKind];
    if (file.size > reader.maxBytes) throw new DocumentError(`${file.name} is too big (at most ${reader.maxBytes / 1e6} MB).`);
    try {
      const body = await request("POST", reader.path, { raw: await file.arrayBuffer() });
      return { name: file.name, kind: serverKind, text: body.text, note: reader.note(body) };
    } catch (error) {
      throw new DocumentError(`${file.name}: ${error.detail || error.message}`);
    }
  }
  if (file.size > MAX_TEXT_BYTES) throw new DocumentError(`${file.name} is too big to be read whole.`);
  const bytes = new Uint8Array(await file.arrayBuffer());
  if (bytes.subarray(0, 8192).includes(0)) throw new DocumentError(`${file.name} is not a text file.`);
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    text = new TextDecoder("windows-1252").decode(bytes);
  }
  if (!text.trim()) throw new DocumentError(`${file.name} is empty.`);
  const lines = text.split("\n").length;
  return { name: file.name, kind: LANGUAGES[ext] ?? "", text: text.replace(/^﻿/, ""), note: `${lines} line${lines === 1 ? "" : "s"}` };
}

/** A file of the composer: a picture (`{name, kind: "image", image: {mime, data}, note}`, the data in base64) or a document (`readDocument`). */
export async function readAttachment(file) {
  const type = file.type || IMAGE_TYPES[extension(file.name)];
  if (!Object.values(IMAGE_TYPES).includes(type)) return readDocument(file);
  const limit = type === "image/gif" || type === "image/webp" ? MAX_ANIMATION_BYTES : MAX_IMAGE_BYTES;
  if (file.size > limit) throw new DocumentError(`${file.name} is too big (at most ${limit / 1e6} MB for a picture).`);
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let start = 0; start < bytes.length; start += 0x8000) binary += String.fromCharCode(...bytes.subarray(start, start + 0x8000));
  return { name: file.name, kind: "image", image: { mime: type, data: btoa(binary) }, note: `${Math.max(1, Math.round(file.size / 1000))} KB` };
}

/** The pictures among the composer's files, as the chat request takes them (the server reads them with the message). */
export const imagesOf = (docs) => docs.filter((doc) => doc.image).map((doc) => ({ name: doc.name, mime: doc.image.mime, data: doc.image.data }));

/** The note that names the pictures in a message, as the server writes it into the message it stores: "(Attached: a.png)". */
export function imageNote(docs) {
  const names = docs.filter((doc) => doc.image).map((doc) => doc.name);
  return names.length ? `(Attached: ${names.join(", ")})` : "";
}

export function forModel(doc) {
  if (doc.image) return ""; // a picture goes to the model as a picture, not as text
  const kind = doc.kind || "text";
  let body;
  if (doc.kind === "pdf" || doc.kind === "docx") body = doc.text;
  else {
    const longest = Math.max(0, ...(doc.text.match(/`+/g) || []).map((run) => run.length));
    const fence = "`".repeat(Math.max(3, longest + 1)); // longer than any run inside: it cannot be closed early
    body = `${fence}${doc.kind}\n${doc.text.trimEnd()}\n${fence}`;
  }
  return `<document name="${doc.name.replace(/["\n]/g, "'")}" type="${kind}">\n${body}\n</document>`;
}

export const totalChars = (docs) => docs.reduce((sum, doc) => sum + forModel(doc).length, 0);

/** The message sent to Clara: what the user wrote, then the documents. */
export function compose(message, docs) {
  const documents = docs.filter((doc) => !doc.image); // pictures are named by the server, not written here
  const parts = message.trim() ? [message.trim()] : [];
  if (documents.length) {
    const names = documents.map((doc) => doc.name).join(", ");
    parts.push(parts.length ? `(Attached: ${names})` : `Here ${documents.length === 1 ? "is" : "are"}: ${names}.`);
    parts.push(...documents.map(forModel));
  }
  return parts.join("\n\n");
}

/** A message as `compose` made it, taken apart: `{text, names}`. A message without documents comes back whole. */
export function splitMessage(content) {
  const start = content.search(/(?:^|\n\n)<document name="/);
  if (start < 0) return { text: content, names: [] };
  const head = content.slice(0, start);
  const names = [];
  const opening = /<document name="([^"\n]*)" type="[^"\n]*">\n/g;
  let match;
  opening.lastIndex = start;
  for (;;) {
    const here = content.indexOf("<document name=", opening.lastIndex);
    if (here < 0) break;
    opening.lastIndex = here;
    match = opening.exec(content);
    if (!match || match.index !== here) break;
    names.push(match[1]);
    let end = content.indexOf("\n</document>", opening.lastIndex);
    while (end !== -1) {
      const after = end + "\n</document>".length;
      if (after === content.length || content.startsWith('\n\n<document name="', after)) break;
      end = content.indexOf("\n</document>", end + 1);
    }
    if (end === -1) break;
    opening.lastIndex = end + "\n</document>".length + 2;
  }
  if (!names.length) return { text: content, names: [] };
  const text = head.replace(/(?:^|\n\n)(?:\(Attached: [^\n]*\)|Here (?:is|are): [^\n]*\.)$/, "").trim();
  return { text, names };
}
