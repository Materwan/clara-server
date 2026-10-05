// The server's HTTP API. The session lives in an HttpOnly cookie: scripts never see the token. Every request
// carries `X-Clara-Web`, the header that makes the server accept that cookie (a page of another site cannot add it).

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

const HEADERS = { "X-Clara-Web": "1" };
export const events = new EventTarget(); // "signed-out" when the server no longer knows this session

async function failure(response) {
  let detail = response.statusText || `HTTP ${response.status}`;
  try {
    const body = await response.json();
    detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
  } catch { /* not JSON */ }
  return new ApiError(response.status, detail);
}

export async function request(method, path, { body, query, raw, signal, quiet401 } = {}) {
  const url = new URL(path, location.origin);
  for (const [key, value] of Object.entries(query || {})) if (value !== undefined && value !== "") url.searchParams.set(key, value);
  const headers = { ...HEADERS };
  let payload;
  if (raw !== undefined) payload = raw;
  else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, { method, headers, body: payload, signal, credentials: "same-origin" });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "Cannot reach the Clara server.");
  }
  if (!response.ok) {
    const error = await failure(response);
    if (response.status === 401 && !quiet401) events.dispatchEvent(new Event("signed-out"));
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

export const api = {
  get: (path, query, options) => request("GET", path, { query, ...options }),
  post: (path, body, options) => request("POST", path, { body, ...options }),
  patch: (path, body) => request("PATCH", path, { body }),
  put: (path, body) => request("PUT", path, { body }),
  delete: (path, query) => request("DELETE", path, { query }),
};

export const conversationPath = (id) => "/v1/conversations/" + id.split("/").map(encodeURIComponent).join("/");

/** The events of one turn: `{type, ...}` objects read from the server-sent-events stream. */
export async function* streamChat(body, signal) {
  let response;
  try {
    response = await fetch("/v1/chat/stream", {
      method: "POST",
      headers: { ...HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal,
      credentials: "same-origin",
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "Cannot reach the Clara server.");
  }
  if (!response.ok) {
    const error = await failure(response);
    if (response.status === 401) events.dispatchEvent(new Event("signed-out"));
    throw error;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let end;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const block = buffer.slice(0, end);
      buffer = buffer.slice(end + 2);
      const data = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart());
      if (!data.length) continue; // a ": keepalive" comment
      try {
        yield JSON.parse(data.join("\n"));
      } catch { /* a broken event is skipped */ }
    }
  }
}
