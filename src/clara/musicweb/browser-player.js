// This browser as a player of Music Assistant. Music Assistant streams the music to a Sendspin player, and this page is
// one: the sound comes out of the device the page is open on. The page never sees the Music Assistant token: it pairs
// the player through the server (the pairing token is the player's own identity, as Music Assistant's web page does),
// and connects through the server's relay, which holds the token and forwards the encrypted Sendspin connection.

import { api } from "/api.js";
import { SendspinPlayer, loadSendspinClientIdentity } from "./vendor/sendspin.js";

const WANTED = "clara.music.browser"; // "on" while the person wants this browser to play
const changes = new EventTarget();

/** What the page shows: status is off, connecting, connected or error; `detail` says more, and `pairing` is the last
 * word of Music Assistant's pairing (it can come while the player is already connected). */
export const browser = { status: "off", detail: "", pairing: "", connected: false, name: "", clientId: null };

let player = null;
let watch = null;
let soundReady = false; // the page has been let to play sound, by a tap (browsers allow no sound before one)

function storage() {
  try {
    return localStorage;
  } catch {
    return null; // storage blocked: the player works for this visit only
  }
}

function read(key) {
  try {
    return storage()?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function write(key, value) {
  try {
    if (value === null) storage()?.removeItem(key);
    else storage()?.setItem(key, value);
  } catch {
    /* nothing to remember */
  }
}

function set(status, detail = "") {
  browser.status = status;
  browser.detail = detail;
  changes.dispatchEvent(new Event("change"));
}

function pairingSaid(text) {
  browser.pairing = text;
  changes.dispatchEvent(new Event("change"));
}

/** Which browser this is, as Music Assistant lists it: "Clara in Chrome on computer". */
function nameOfBrowser() {
  const agent = navigator.userAgent;
  const app = /Edg\//.test(agent) ? "Edge" : /Firefox\//.test(agent) ? "Firefox" : /Chrome\//.test(agent) ? "Chrome" : /Safari\//.test(agent) ? "Safari" : "browser";
  const device = /iPhone|iPad|iPod/.test(agent) ? "iOS" : /Android/.test(agent) ? "Android" : "computer";
  return `Clara in ${app} on ${device}`;
}

function pairingText(event, detail) {
  if (event === "pending") return "Music Assistant is waiting to pair this browser.";
  if (event === "started") return "Pairing with Music Assistant…";
  if (event === "finalized") return "Paired with Music Assistant.";
  return `Pairing stopped${detail ? `: ${detail}` : "."}`;
}

/** Whether this browser should play (it was turned on, and stays on until it is turned off). */
export function wanted() {
  return read(WANTED) === "on";
}

/** The Sendspin player of this page, made with the browser's own identity and its relay address. */
function create(identity) {
  if (player) player.disconnect("user_request");
  browser.clientId = identity.clientId;
  browser.name = nameOfBrowser();
  player = new SendspinPlayer({
    baseUrl: `${location.origin}/v1/me/music/relay/${identity.clientId}`,
    clientName: browser.name,
    // Music Assistant takes a player for its built-in web player only when it says it is one (as its own page does)
    productName: "Web Player",
    storage: storage(),
    onPairing: (event, detail) => pairingSaid(pairingText(event, detail)),
    // As Music Assistant's own page does: it pairs the player each time it connects, and again after a reconnection
    reconnect: { onReconnected: () => pairHere(identity).catch(() => {}) },
  });
  return player;
}

/** Keep the status current: the connection comes and goes (the player reconnects by itself). */
function watchConnection() {
  clearInterval(watch);
  watch = setInterval(() => {
    const up = Boolean(player?.isConnected);
    if (up === browser.connected) return;
    browser.connected = up;
    if (up) {
      browser.pairing = "";
      set("connected", "Connected. Music Assistant can play here.");
    } else {
      set("connecting", "Connection lost: reconnecting…");
    }
  }, 1000);
}

/**
 * Turn this browser on as a player. From a tap (`withSound`), the sound is unlocked at once, before anything waits:
 * browsers allow sound only in direct answer to a tap.
 */
export async function turnOn({ withSound = false } = {}) {
  const identity = loadSendspinClientIdentity(storage());
  const created = create(identity);
  // Not awaited before connecting: the sound waits for the tap, the connection does not
  const unlocked = withSound ? unlockFrom(created) : Promise.resolve();
  set("connecting", "Connecting…");
  try {
    await created.connect();
  } catch (error) {
    set("error", error.message || String(error));
    throw error;
  }
  write(WANTED, "on");
  browser.connected = created.isConnected;
  browser.pairing = "";
  set(created.isConnected ? "connected" : "connecting", created.isConnected ? "Connected. Music Assistant can play here." : "Connecting…");
  watchConnection();
  await unlocked;
  // Music Assistant can only pair a player that is connected to it, so this comes after the connection
  await pairHere(identity);
}

/** Ask Music Assistant to pair this player: it knows the player by its connection, so this is sent once connected. */
async function pairHere(identity) {
  pairingSaid("Pairing with Music Assistant…");
  try {
    await api.post("/v1/me/music/sendspin/pair", { pairing_token: identity.pairingToken });
  } catch (error) {
    set("error", `Music Assistant did not pair this browser: ${error.detail || error.message || error}`);
    throw error;
  }
}

/** Turn this browser off: it stops being a player until it is turned on again. */
export function turnOff() {
  write(WANTED, "off");
  soundReady = false;
  clearInterval(watch);
  player?.disconnect("user_request");
  player = null;
  browser.connected = false;
  set("off");
}

async function unlockFrom(current) {
  try {
    await current.unlock();
    soundReady = true;
  } catch {
    /* the next tap tries again */
  }
  changes.dispatchEvent(new Event("change"));
}

/** Let the sound through: called from a tap, as browsers require. */
export async function unlockSound() {
  if (player) await unlockFrom(player);
}

/** What the browser player is doing, for the page to show: whether it is connected and synced, playing, muted, its
 * volume and its stream. Null when it is not a player. */
export function diagnostics() {
  if (!player) return null;
  const sync = player.timeSyncInfo;
  return {
    connected: player.isConnected,
    synced: Boolean(sync?.synced),
    playing: player.isPlaying,
    muted: player.muted,
    volume: player.volume,
    format: player.currentFormat ? `${player.currentFormat.codec} ${player.currentFormat.sample_rate} Hz` : null,
    soundReady,
  };
}

/** Whether the page still needs a tap before this browser makes sound. */
export function soundNeedsTap() {
  return Boolean(player) && !soundReady;
}

/** The first tap or key press on the page lets the sound through, when this browser is a player. */
function firstTap() {
  if (player && !soundReady) unlockSound();
}
document.addEventListener("pointerdown", firstTap, true);
document.addEventListener("keydown", firstTap, true);

/** Turn it on again after a reload, without a tap (the sound waits for one). */
export async function resume() {
  if (!wanted() || player) return;
  try {
    await turnOn({ withSound: false });
  } catch {
    /* the status says why */
  }
}

/** The listener for changes of the status (a page redraws itself). */
export function onChange(listener) {
  changes.addEventListener("change", listener);
  return () => changes.removeEventListener("change", listener);
}
