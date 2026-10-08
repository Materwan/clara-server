// Line icons drawn on a 24-unit grid, and Clara's mark. Built with the DOM like everything else on the page.

const NS = "http://www.w3.org/2000/svg";

// Each icon is a list of strokes; "fill:" marks a shape that is filled instead of stroked.
const ICONS = {
  plus: ["M12 5v14", "M5 12h14"],
  chat: ["M20 11.5a7.5 7.5 0 0 1-11.1 6.6L4 19.5l1.4-4.5A7.5 7.5 0 1 1 20 11.5Z"],
  memory: ["M6.5 3.5h10a2 2 0 0 1 2 2v13a2 2 0 0 1-2 2h-10Z", "M6.5 3.5v17", "M10 8.5h5", "M10 12h5"],
  user: ["M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M4.5 20.5a7.5 7.5 0 0 1 15 0"],
  admin: ["M4 7h9", "M17 7h3", "M4 17h3", "M11 17h9", "M15 4.5v5", "M9 14.5v5"],
  search: ["M10.5 17.5a7 7 0 1 0 0-14 7 7 0 0 0 0 14Z", "M20 20l-4.5-4.5"],
  more: ["fill:M5 13.6a1.6 1.6 0 1 0 0-3.2 1.6 1.6 0 0 0 0 3.2Z", "fill:M12 13.6a1.6 1.6 0 1 0 0-3.2 1.6 1.6 0 0 0 0 3.2Z", "fill:M19 13.6a1.6 1.6 0 1 0 0-3.2 1.6 1.6 0 0 0 0 3.2Z"],
  pin: ["M9 3.5h6", "M10 3.5v6l-3 4h10l-3-4v-6", "M12 13.5v7"],
  clip: ["M20 11.3l-7.9 7.9a5 5 0 0 1-7.1-7.1l8.5-8.5a3.3 3.3 0 0 1 4.7 4.7l-8.5 8.5a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9"],
  send: ["M12 19V5.5", "M6 11l6-6 6 6"],
  stop: ["fill:M7.5 6.5h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1v-9a1 1 0 0 1 1-1Z"],
  grip: ["fill:M9 6.6a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8Z", "fill:M15 6.6a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8Z", "fill:M9 13.4a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8Z", "fill:M15 13.4a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8Z", "fill:M9 20.2a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8Z", "fill:M15 20.2a1.4 1.4 0 1 0 0-2.8 1.4 1.4 0 0 0 0 2.8Z"],
  menu: ["M4 7h16", "M4 12h16", "M4 17h11"],
  close: ["M6 6l12 12", "M18 6L6 18"],
  copy: ["M9.5 9h9a1 1 0 0 1 1 1v9.5a1 1 0 0 1-1 1h-9a1 1 0 0 1-1-1V10a1 1 0 0 1 1-1Z", "M15.5 9V5.5a1 1 0 0 0-1-1h-9a1 1 0 0 0-1 1V15a1 1 0 0 0 1 1h3"],
  check: ["M5 12.5l4.5 4.5L19 7"],
  trash: ["M4 7h16", "M9.5 7V4.5h5V7", "M6 7l1 13h10l1-13", "M10 11v5.5", "M14 11v5.5"],
  sun: ["M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M12 2.5v2", "M12 19.5v2", "M4.6 4.6L6 6", "M18 18l1.4 1.4", "M2.5 12h2", "M19.5 12h2", "M4.6 19.4L6 18", "M18 6l1.4-1.4"],
  moon: ["M19.5 14.5A7.5 7.5 0 0 1 9.5 4.5a7.5 7.5 0 1 0 10 10Z"],
  auto: ["M12 20a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z", "fill:M12 4a8 8 0 0 1 0 16Z"],
  logout: ["M10 4H6.5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2H10", "M15 16l4-4-4-4", "M19 12H9"],
  edit: ["M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16Z", "M13.5 6.5l4 4"],
  terminal: ["M4.5 5h15a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1h-15a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z", "M7.5 9.5l3 2.5-3 2.5", "M12.5 15h4"],
  users: ["M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7Z", "M2.5 20a6.5 6.5 0 0 1 13 0", "M16 4.3a3.5 3.5 0 0 1 0 6.4", "M18 14a6.5 6.5 0 0 1 3.5 6"],
  bot: ["M6 9h12a2 2 0 0 1 2 2v6.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V11a2 2 0 0 1 2-2Z", "M12 9V5.5", "M12 4h.01", "M9 14h.01", "M15 14h.01", "M2 13.5v2", "M22 13.5v2"],
  server: ["M5 4h14a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z", "M5 13h14a1 1 0 0 1 1 1v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-5a1 1 0 0 1 1-1Z", "M8 7.5h.01", "M8 16.5h.01"],
  link: ["M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1", "M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1"],
  key: ["M15 14a5 5 0 1 0-4.6-3L3.5 18v2.5H6v-2h2v-2h2l1.4-1.4A5 5 0 0 0 15 14Z", "M16.5 7.5h.01"],
  laptop: ["M5.5 5.5h13a1 1 0 0 1 1 1V15h-15V6.5a1 1 0 0 1 1-1Z", "M2.5 18.5h19"],
  phone: ["M8.5 2.5h7a1.5 1.5 0 0 1 1.5 1.5v16a1.5 1.5 0 0 1-1.5 1.5h-7A1.5 1.5 0 0 1 7 20V4a1.5 1.5 0 0 1 1.5-1.5Z", "M11 18h2"],
  globe: ["M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z", "M3 12h18", "M12 3c2.4 2.5 3.5 5.5 3.5 9s-1.1 6.5-3.5 9c-2.4-2.5-3.5-5.5-3.5-9S9.6 5.5 12 3Z"],
  file: ["M14 3H7a1.5 1.5 0 0 0-1.5 1.5v15A1.5 1.5 0 0 0 7 21h10a1.5 1.5 0 0 0 1.5-1.5V7.5Z", "M14 3v4.5h4.5"],
  power: ["M12 3.5v8", "M7 6.3a7.5 7.5 0 1 0 10 0"],
  compress: ["M4 9h5V4", "M20 15h-5v5", "M9 9L4 4", "M15 15l5 5"],
  bolt: ["M13 3L5 13.5h6L10 21l8-10.5h-6Z"],
  folder: ["M3.5 7A1.5 1.5 0 0 1 5 5.5h4.2l2 2.2H19a1.5 1.5 0 0 1 1.5 1.5v8.3A1.5 1.5 0 0 1 19 19H5a1.5 1.5 0 0 1-1.5-1.5Z"],
  branch: ["M6 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z", "M6 20a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z", "M18 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z", "M6 8v8", "M18 8c0 5-6 4-11 8.3"],
  refresh: ["M19.5 10.5A7.5 7.5 0 0 0 6 6.6L4.5 8", "M4.5 4v4h4", "M4.5 13.5A7.5 7.5 0 0 0 18 17.4l1.5-1.4", "M19.5 20v-4h-4"],
  download: ["M12 4.5V15", "M7.5 10.5L12 15l4.5-4.5", "M4.5 15v3.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V15"],
  upload: ["M12 15V4.5", "M7.5 9L12 4.5 16.5 9", "M4.5 15v3.5A1.5 1.5 0 0 0 6 20h12a1.5 1.5 0 0 0 1.5-1.5V15"],
  back: ["M19 12H5.5", "M11 6l-6 6 6 6"],
  tasks: ["M10 6.5h9.5", "M10 12h9.5", "M10 17.5h9.5", "M4.5 6.5l1.2 1.2 2.2-2.4", "M4.5 12l1.2 1.2 2.2-2.4", "M4.5 17.5l1.2 1.2 2.2-2.4"],
  calendar: ["M5.5 5.5h13a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1h-13a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1Z", "M4.5 10h15", "M8.5 3.5v3", "M15.5 3.5v3"],
  clock: ["M12 20.5a8.5 8.5 0 1 0 0-17 8.5 8.5 0 0 0 0 17Z", "M12 7.5V12l3 2"],
  play: ["fill:M8.5 5.8v12.4a.8.8 0 0 0 1.2.7l10-6.2a.8.8 0 0 0 0-1.4l-10-6.2a.8.8 0 0 0-1.2.7Z"],
  pause: ["M9 5.5v13", "M15 5.5v13"],
  bell: ["M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 1.5h-15Z", "M10 20.5a2 2 0 0 0 4 0"],
  chevron: ["M9 6l6 6-6 6"],
  plug: ["M9 3v5", "M15 3v5", "M6.5 8h11v2.5a5.5 5.5 0 0 1-11 0Z", "M12 16v5"],
  cloud: ["M7.5 18.5a4.5 4.5 0 0 1-.6-8.96A5.8 5.8 0 0 1 18 9.8a4.35 4.35 0 0 1-.5 8.7Z"],
  shield: ["M12 3.5l7 2.8v5.2c0 4.3-2.9 7.7-7 9.5-4.1-1.8-7-5.2-7-9.5V6.3Z", "M9 12l2.2 2.2L15.5 10"],
};

/** An icon as an <svg> element; `label` makes it announced, otherwise it is decorative. */
export function icon(name, { size = 20, label = "", className = "" } = {}) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("class", ("icon " + className).trim());
  if (label) {
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
  } else svg.setAttribute("aria-hidden", "true");
  for (const stroke of ICONS[name] || []) {
    const path = document.createElementNS(NS, "path");
    const filled = stroke.startsWith("fill:");
    path.setAttribute("d", filled ? stroke.slice(5) : stroke);
    if (filled) path.setAttribute("class", "solid");
    svg.append(path);
  }
  return svg;
}

/** Clara's mark: her portrait (`clara.png`, a round picture with a violet ring), `size` pixels wide. */
export function mark(size = 28, className = "") {
  const picture = document.createElement("img");
  picture.src = "/clara.png";
  picture.alt = "";
  picture.width = size;
  picture.height = size;
  picture.decoding = "async";
  picture.className = ("mark " + className).trim();
  picture.setAttribute("aria-hidden", "true");
  return picture;
}

/** A ring that fills up to `percent` (the context meter). */
export function ring(size = 22) {
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 36 36");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("class", "ring");
  svg.setAttribute("aria-hidden", "true");
  const track = document.createElementNS(NS, "circle");
  const fill = document.createElementNS(NS, "circle");
  for (const [node, cls] of [[track, "track"], [fill, "fill"]]) {
    node.setAttribute("cx", 18);
    node.setAttribute("cy", 18);
    node.setAttribute("r", 15);
    node.setAttribute("class", cls);
    node.setAttribute("pathLength", 100);
  }
  fill.setAttribute("stroke-dasharray", "0 100");
  svg.append(track, fill);
  svg.set = (percent) => fill.setAttribute("stroke-dasharray", `${Math.max(0, Math.min(100, percent))} 100`);
  return svg;
}
