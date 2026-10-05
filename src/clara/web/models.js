// The models people may choose (the administrators select them, see admin.js) and the picker used by the account
// page and the chat. A model costs its weight in credits for every token, whatever it reads or writes.

import { api } from "./api.js";
import { h } from "./ui.js";

/** What the surfaces are called (the others are shown by their own name). */
export const SURFACE_NAMES = { web: "Web site", app: "Desktop app", cli: "Terminal (clara-chat)", console: "Console" };

export const weightText = (weight) => String(Number(Number(weight).toFixed(3)));

/** What a token of a model costs, in words: "0.4 credit per token". */
export const costText = (weight) => `${weightText(weight)} ${Number(weight) === 1 ? "credit" : "credits"} per token`;

/** `{models, default, choices, current, surfaces}`: what this person may choose, and what they chose. */
export const loadModels = (who) => api.get("/v1/models", who);

/** A model's name, the way a list shows it. */
export const modelLabel = (model) => `${model.name} (${model.provider_label})`;

/** Choose `ref` (null: the server's own) for `surface`; the server's answer holds the choices and the model now in use. */
export const chooseModel = (who, surface, ref) => api.put("/v1/models/choice", { ...who, model: ref, for_surface: surface });

/**
 * A select with the server's own model and every model the person may choose. `choice` is the model chosen (null:
 * the server's), `onChange(ref | null)` is called when they pick another.
 */
export function modelSelect(info, choice, onChange, label = "Model") {
  const known = info.models.some((model) => model.ref === choice);
  const select = h("select", { class: "model-select", "aria-label": label },
    h("option", { value: "" }, `Server default: ${info.default.name} (${costText(info.default.weight)})`),
    info.models.map((model) => h("option", { value: model.ref }, `${modelLabel(model)}, ${costText(model.weight)}`)));
  select.value = known ? choice : "";
  select.addEventListener("change", () => onChange(select.value || null));
  return select;
}
