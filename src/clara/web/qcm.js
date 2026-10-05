// QCM: the form Clara asks through her `qcm` tool. Questions are `single` (radio buttons), `multiple` (check
// boxes) or `text` (a box to type in). The answers go back to Clara as the user's next message, written exactly as
// clara/qcm.py writes them (format_answers), so that the server can show the form answered when the conversation
// is opened again: keep the two in step.

import { icon } from "./icons.js";
import { inline } from "./markdown.js";
import { h } from "./ui.js";

const LETTERS = "ABCDEFGHIJ";
const NO_ANSWER = "(no answer)";
const MARK = /^\[QCM answers [0-9a-f]{8}\] ?([^\n]*)/;
let counter = 0; // makes the names of the radio groups unique on the page

const byNumber = (a, b) => a - b;

/** A span with the text of a question, an option or an explanation: formulas ($x^2$, \(x^2\), $$...$$) typeset, as in the chat. */
function rich(tag, props, text) {
  const node = h(tag, props);
  inline(text, node);
  return node;
}
const emptyAnswer = (question) => (question.type === "text" ? "" : []);

/** The message the user sends for a form. `answers`: one list of option numbers per choice question, one text per text question. */
export function formatAnswers(form, answers) {
  const lines = [`[QCM answers ${form.ref}]${form.title ? " " + form.title : ""}`];
  form.questions.forEach((question, index) => {
    const answer = answers[index];
    const given = question.type === "text"
      ? String(answer || "").trim()
      : [...(answer || [])].sort(byNumber).map((option) => `${LETTERS[option]}. ${question.options[option]}`).join("; ");
    lines.push(`\n${index + 1}. ${question.text}\nAnswer: ${given || NO_ANSWER}`);
  });
  return lines.join("\n");
}

/** How the answers message is shown in the conversation: without the reference the server uses to recognise it. */
export function displayAnswers(text) {
  return text.replace(MARK, (_, title) => (title ? `QCM answers: ${title}` : "QCM answers"));
}

export const isAnswers = (text) => MARK.test(text);

/** `[right, asked]` over the choice questions whose correct options are known. */
export function grade(form, answers) {
  let right = 0;
  let asked = 0;
  form.questions.forEach((question, index) => {
    if (question.type === "text" || !question.correct) return;
    asked += 1;
    const given = [...(answers[index] || [])].sort(byNumber);
    if (given.length === question.correct.length && given.every((option, at) => option === question.correct[at])) right += 1;
  });
  return [right, asked];
}

/**
 * The card of one form. `form.answers` (an array once answered) and `form.draft` (what is selected so far) live on the
 * form itself, so the card can be built again, with the same state, whenever the conversation is drawn again.
 * `submit(text)` sends the answers; it returns false when it cannot just now (Clara is still writing).
 */
export function qcmNode(form, { submit }) {
  const uid = `qcm${(counter += 1)}`;
  form.draft ||= form.questions.map(emptyAnswer);
  const root = h("section", { class: "qcm", "aria-label": form.title || "Questions" });
  let sendButton = null; // the footer's, while the form can still be answered
  let progress = null;
  draw();
  return root;

  function answered() {
    return Array.isArray(form.answers);
  }

  function count() {
    return form.draft.filter((answer) => (typeof answer === "string" ? answer.trim() : answer.length)).length;
  }

  function send() {
    const answers = form.draft.map((answer) => (typeof answer === "string" ? answer.trim() : [...answer].sort(byNumber)));
    if (submit(formatAnswers(form, answers)) === false) return;
    form.answers = answers;
    draw();
  }

  function draw() {
    const done = answered();
    const shown = done ? form.answers : form.draft;
    const total = form.questions.length;
    root.classList.toggle("done", done);
    const head = h("header", { class: "qcm-head" },
      h("span", { class: "qcm-kind" }, "QCM"),
      rich("h3", {}, form.title || (total === 1 ? "A question" : `${total} questions`)),
      form.title && h("span", { class: "qcm-count" }, `${total} ${total === 1 ? "question" : "questions"}`));
    const questions = form.questions.map((question, index) => questionNode(question, index, shown[index], done));
    root.replaceChildren(head, ...questions, footer(done));
  }

  function questionNode(question, index, given, done) {
    const known = done && question.correct;
    const right = known && sameOptions(given, question.correct);
    const body = question.type === "text" ? textBox(question, index, given) : question.options.map((_option, at) => optionNode(question, index, at, given, done));
    return h("fieldset", { class: "qcm-question" + (known ? (right ? " right" : " wrong") : ""), disabled: done },
      h("legend", {}, h("span", { class: "num" }, index + 1), rich("span", { class: "q-text" }, question.text)),
      question.type === "multiple" && h("p", { class: "qcm-hint" }, "Select all that apply."),
      h("div", { class: "qcm-options" }, body),
      known && h("p", { class: "qcm-verdict", role: "status" }, icon(right ? "check" : "close", { size: 16 }), right ? "Correct" : "Not quite"),
      done && question.explanation && rich("p", { class: "qcm-explain" }, question.explanation),
      done && question.type === "text" && question.answer && h("p", { class: "qcm-explain" }, h("strong", {}, "Expected: "), rich("span", {}, question.answer)));
  }

  function optionNode(question, index, at, given, done) {
    const picked = given.includes(at);
    const correct = done && question.correct ? question.correct.includes(at) : null;
    const input = h("input", {
      type: question.type === "single" ? "radio" : "checkbox", name: `${uid}-${index}`, checked: picked,
      onchange: () => {
        const others = form.draft[index].filter((option) => option !== at);
        form.draft[index] = question.type === "single" ? [at] : input.checked ? [...others, at] : others;
        refresh();
      },
    });
    const state = correct === null ? "" : correct ? (picked ? " right" : " missed") : picked ? " wrong" : "";
    return h("label", { class: "qcm-option" + (picked ? " picked" : "") + state }, input,
      h("span", { class: "letter" }, LETTERS[at]), rich("span", { class: "opt-text" }, question.options[at]),
      state.includes("right") && icon("check", { size: 16 }), state.includes("wrong") && icon("close", { size: 16 }),
      state.includes("missed") && h("span", { class: "qcm-note" }, "correct answer"));
  }

  function textBox(question, index, given) {
    const box = h("textarea", {
      rows: 2, placeholder: "Your answer", "aria-label": question.text, value: given, maxLength: 4000,
      oninput: () => { form.draft[index] = box.value; refresh(); },
    });
    return [box];
  }

  function footer(done) {
    if (done) {
      sendButton = progress = null;
      const [right, asked] = form.graded ? grade(form, form.answers) : [0, 0];
      return h("footer", { class: "qcm-foot" }, asked
        ? h("p", { class: "qcm-score", role: "status" }, h("strong", {}, `${right} / ${asked}`), ` correct answer${asked === 1 ? "" : "s"}`)
        : h("p", { class: "qcm-score" }, "Answers sent to Clara."));
    }
    progress = h("span", { class: "qcm-progress" });
    sendButton = h("button", { class: "primary", onclick: send }, icon("send", { size: 16 }), "Send answers");
    refresh();
    return h("footer", { class: "qcm-foot" }, progress, sendButton);
  }

  function refresh() {
    if (!sendButton) return;
    const given = count();
    progress.textContent = `${given} of ${form.questions.length} answered`;
    sendButton.disabled = given === 0;
  }
}

function sameOptions(given, correct) {
  const sorted = [...given].sort(byNumber);
  return sorted.length === correct.length && sorted.every((option, at) => option === correct[at]);
}
