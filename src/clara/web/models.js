// The models people may choose (the administrators select them, see admin.js) and the picker used by the account
// page and the chat. A model costs its weight in credits for every token, whatever it reads or writes.

import { api } from "./api.js";
import { h } from "./ui.js";

/** What the surfaces are called (the others are shown by their own name). */
export const SURFACE_NAMES = { web: "Web site", app: "Desktop app", cli: "Terminal (clara-chat)", console: "Console" };

export const weightText = (weight) => String(Number(Number(weight).toFixed(3)));

/** What a token of a model costs, in words: "0.4 credit per token". */
export const costText = (weight) => Number(weight) === 0 ? "your own API key, no credits" : `${weightText(weight)} ${Number(weight) === 1 ? "credit" : "credits"} per token`;

/** The models of the providers this person brought an API key for (they cost no credits), and the model `ref` among every one. */
export const personalModels = (info) => info.personal || [];
export const findModel = (info, ref) => [...info.models, ...personalModels(info)].find((model) => model.ref === ref);
export const anyModel = (info) => info.models.length + personalModels(info).length > 0;

/** `{models, default, choices, current, surfaces}`: what this person may choose, and what they chose. */
export const loadModels = (who) => api.get("/v1/models", who);

/** A model's name, the way a list shows it. */
export const modelLabel = (model) => `${model.name} (${model.provider_label})`;

/** The abilities the server names a model by, and the word that says each one. */
const ABILITIES = [["thinking", "Thinking"], ["tools", "Tools"], ["vision", "Image input"]];

/** A number of tokens in words: "262K", "1M". */
export const contextText = (tokens) => (tokens >= 1_000_000 ? `${Number((tokens / 1_000_000).toFixed(1))}M` : `${Math.round(tokens / 1000)}K`);

/** What a model is known to do, in words: ["Thinking", "Tools", "262K context"]. What its provider does not say is left out; a model that can do none of the three says "Text only". */
export function abilityWords(model) {
  const caps = model?.capabilities || {};
  const words = ABILITIES.filter(([key]) => caps[key] === true).map(([, word]) => word);
  if (caps.context) words.push(`${contextText(caps.context)} context`);
  if (ABILITIES.every(([key]) => caps[key] === false)) words.unshift("Text only");
  return words;
}

/** The abilities of a model as badges, or a note that its provider does not say what it can do. */
export function abilityBadges(model) {
  const words = abilityWords(model);
  if (!words.length) return h("span", { class: "muted small" }, "Not reported");
  return h("span", { class: "row wrap" }, words.map((word) => h("span", { class: "badge" }, word)));
}

/** What a model can do, after its name in an option: " · Thinking, Tools" (nothing when it is not known). */
const abilitySuffix = (model) => {
  const words = abilityWords(model);
  return words.length ? ` · ${words.join(", ")}` : "";
};

/** Choose `ref` (null: the server's own) for `surface`; the server's answer holds the choices and the model now in use. */
export const chooseModel = (who, surface, ref) => api.put("/v1/models/choice", { ...who, model: ref, for_surface: surface });

/**
 * A select with the server's own model and every model the person may choose. `choice` is the model chosen (null:
 * the server's), `onChange(ref | null)` is called when they pick another.
 */
export function modelSelect(info, choice, onChange, label = "Model") {
  const known = Boolean(findModel(info, choice));
  const personal = personalModels(info);
  const select = h("select", { class: "model-select", "aria-label": label },
    h("option", { value: "" }, `Server default: ${info.default.name} (${costText(info.default.weight)})${abilitySuffix(info.default)}`),
    info.models.map((model) => h("option", { value: model.ref }, `${modelLabel(model)}, ${costText(model.weight)}${abilitySuffix(model)}`)),
    personal.length ? h("optgroup", { label: "With your own API key (no credits)" },
      personal.map((model) => h("option", { value: model.ref }, `${modelLabel(model)}${abilitySuffix(model)}`))) : null);
  select.value = known ? choice : "";
  select.addEventListener("change", () => onChange(select.value || null));
  return select;
}
