from clara.discord_bot.mentions import add_pings, readable, split_message
from clara.discord_bot.routing import Action, Incoming, conversation_of, route, space_of
from clara.discord_bot.texts import ENGLISH, FRENCH, language, t

from .conftest import FakeUser

CLARA = FakeUser(1, "clara", "Clara", bot=True)
PAUL = FakeUser(2, "paul_42", "Paul")
JEAN = FakeUser(3, "jp", "Jean Pierre")


def incoming(**fields) -> Incoming:
    values = {"from_bot": False, "is_self": False, "private": False, "addressed": False, "signed_in": True,
              "text": "hello"}
    return Incoming(**{**values, **fields})


# --- routing ----------------------------------------------------------------------------------------------


def test_messages_for_clara_are_answered():
    assert route(incoming(addressed=True)) is Action.ANSWER
    assert route(incoming(private=True)) is Action.ANSWER


def test_a_message_with_only_files_is_for_her_only_when_she_is_addressed():
    assert route(incoming(text="", files=True, addressed=True)) is Action.ANSWER
    assert route(incoming(text="", files=True)) is Action.IGNORE
    assert route(incoming(text="", files=True, addressed=True, signed_in=False)) is Action.HINT


def test_other_messages_are_sent_as_maybe_or_only_observed_while_she_is_busy():
    assert route(incoming()) is Action.MAYBE
    assert route(incoming(channel_busy=True)) is Action.OBSERVE


def test_people_without_an_account_get_a_hint_only_when_they_talk_to_her():
    assert route(incoming(signed_in=False, addressed=True)) is Action.HINT
    assert route(incoming(signed_in=False, private=True)) is Action.HINT
    assert route(incoming(signed_in=False)) is Action.IGNORE


def test_bots_herself_and_empty_messages_are_ignored():
    assert route(incoming(from_bot=True, addressed=True)) is Action.IGNORE
    assert route(incoming(is_self=True)) is Action.IGNORE
    assert route(incoming(addressed=True, text="")) is Action.IGNORE


def test_old_style_commands_get_the_slash_hint():
    assert route(incoming(addressed=True, text="/register")) is Action.SLASH
    assert route(incoming(text="/shrug")) is Action.MAYBE  # not for her: just conversation


def test_conversations_and_spaces():
    assert conversation_of(10, private=False) == "discord:channel:10"
    assert conversation_of(10, private=True) is None
    assert space_of(5) == "discord:guild:5"


# --- text ---------------------------------------------------------------------------------------------------


def test_mentions_become_names_and_the_calling_mention_goes():
    text = readable("<@1> tell <@!2> about <#7> and <@&9>, <@8>", [PAUL], 1, "Clara", {9: "mods"}, {7: "news"})
    assert text == "tell @Paul about #news and @mods, @someone"
    assert readable("thanks <@1>!", [], 1, "Clara") == "thanks @Clara!"


def test_names_become_pings_longest_first_never_clara():
    members = [CLARA, PAUL, JEAN, FakeUser(4, "jean", "Jean")]
    assert add_pings("Hi @Jean Pierre and @paul_42, @Clara.", members, 1) == "Hi <@3> and <@2>, @Clara."
    assert add_pings("@Jean, ok", members, 1) == "<@4>, ok"
    assert add_pings("@Nobody here @everyone", members, 1) == "@Nobody here @everyone"
    assert add_pings("mail me at a@Paul.com", members, 1) == "mail me at a@Paul.com"


def test_pings_are_limited():
    members = [FakeUser(10 + i, f"u{i}") for i in range(5)]
    text = " ".join(f"@u{i}" for i in range(5))
    assert add_pings(text, members, 1, max_pings=2) == "<@10> <@11> @u2 @u3 @u4"


def test_long_answers_are_split_and_code_blocks_stay_closed():
    assert split_message("short") == ["short"]
    words = " ".join(["word"] * 1000)
    pieces = split_message(words, limit=100)
    assert all(len(p) <= 100 for p in pieces) and " ".join(pieces) == words
    code = "```py\n" + "\n".join(f"line {i}" for i in range(60)) + "\n```"
    pieces = split_message(code, limit=200)
    assert all(len(p) <= 200 for p in pieces)
    assert all(p.count("```") % 2 == 0 for p in pieces)  # each piece opens and closes its block
    assert pieces[1].startswith("```py\n")


def test_languages():
    assert language("fr") == FRENCH and language("en-GB") == ENGLISH and language(None) == ENGLISH
    assert "/register" in t(FRENCH, "need_account") and "/register" in t(ENGLISH, "need_account")
    assert t(ENGLISH, "logged_in", user="erwan") == "Signed in as **erwan**."
