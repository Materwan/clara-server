// The player, as the server reports it: what it plays and how far, refreshed every few seconds. The bar at the foot of
// every page draws it, the pages hear about each change through `changes`, and the commands of the player go from here.
// Pictures are fetched with the session header and kept as data URLs, the only picture source the page's policy allows.

import { api } from "/api.js";
import { icon } from "/icons.js";
import { h, toast } from "/ui.js";
import { onChange as onBrowserChange, soundNeedsTap } from "./browser-player.js";

export const changes = new EventTarget(); // "change" when the player's answer moves on
export const player = { now: null, error: null, at: 0 }; // `at`: when `now` came, to move the position on from there

const REFRESH_MS = 4000;
const TICK_MS = 250;
const pictures = new Map(); // picture id -> a promise of the data URL, or of null when there is none
const REPEAT_NEXT = { off: "all", all: "one", one: "off" };
const REPEAT_ICON = { off: "repeat", all: "repeat", one: "repeat-one" };

export const failure = (error) => h("p", { class: "notice warn" }, error?.detail || String(error));

/** m:ss, or h:mm:ss for the longer things. */
export function clock(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const hours = Math.floor(total / 3600);
  const minutes = String(Math.floor((total % 3600) / 60)).padStart(hours ? 2 : 1, "0");
  const rest = String(total % 60).padStart(2, "0");
  return hours ? `${hours}:${minutes}:${rest}` : `${minutes}:${rest}`;
}

/** Where the player is now, in seconds: the server's position, moved on while it plays. */
export function position() {
  const now = player.now;
  if (!now) return 0;
  const moved = now.state === "playing" ? (performance.now() - player.at) / 1000 : 0;
  return Math.min(now.duration || Infinity, now.position + moved);
}

function fetchPicture(id) {
  return (async () => {
    try {
      const response = await fetch(`/v1/me/music/image?${new URLSearchParams({ id })}`, { headers: { "X-Clara-Web": "1" }, credentials: "same-origin" });
      if (!response.ok) return null;
      const blob = await response.blob();
      return await new Promise((resolve) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => resolve(null);
        reader.readAsDataURL(blob);
      });
    } catch {
      return null; // no picture: the placeholder stays
    }
  })();
}

/** A cover: the picture of `image` ({id}) once it is fetched, a disc until then or when there is none. */
export function cover(image, size = "md") {
  const box = h("span", { class: `cover ${size}`, "aria-hidden": "true" }, icon("disc", { size: size === "lg" ? 40 : 22 }));
  if (!image?.id) return box;
  if (!pictures.has(image.id)) pictures.set(image.id, fetchPicture(image.id));
  pictures.get(image.id).then((url) => {
    if (url) box.replaceChildren(h("img", { src: url, alt: "", decoding: "async" }));
  });
  return box;
}

/** Ask the server for the player's state now, and tell the pages. */
export async function refreshNow() {
  try {
    player.now = await api.get("/v1/me/music/now");
    player.error = null;
    player.at = performance.now();
  } catch (error) {
    player.now = null;
    player.error = error;
  }
  changes.dispatchEvent(new Event("change"));
  return player.now;
}

/** Keep the state fresh: every few seconds while the page is in view, and at once when it comes back. */
export function startPolling() {
  const tick = () => { if (document.visibilityState === "visible") refreshNow(); };
  setInterval(tick, REFRESH_MS);
  document.addEventListener("visibilitychange", tick);
  window.addEventListener("focus", tick);
  refreshNow();
}

/** Play `uri` now (replacing the queue), or put it next (`next`) or at the end of the queue (`end`). */
export async function playUri(uri, where = "replace") {
  try {
    const answer = where === "replace"
      ? await api.post("/v1/me/music/play", { uri })
      : await api.post("/v1/me/music/queue", { uri, position: where });
    toast(answer.message);
  } catch (error) {
    toast(error.detail || String(error), true);
    return;
  }
  refreshNow();
}

/** A transport or setting of the player (see the server's control route). False when it failed. */
export async function control(action, value = null) {
  try {
    await api.post("/v1/me/music/control", { action, value });
  } catch (error) {
    toast(error.detail || String(error), true);
    return false;
  }
  await refreshNow();
  return true;
}

/** The bar at the foot of the page: the track, its position, the transport, the volume and a way to the queue. */
export function mountBar(slot) {
  let dragging = false;
  let shownUri = null;
  const art = h("span", { class: "bar-art" });
  const title = h("strong", {}, "Nothing playing");
  const detail = h("span", { class: "muted small" }, "Pick something in Discover or Search");
  const scrub = h("input", { class: "scrub", type: "range", min: 0, max: 1, step: 1, value: 0, "aria-label": "Position" });
  const elapsed = h("span", { class: "bar-time elapsed" }, "0:00");
  const total = h("span", { class: "bar-time total" }, "0:00");
  const volume = h("input", { class: "volume", type: "range", min: 0, max: 100, step: 1, value: 0, "aria-label": "Volume" });
  const shuffle = h("button", { class: "ghost icon-btn shuffle", type: "button", "aria-label": "Shuffle", title: "Shuffle", onclick: () => control("shuffle", !player.now?.shuffle) }, icon("shuffle", { size: 19 }));
  const prev = h("button", { class: "ghost icon-btn", type: "button", "aria-label": "Previous", title: "Previous", onclick: () => control("previous") }, icon("prev", { size: 20 }));
  const playPause = h("button", { class: "primary play icon-btn", type: "button", onclick: () => control(player.now?.state === "playing" ? "pause" : "play") });
  const next = h("button", { class: "ghost icon-btn", type: "button", "aria-label": "Next", title: "Next", onclick: () => control("next") }, icon("next", { size: 20 }));
  const repeat = h("button", { class: "ghost icon-btn repeat", type: "button", onclick: () => control("repeat", REPEAT_NEXT[player.now?.repeat || "off"]) });
  const queue = h("a", { class: "bar-queue", href: "#/queue", "aria-label": "Queue", title: "Queue" }, icon("queue", { size: 19 }));

  scrub.addEventListener("pointerdown", () => { dragging = true; });
  scrub.addEventListener("input", () => { elapsed.textContent = clock(Number(scrub.value)); });
  scrub.addEventListener("change", () => {
    dragging = false;
    control("seek", Math.round(Number(scrub.value)));
  });
  let volumeTimer = null;
  let volumeDragging = false; // the slider is in hand: the refreshes do not move it
  const fillVolume = () => volume.style.setProperty("--fill", `${volume.value}%`);
  volume.addEventListener("pointerdown", () => { volumeDragging = true; });
  volume.addEventListener("change", () => { volumeDragging = false; });
  volume.addEventListener("input", () => {
    fillVolume();
    clearTimeout(volumeTimer);
    volumeTimer = setTimeout(() => control("volume", Number(volume.value)), 200);
  });

  const root = h("section", { class: "bar", "aria-label": "Player" },
    h("div", { class: "bar-main" },
      h("div", { class: "bar-now" }, art, h("div", { class: "text" }, title, h("div", {}, detail))),
      h("div", { class: "bar-center" },
        h("div", { class: "bar-ctl" }, shuffle, prev, playPause, next, repeat),
        h("div", { class: "bar-seek" }, elapsed, scrub, total)),
      h("div", { class: "bar-side" }, volume, queue)));
  slot.append(root);

  function draw() {
    const now = player.now;
    const track = now?.track;
    if (track?.uri !== shownUri) {
      shownUri = track?.uri ?? null;
      art.replaceChildren(cover(track?.image, "md"));
    }
    if (player.error) {
      title.textContent = "Music is not available";
      detail.replaceChildren(player.error.status === 409 ? h("a", { href: "#/setup" }, "Choose your player") : player.error.detail || String(player.error));
    } else if (!now) {
      title.textContent = "Nothing playing";
      detail.textContent = "Loading…";
    } else if (track) {
      title.textContent = track.title;
      detail.textContent = soundNeedsTap() ? "Tap the page to hear it here" : [track.subtitle, now.name].filter(Boolean).join(" · ");
    } else {
      title.textContent = "Nothing playing";
      detail.textContent = "Pick something in Discover or Search";
    }
    const playing = now?.state === "playing";
    const ready = Boolean(track);
    playPause.replaceChildren(icon(playing ? "pause" : "play", { size: 20 }));
    playPause.setAttribute("aria-label", playing ? "Pause" : "Play");
    playPause.title = playing ? "Pause" : "Play";
    playPause.disabled = !ready && !player.error;
    for (const button of [prev, next]) button.disabled = !ready;
    shuffle.disabled = !ready;
    shuffle.setAttribute("aria-pressed", String(Boolean(now?.shuffle)));
    shuffle.classList.toggle("on", Boolean(now?.shuffle));
    repeat.replaceChildren(icon(REPEAT_ICON[now?.repeat || "off"], { size: 19 }));
    repeat.setAttribute("aria-label", `Repeat: ${now?.repeat || "off"}`);
    repeat.title = repeat.getAttribute("aria-label");
    repeat.setAttribute("aria-pressed", String((now?.repeat || "off") !== "off"));
    repeat.classList.toggle("on", (now?.repeat || "off") !== "off");
    repeat.disabled = !ready;
    if (!dragging && !volumeDragging) {
      volume.value = now?.volume ?? 0;
      fillVolume();
      scrub.max = Math.max(1, now?.duration || 1);
    }
    tick();
  }

  function tick() {
    const duration = player.now?.duration || 0;
    if (!dragging) {
      const at = position();
      scrub.value = Math.floor(at);
      scrub.style.setProperty("--fill", duration ? `${(at / duration) * 100}%` : "0%");
      elapsed.textContent = clock(at);
      total.textContent = clock(duration);
    }
  }

  changes.addEventListener("change", draw);
  const stopBrowserWatch = onBrowserChange(draw);
  const timer = setInterval(tick, TICK_MS);
  draw();
  return {
    destroy() {
      clearInterval(timer);
      changes.removeEventListener("change", draw);
      stopBrowserWatch();
      root.remove();
    },
  };
}

