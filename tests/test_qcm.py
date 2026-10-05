"""QCM: the form the model builds, the answers read back, the tool, and what the clients are sent."""

import json

import pytest
from conftest import FakeBackend, call, fake_providers, say
from fastapi.testclient import TestClient

from clara.qcm import (
    MAX_OPTIONS,
    MAX_QUESTIONS,
    build_form,
    format_answers,
    forms_in,
    grade,
    parse_answers,
    with_answers,
)
from clara.server import create_app

AUTH = {"Authorization": "Bearer secret-cli"}

QUESTIONS = [
    {"text": "Capital of France?", "options": ["Paris", "Lyon"], "correct": [0]},
    {"text": "Primes?", "type": "multiple", "options": ["2", "4", "5"], "correct": [0, 2], "explanation": "4 = 2 x 2"},
    {"text": "Why?", "type": "text", "answer": "Because."},
]


def test_a_form_is_cleaned_and_graded_when_every_choice_has_its_answer():
    form = build_form({"title": "  Geo  quiz ", "questions": QUESTIONS})
    assert form["title"] == "Geo quiz" and form["graded"] and len(form["ref"]) == 8
    first, second, third = form["questions"]
    assert first["type"] == "single" and first["correct"] == [0]
    assert second["correct"] == [0, 2] and second["explanation"] == "4 = 2 x 2"
    assert third == {"text": "Why?", "type": "text", "options": [], "correct": None, "explanation": "", "answer": "Because."}


def test_without_correct_options_the_form_is_not_graded():
    form = build_form({"questions": [{"text": "Favourite?", "options": ["a", "b"]}, QUESTIONS[0]]})
    assert not form["graded"]
    assert not build_form({"questions": [QUESTIONS[2]]})["graded"]


def test_the_ref_follows_the_questions_only():
    one = build_form({"questions": QUESTIONS})
    assert one["ref"] == build_form({"questions": QUESTIONS})["ref"]
    assert one["ref"] != build_form({"questions": QUESTIONS[:2]})["ref"]


@pytest.mark.parametrize(
    "arguments, fragment",
    [
        ({}, "questions must be a list"),
        ({"questions": []}, "at least one"),
        ({"questions": [{"text": "q", "options": ["a", "b"]}] * (MAX_QUESTIONS + 1)}, "at most"),
        ({"questions": [{"text": "q", "options": ["a"]}]}, "options are needed"),
        ({"questions": [{"text": "q", "options": [str(n) for n in range(MAX_OPTIONS + 1)]}]}, "options are needed"),
        ({"questions": [{"text": "q", "options": ["a", "A"]}]}, "must differ"),
        ({"questions": [{"text": "", "options": ["a", "b"]}]}, "text is empty"),
        ({"questions": [{"text": "q", "type": "radio", "options": ["a", "b"]}]}, "type must be"),
        ({"questions": [{"text": "q", "type": "text", "options": ["a", "b"]}]}, "no options"),
        ({"questions": [{"text": "q", "options": ["a", "b"], "correct": [2]}]}, "not an option"),
        ({"questions": [{"text": "q", "options": ["a", "b"], "correct": [0, 1]}]}, "exactly one"),
        ({"questions": [{"text": "q", "type": "multiple", "options": ["a", "b"], "correct": ["x"]}]}, "numbers"),
        ({"questions": ["q"]}, "must be an object"),
    ],
)
def test_a_bad_form_is_refused_with_the_reason(arguments, fragment):
    with pytest.raises(ValueError, match=fragment):
        build_form(arguments)


def test_the_limit_is_twenty_questions_and_ten_options():
    assert MAX_QUESTIONS == 20 and MAX_OPTIONS == 10
    build_form({"questions": [{"text": "q", "options": [str(n) for n in range(10)]}] * 20})


def test_questions_sent_as_json_text_are_accepted():
    assert len(build_form({"questions": json.dumps(QUESTIONS)})["questions"]) == 3


def test_answers_survive_the_message_they_are_written_in():
    form = build_form({"title": "Geo", "questions": QUESTIONS})
    answers = [[1], [0, 2], "Because\n\n2. not a question; B. Lyon"]
    message = format_answers(form, answers)
    assert message.startswith(f"[QCM answers {form['ref']}] Geo\n\n1. Capital of France?\nAnswer: B. Lyon")
    assert parse_answers(form, message) == answers
    assert grade(form, answers) == (1, 2)
    assert grade(form, [[0], [0, 2], ""]) == (2, 2)
    assert grade(form, [[0], [0], ""]) == (1, 2)


def test_unanswered_questions_come_back_empty():
    form = build_form({"questions": QUESTIONS})
    assert parse_answers(form, format_answers(form, [[], [], " "])) == [[], [], ""]


def test_a_message_for_another_form_is_not_its_answers():
    form = build_form({"questions": QUESTIONS})
    other = build_form({"questions": QUESTIONS[:2]})
    assert parse_answers(form, format_answers(other, [[0], [0]])) is None
    assert parse_answers(form, "hello") is None


def test_with_answers_pairs_each_form_with_its_message():
    one = build_form({"questions": QUESTIONS[:1]})
    two = build_form({"questions": QUESTIONS[1:2]})
    done = with_answers([one, two], ["hi", format_answers(two, [[2]])])
    assert done[0]["answers"] is None and done[1]["answers"] == [[2]]


def test_stored_calls_give_their_forms_and_skip_refused_ones():
    good = {"function": {"name": "qcm", "arguments": {"questions": QUESTIONS}}}
    bad = {"function": {"name": "qcm", "arguments": {"questions": []}}}
    other = {"function": {"name": "remember", "arguments": {"fact": "x"}}}
    assert len(forms_in([other, bad, good])) == 1
    assert forms_in(None) == []


# ---- through the server ----------------------------------------------------------------------------


class Server:
    """A test client of the app, with a scripted model."""

    def __init__(self, settings, *rounds):
        self.backend = FakeBackend(*rounds)
        self.client = TestClient(create_app(settings, fake_providers(settings, self.backend)))

    def __enter__(self):
        self.client.__enter__()
        return self

    def __exit__(self, *exc):
        self.client.__exit__(*exc)

    def chat(self, surface, message="quiz me", conversation=None):
        body = {"surface": surface, "user_id": "erwan", "message": message}
        if conversation:
            body["conversation"] = conversation
        with self.client.stream("POST", "/v1/chat/stream", json=body, headers=AUTH) as response:
            return [json.loads(line[6:]) for line in response.iter_lines() if line.startswith("data: ")]

    def messages(self, conversation):
        params = {"surface": "web", "user_id": "erwan"}
        return self.client.get(f"/v1/conversations/{conversation}/messages", params=params, headers=AUTH).json()


def test_the_model_asks_a_qcm_and_the_client_receives_the_form(settings):
    with Server(settings, call("qcm", title="Geo", questions=QUESTIONS), say("Good luck!")) as server:
        events = server.chat("web")
    kinds = [e["type"] for e in events]
    forms = [e["form"] for e in events if e["type"] == "qcm"]
    assert len(forms) == 1 and forms[0]["title"] == "Geo" and forms[0]["graded"]
    assert kinds.index("tool") < kinds.index("qcm")
    assert events[-1]["type"] == "done" and events[-1]["reply"] == "Good luck!"


def test_a_second_qcm_in_one_answer_is_refused(settings):
    one = {"questions": QUESTIONS[:1]}
    with Server(settings, call("qcm", **one), call("qcm", **one), say("ok")) as server:
        events = server.chat("app")
    tools = [e for e in events if e["type"] == "tool"]
    assert [e["result"].startswith("Error") for e in tools] == [False, True]
    assert len([e for e in events if e["type"] == "qcm"]) == 1


def test_a_bad_form_gives_the_model_an_error_to_read_and_the_client_nothing(settings):
    with Server(settings, call("qcm", questions=[{"text": "q", "options": ["only"]}]), say("sorry")) as server:
        events = server.chat("web")
    assert not [e for e in events if e["type"] == "qcm"]
    assert "options are needed" in next(e for e in events if e["type"] == "tool")["result"]


def test_other_clients_are_not_offered_the_tool(settings):
    with Server(settings, say("hello"), say("hello")) as server:
        server.chat("cli")
        server.chat("web", conversation="web:erwan:1")
        offered = [{t["function"]["name"] for t in (tools or [])} for _, tools in server.backend.calls]
    assert "qcm" not in offered[0] and "qcm" in offered[1]


def test_reopening_a_conversation_gives_the_form_and_its_answers(settings):
    form = build_form({"title": "Geo", "questions": QUESTIONS})
    answers = format_answers(form, [[0], [0, 2], "Because"])
    conversation = "web:erwan:quiz"
    with Server(settings, call("qcm", title="Geo", questions=QUESTIONS), say("Here it is."), say("2 of 2.")) as server:
        server.chat("web", conversation=conversation)
        before = [m for m in server.messages(conversation)["messages"] if "qcm" in m]
        assert len(before) == 1 and before[0]["qcm"][0]["answers"] is None
        assert before[0]["qcm"][0]["questions"][0]["options"] == ["Paris", "Lyon"]
        server.chat("web", message=answers, conversation=conversation)
        after = server.messages(conversation)["messages"]
    asked = [m for m in after if "qcm" in m]
    assert len(asked) == 1 and asked[0]["qcm"][0]["answers"] == [[0], [0, 2], "Because"]
    assert [m["role"] for m in after] == ["user", "assistant", "assistant", "user", "assistant"]
