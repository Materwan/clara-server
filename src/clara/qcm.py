"""QCM: a form of questions Clara asks through the `qcm` tool, answered on the web site and in the desktop app.

A question is `single` (one option), `multiple` (any number of options) or `text` (a free answer). Choice
questions may name their correct options, which makes the clients grade them; a `text` question may carry a
reference answer, shown after the user has answered. The QCM is not blocking: the turn ends with the form
displayed, and the answers come back as the user's next message, written like `format_answers` does, so that
the model reads them and the form can be shown answered when the conversation is opened again.

The clients write the same message (`clara-app` in Python, the web site in JavaScript): keep them in step.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_QUESTIONS = 20
MIN_OPTIONS = 2
MAX_OPTIONS = 10
MAX_TITLE = 120
MAX_QUESTION = 500
MAX_OPTION = 200
MAX_NOTE = 500  # an explanation, or the reference answer of a text question
SINGLE, MULTIPLE, TEXT = "single", "multiple", "text"
TYPES = (SINGLE, MULTIPLE, TEXT)
SURFACES = ("web", "app")  # the clients that can show a form

NO_ANSWER = "(no answer)"
_MARK = "[QCM answers {ref}]"
_MARK_RE = re.compile(r"^\[QCM answers ([0-9a-f]{8})\]")
LETTERS = "ABCDEFGHIJ"


def _line(value: Any, name: str, limit: int, required: bool = True) -> str:
    """One line of text: the whitespace folded, at most `limit` characters."""
    if value is None or isinstance(value, (dict, list)):
        value = ""
    text = " ".join(str(value).split())
    if not text and required:
        raise ValueError(f"{name} is empty.")
    if len(text) > limit:
        raise ValueError(f"{name} is too long ({len(text)} characters, at most {limit}).")
    return text


def _note(value: Any, name: str) -> str:
    """A short text that may have several lines."""
    text = "" if value is None or isinstance(value, (dict, list)) else str(value).strip()
    if len(text) > MAX_NOTE:
        raise ValueError(f"{name} is too long ({len(text)} characters, at most {MAX_NOTE}).")
    return text


def _list(value: Any, name: str) -> list:
    if isinstance(value, str):  # some models send a JSON text instead of a list
        try:
            value = json.loads(value)
        except ValueError:
            raise ValueError(f"{name} must be a list.") from None
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{name} must be a list.")
    return list(value)


def _correct(value: Any, count: int, kind: str, where: str) -> list[int]:
    items = _list(value, f"{where}: correct")
    chosen: list[int] = []
    for item in items:
        if isinstance(item, bool) or not isinstance(item, (int, str)) or not str(item).strip().lstrip("-").isdigit():
            raise ValueError(f"{where}: correct holds the numbers of the options, the first being 0.")
        number = int(item)
        if not 0 <= number < count:
            raise ValueError(f"{where}: correct {number} is not an option (0 to {count - 1}).")
        if number not in chosen:
            chosen.append(number)
    if kind == SINGLE and len(chosen) != 1:
        raise ValueError(f"{where}: a single question has exactly one correct option.")
    if kind == MULTIPLE and not chosen:
        raise ValueError(f"{where}: a multiple question needs at least one correct option (or leave correct out).")
    return sorted(chosen)


def _question(raw: Any, number: int) -> dict:
    where = f"Question {number}"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be an object with text, type and options.")
    kind = str(raw.get("type") or SINGLE).strip().lower()
    if kind not in TYPES:
        raise ValueError(f"{where}: type must be one of {', '.join(TYPES)}.")
    question = {
        "text": _line(raw.get("text"), f"{where}: text", MAX_QUESTION),
        "type": kind,
        "options": [],
        "correct": None,
        "explanation": _note(raw.get("explanation"), f"{where}: explanation"),
        "answer": "",
    }
    if kind == TEXT:
        if raw.get("options"):
            raise ValueError(f"{where}: a text question has no options.")
        question["answer"] = _note(raw.get("answer"), f"{where}: answer")
        return question
    options = [_line(option, f"{where}: an option", MAX_OPTION) for option in _list(raw.get("options"), f"{where}: options")]
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        raise ValueError(f"{where}: {MIN_OPTIONS} to {MAX_OPTIONS} options are needed, not {len(options)}.")
    if len({option.casefold() for option in options}) != len(options):
        raise ValueError(f"{where}: the options must differ.")
    question["options"] = options
    if raw.get("correct") not in (None, "", []):
        question["correct"] = _correct(raw["correct"], len(options), kind, where)
    return question


def reference(title: str, questions: list[dict]) -> str:
    """A short id of a form, the same each time it is built from the same questions: the answers carry it."""
    material = json.dumps([title, [[q["text"], q["type"], q["options"]] for q in questions]], ensure_ascii=False)
    return hashlib.sha1(material.encode("utf-8")).hexdigest()[:8]


def build_form(arguments: dict[str, Any]) -> dict:
    """The form the model asked for, checked and cleaned (ValueError says what to fix), as sent to the clients:
    `{"ref", "title", "graded", "questions": [{"text", "type", "options", "correct", "explanation", "answer"}]}`."""
    questions = [_question(raw, number) for number, raw in enumerate(_list(arguments.get("questions"), "questions"), 1)]
    if not questions:
        raise ValueError("A QCM needs at least one question.")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(f"A QCM holds at most {MAX_QUESTIONS} questions, not {len(questions)}.")
    title = _line(arguments.get("title"), "title", MAX_TITLE, required=False)
    return {
        "ref": reference(title, questions),
        "title": title,
        # graded: every choice question names its correct options, so the clients can score the QCM
        "graded": any(q["type"] != TEXT for q in questions)
        and all(q["correct"] is not None for q in questions if q["type"] != TEXT),
        "questions": questions,
    }


def forms_in(tool_calls: list[dict] | None) -> list[dict]:
    """The forms of the `qcm` calls stored with a message (a call that was refused leaves none)."""
    forms = []
    for call in tool_calls or []:
        function = call.get("function") or {}
        if function.get("name") != "qcm":
            continue
        arguments = function.get("arguments")
        try:
            forms.append(build_form(arguments if isinstance(arguments, dict) else {}))
        except ValueError:
            pass
    return forms


# ---- the answers -------------------------------------------------------------------------------------


def _token(question: dict, index: int) -> str:
    return f"{LETTERS[index]}. {question['options'][index]}"


def format_answers(form: dict, answers: list) -> str:
    """The message the user sends for a form: a choice question's answer is a list of option numbers, a
    text question's a string."""
    lines = [_MARK.format(ref=form["ref"]) + (f" {form['title']}" if form["title"] else "")]
    for number, (question, answer) in enumerate(zip(form["questions"], answers), 1):
        if question["type"] == TEXT:
            given = str(answer or "").strip()
        else:
            given = "; ".join(_token(question, index) for index in sorted(answer or []))
        lines.append(f"\n{number}. {question['text']}\nAnswer: {given or NO_ANSWER}")
    return "\n".join(lines)


def answered_ref(message: str) -> str | None:
    """The form a user message answers, if it is the answers to one."""
    found = _MARK_RE.match(message)
    return found.group(1) if found else None


def parse_answers(form: dict, message: str) -> list | None:
    """What `format_answers` wrote, back as one answer per question (None: the message is not for this form)."""
    if answered_ref(message) != form["ref"]:
        return None
    count = len(form["questions"])
    position = message.find("\n\n1. ")
    answers: list = []
    for number, question in enumerate(form["questions"], 1):
        if position < 0:
            answers.append("" if question["type"] == TEXT else [])
            continue
        end = message.find(f"\n\n{number + 1}. ", position + 1) if number < count else -1
        block = message[position + 2 : end if end >= 0 else len(message)]
        position = end
        _, _, given = block.partition("\nAnswer: ")
        given = "" if given == NO_ANSWER else given
        if question["type"] == TEXT:
            answers.append(given)
        else:
            answers.append([index for index in range(len(question["options"])) if _chosen(_token(question, index), given)])
    return answers


def _chosen(token: str, given: str) -> bool:
    """Is `token` one of the "; "-separated choices of `given`?"""
    return given == token or given.startswith(token + "; ") or given.endswith("; " + token) or f"; {token}; " in given


def grade(form: dict, answers: list) -> tuple[int, int]:
    """`(right, asked)` over the choice questions that name their correct options."""
    right = asked = 0
    for question, answer in zip(form["questions"], answers):
        if question["type"] == TEXT or question["correct"] is None:
            continue
        asked += 1
        right += sorted(answer or []) == question["correct"]
    return right, asked


def with_answers(forms: list[dict], later: list[str]) -> list[dict]:
    """The forms, each with the `answers` the user gave in one of the messages `later` (None: not answered)."""
    done = []
    for form in forms:
        answers = next((found for message in later if (found := parse_answers(form, message)) is not None), None)
        done.append({**form, "answers": answers})
    return done
