"""The brain: one turn of conversation, whatever client it comes from.

    resolve the person -> wait for the conversation's turn -> build the prompt
    -> stream the model, running tools -> store the turn -> compact if too long

`Agent.turn()` is the only entry point and yields events (dicts), so the same
code serves streaming clients (events as they come) and plain ones (last event).

Tools come in two kinds. *Server tools* (`remember`, `forget`) run here. *Client
tools* are described by the client in its request and run on the client's machine
(files, shell...): when the model calls one, the turn emits a `tool_requests`
event and waits until the client posts the results (`submit_results`), then goes
on with the next model round. The client never has to open a second stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid
import weakref
from dataclasses import dataclass, replace
from datetime import datetime
from typing import AsyncIterator, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .compaction import (
    build_transcript,
    chunk_messages,
    estimate_message_tokens,
    estimate_prompt_tokens,
    estimate_tokens,
    summary_request,
    transcript_budget,
)
from .limits import UsageLimitReached, UsageLimits
from .llm import LlmBackend, LlmChunk
from .markdownfiles import MarkdownFiles
from .memory import ConversationState, Fact, Memory, Person, StoredMessage, TurnRow
from .models import Chosen, ModelCatalog
from .notifications import SERVER, SURFACE_RE, NotificationError, Notifier
from .projects import PROJECT_TOOLS, Projects
from .prompt import SystemPrompt
from .qcm import SURFACES as QCM_SURFACES
from .reminders import ReminderService
from .tasks import TaskService
from .tools import ABOUT_PERSON, MARKDOWN_TOOLS, QCM, Toolbox, ToolContext

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 40
MAX_FACTS_IN_PROMPT = 100
DEFAULT_FACTS_TOKEN_BUDGET = 2_000
RECENT_TOOL_RESULTS_KEPT = 8  # at most this many tool outputs of past turns are replayed...
TOOL_HISTORY_SHARE = 0.25  # ...and only while they fit in this share of the window; older ones become a note
OMITTED = "[output omitted to save context]"
OMITTED_IN_TURN = "[output omitted to fit the context: run the tool again if you still need it]"
NOT_RUN = "[not run: the answer was interrupted]"
TOOL_EVENT_RESULT = 500  # characters of a server tool's result shown to the client
DEFAULT_CONTEXT_WINDOW = 32_768
PROMPT_LIMIT = 0.95  # share of the window a prompt may fill; beyond, the model would truncate it silently
ROUND_SEPARATOR = "\n\n"  # between the texts of two model rounds
MAX_MESSAGE_SHARE = 0.5  # share of the window a single new message may take
NOTIFIED_REPLY = 200  # characters of the answer quoted in the notification of a long turn
TITLE_EXCERPT = 1_500  # characters of the first question, and of the first answer, a title is written from
TITLE_LENGTH = 60
MODES = ("answer", "observe", "maybe")
PASS = "<pass>"  # what the model answers to a "maybe" message it has nothing to add to
MAYBE_NOTE = (
    "[This message was not addressed to you. Answer only if you have something genuinely useful or welcome to "
    f"add, or if they clearly want you; otherwise reply exactly {PASS} and nothing else.]"
)
MAX_FOCUS = 5  # people the message is about whose facts are shown
FOCUS_FACTS = 15  # facts shown for each of them (the newest)
TITLE_INSTRUCTIONS = (
    "Write a title for the conversation below: 3 to 6 words saying what it is about, in the language it "
    "is written in. Answer with the title alone: no quotes, no full stop, no comment."
)


def clean_title(text: str) -> str:
    """The title a model wrote, as it is shown: one line, no quotes or "Title:", no full stop, not too long."""
    line = next((line for line in text.splitlines() if line.strip()), "")
    line = re.sub(r"^\s*#*\s*", "", line)
    line = re.sub(r"^(?:\*\*)?(?:title|titre)\s*:\s*", "", line, flags=re.IGNORECASE)
    line = " ".join(line.strip().strip("*_`\"'«»“”‘’").split()).rstrip(" .:;!")
    if len(line) > TITLE_LENGTH:
        line = line[:TITLE_LENGTH].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return line


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def passed(reply: str) -> bool:
    """Did the model decline to answer a "maybe" message?"""
    text = reply.strip().strip("`*_.!").strip().lower()
    return not text or text in (PASS, "pass", "[pass]") or text.startswith(PASS)


def now_in(timezone: str | None) -> datetime:
    """The current time in an IANA timezone ("Europe/Paris"), or the server's own."""
    return datetime.now(ZoneInfo(timezone)) if timezone else datetime.now().astimezone()


class NothingToCompact(Exception):
    """The conversation has no messages to summarise."""


class ClientToolTimeout(Exception):
    """The client did not send the results of its tools in time."""


class ModelTimeout(Exception):
    """The model stopped answering."""


class PromptTooLarge(Exception):
    """What must be sent to the model does not fit in its context window."""


class NothingToTitle(Exception):
    """The conversation is not listed, or has nothing a title could be written from."""


class ServerStopping(Exception):
    """The server is shutting down: it finishes what is running and takes nothing new."""


async def with_idle_timeout(
    stream: AsyncIterator[LlmChunk], first: float, idle: float
) -> AsyncIterator[LlmChunk]:
    """The chunks of `stream`; ModelTimeout if none comes within `first` seconds, then `idle`
    seconds of each other (a model that has hung would hold its slot for ever)."""
    iterator = stream.__aiter__()
    wait = first
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), wait)
            except StopAsyncIteration:
                return
            except asyncio.TimeoutError:
                what = "to start answering" if wait == first else "between two pieces of its answer"
                raise ModelTimeout(f"The model took more than {wait:g} seconds {what}.") from None
            wait = idle
            yield chunk
    finally:
        close = getattr(iterator, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


_END = object()


@dataclass(frozen=True)
class ChatRequest:
    surface: str  # "cli", "discord", "web"...: where the person is talking from
    user_id: str  # the person's id on that surface
    user_name: str | None
    message: str
    conversation: str | None = None  # default: a private thread per account
    tools: tuple[dict, ...] = ()  # client tools, as function schemas
    instructions: str = ""  # added to the system prompt (what the client is for)
    prefix: str = ""  # shown to the model before the message, never in summaries
    ephemeral: bool = False  # a one-shot job: no persona, no memory, nothing stored
    timezone: str | None = None  # IANA name for the date and time shown to the model (default: the server's)
    no_tools: bool = False  # the server's own tools are not offered (the model can only write)
    quiet: bool = False  # never notify the person about this turn (it is itself an announcement)
    # A group space (a Discord server): its id, the members who have an account, the people the message is about
    space: str | None = None
    roster: tuple[Person, ...] = ()
    focus: tuple[Person, ...] = ()
    # "answer" (the message is for Clara), "observe" (it was not: only stored, as context) or "maybe" (it was
    # not, and Clara answers only if she has something worth adding; else it is only stored)
    mode: str = "answer"
    project: int | None = None  # the project the conversation is in (its files are in the prompt, or read by tools)

    @property
    def conversation_id(self) -> str:
        return self.conversation or f"{self.surface}:{self.user_id}"

    @property
    def group(self) -> bool:
        """Several people talk in this conversation: every message carries its author's name."""
        return self.space is not None


@dataclass
class _Pending:
    owner: str
    expected: frozenset[str]
    future: asyncio.Future


class Agent:
    def __init__(
        self,
        memory: Memory,
        backend: LlmBackend,
        toolbox: Toolbox,
        prompt: SystemPrompt,
        history_turns: int = 20,
        max_concurrent_llm: int = 2,
        max_tool_rounds: int = MAX_TOOL_ROUNDS,
        context_window: int | Callable[[], int] = DEFAULT_CONTEXT_WINDOW,
        compact_percent: int = 80,
        keep_recent_turns: int = 2,
        facts_token_budget: int = DEFAULT_FACTS_TOKEN_BUDGET,
        purge_summarised: bool = False,
        transcript_chars: int | None = None,
        tool_timeout: float = 900.0,
        first_token_timeout: float = 300.0,
        idle_timeout: float = 120.0,
        clock: Callable[[str | None], datetime] = now_in,
        reminders: ReminderService | None = None,
        notifier: Notifier | None = None,
        long_turn_seconds: float = 0.0,
        projects: Projects | None = None,
        markdown: MarkdownFiles | None = None,
        limits: UsageLimits | None = None,
        models: ModelCatalog | None = None,
        tasks: TaskService | None = None,
    ):
        self.memory = memory
        self.projects = projects  # the files of the conversations that are part of a project
        self.markdown = markdown  # the markdown files Clara writes for a person (the markdown tools)
        self.limits = limits  # the daily credits of each person (limits.py); None: nobody is limited
        self.models = models  # the model each person chose, and what it costs (models.py); None: one backend
        self.reminders = reminders  # lets the model's tools set reminders
        self.tasks = tasks  # lets the model's tools keep the person's to-do list
        self.notifier = notifier  # lets the model notify, and the agent say when long work is done
        self.long_turn_seconds = long_turn_seconds  # a turn this long notifies its person when done (0: never)
        self.backend = backend
        self.toolbox = toolbox
        self.prompt = prompt
        self.history_turns = history_turns
        self.max_tool_rounds = max_tool_rounds
        self.compact_percent = compact_percent
        self.keep_recent_turns = keep_recent_turns  # turns a compaction leaves as they are
        self.facts_token_budget = facts_token_budget  # tokens of facts shown in the system prompt
        self.purge_summarised = purge_summarised  # delete messages once a summary stands for them
        self._transcript_chars = transcript_chars  # characters per summary request (default: from the window)
        self.tool_timeout = tool_timeout
        self.first_token_timeout = first_token_timeout  # seconds before the model starts answering
        self.idle_timeout = idle_timeout  # seconds the model may pause once it has started
        self._clock = clock
        self._context_window = context_window
        self.stats = AgentStats()
        self.max_concurrent_llm = max_concurrent_llm
        self._llm_slots = asyncio.Semaphore(max_concurrent_llm)
        # One lock per conversation: two messages of the same thread are answered in
        # order, different threads run in parallel. Unused locks are garbage collected.
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._pending: dict[str, _Pending] = {}
        self.accepting = True  # False once the server is stopping: no new turn, no new compaction
        self._compactions = 0

    @property
    def busy(self) -> bool:
        """Is a reply, an agent turn (waiting for its client's tools too) or a compaction running?"""
        return self.stats.active > 0 or self._compactions > 0

    def _conversation_lock(self, conversation: str) -> asyncio.Lock:
        lock = self._locks.get(conversation)
        if lock is None:
            lock = self._locks[conversation] = asyncio.Lock()
        return lock

    @property
    def window(self) -> int:
        return self._context_window() if callable(self._context_window) else self._context_window

    # ------------------------------------------------------------------
    # Client tools
    # ------------------------------------------------------------------
    def validate(self, request: ChatRequest) -> None:
        """Raise ValueError if the request's tools are unusable (checked before streaming)."""
        if request.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if request.mode != "answer" and (request.ephemeral or request.tools):
            raise ValueError("A message Clara may not answer cannot be ephemeral or bring tools.")
        if request.timezone:
            try:
                ZoneInfo(request.timezone)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                raise ValueError(f"Unknown timezone: {request.timezone}") from None
        names: set[str] = set()
        for schema in request.tools:
            function = schema.get("function") if isinstance(schema, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or not name:
                raise ValueError("Each tool needs a function name.")
            if name in names or (not request.ephemeral and name in self.toolbox.names):
                raise ValueError(f"Tool name used twice, or reserved by the server: {name}")
            names.add(name)

    def submit_results(self, turn_id: str, owner: str, results: dict[str, str]) -> None:
        """Hand the client's tool results to the turn that waits for them.

        KeyError: no such turn waiting (or it belongs to another client).
        ValueError: the results do not match the calls that were requested.
        """
        pending = self._pending.get(turn_id)
        if pending is None or pending.owner != owner or pending.future.done():
            raise KeyError(turn_id)
        if set(results) != pending.expected:
            raise ValueError(f"Expected results for exactly: {', '.join(sorted(pending.expected))}")
        pending.future.set_result(results)

    async def _wait_for_client(self, turn_id: str) -> dict[str, str]:
        pending = self._pending[turn_id]
        try:
            return await asyncio.wait_for(pending.future, self.tool_timeout)
        except asyncio.TimeoutError:
            raise ClientToolTimeout("The client did not return its tool results in time.") from None
        finally:
            self._pending.pop(turn_id, None)

    # ------------------------------------------------------------------
    # Prompt
    # ------------------------------------------------------------------
    @staticmethod
    def _user_content(text: str, prefix: str = "", author: str = "") -> str:
        content = f"{prefix}\n\n{text}" if prefix else text
        return f"{author}: {content}" if author else content

    def _recent_tool_rows(self, stored: list[StoredMessage], window: int | None = None) -> set[int]:
        """The tool outputs replayed in full: the newest, at most RECENT_TOOL_RESULTS_KEPT of them and while
        they fit in TOOL_HISTORY_SHARE of the window (the newest is always kept)."""
        budget = TOOL_HISTORY_SHARE * (window or self.window)
        kept: set[int] = set()
        used = 0
        for index in reversed([i for i, message in enumerate(stored) if message.role == "tool"]):
            cost = estimate_tokens(stored[index].content)
            if len(kept) >= RECENT_TOOL_RESULTS_KEPT or (kept and used + cost > budget):
                break
            kept.add(index)
            used += cost
        return kept

    def _replay(
        self, stored: list[StoredMessage], person: Person, group: bool = False, window: int | None = None
    ) -> list[dict]:
        """Past messages as the model wants them. Old tool outputs are cut to save context. Messages of other
        people carry their name; in a group, everybody's do."""
        recent = self._recent_tool_rows(stored, window)
        messages: list[dict] = []
        for index, message in enumerate(stored):
            if message.role == "user":
                other = (group or message.person_id != person.id) and message.author
                content = self._user_content(message.content, message.prefix, message.author if other else "")
                messages.append({"role": "user", "content": content})
            elif message.role == "assistant":
                entry: dict = {"role": "assistant", "content": message.content}
                if message.tool_calls:
                    entry["tool_calls"] = message.tool_calls
                    if message.thinking:
                        entry["thinking"] = message.thinking
                messages.append(entry)
            else:
                content = message.content if index in recent else OMITTED
                messages.append({"role": "tool", "tool_name": message.tool_name, "content": content})
        return messages

    def _facts_for_prompt(self, person: Person) -> tuple[list[Fact], int]:
        """The newest facts that fit the token budget (oldest first), and how many were left out."""
        shown: list[Fact] = []
        used = 0
        for fact in reversed(self.memory.facts(person.id, MAX_FACTS_IN_PROMPT)):  # newest first
            cost = estimate_tokens(f"- [{fact.id}] {fact.text}") + 1
            if used + cost > self.facts_token_budget:
                break
            shown.append(fact)
            used += cost
        shown.reverse()
        return shown, max(0, self.memory.fact_count(person.id) - len(shown))

    def _build_messages(
        self, request: ChatRequest, person: Person, state: ConversationState, window: int | None = None
    ) -> list[dict]:
        messages: list[dict] = []
        now = self._clock(request.timezone)
        if request.ephemeral:
            if request.instructions.strip():
                messages.append({"role": "system", "content": request.instructions.strip()})
        else:
            facts, omitted = self._facts_for_prompt(person)
            others = tuple(
                (other, self.memory.facts(other.id, FOCUS_FACTS))
                for other in request.focus[:MAX_FOCUS]
                if other.id != person.id
            )
            project = self._project(request, window)
            system = self.prompt.render(
                person, request.surface, facts, now, request.instructions, state.summary, omitted,
                self.memory.relation(person.id), request.roster, others,
                project=project.text if project else "",
            )
            messages.append({"role": "system", "content": system})
            stored = self.memory.history(request.conversation_id, self.history_turns, state.upto_id)
            messages.extend(self._replay(stored, person, request.group, window))
        content = self._user_content(request.message, request.prefix, person.name if request.group else "")
        if not request.ephemeral:
            # Only here, never stored: replayed history and system prompt stay identical between turns
            note = f"{MAYBE_NOTE}\n" if request.mode == "maybe" else ""
            content = f"[time: {now.strftime('%H:%M')}]\n{note}\n{content}"
        messages.append({"role": "user", "content": content})
        return messages

    def _project(self, request: ChatRequest, window: int | None = None):
        if request.project is None or request.ephemeral or self.projects is None:
            return None
        return self.projects.context(request.project, window or self.window)

    def _project_tools_needed(self, request: ChatRequest, window: int | None = None) -> bool:
        """Are the files of the request's project too big for the prompt, so that Clara reads them with tools?"""
        if request.project is None or request.ephemeral or self.projects is None:
            return False
        return not self.projects.inline(request.project, window or self.window)

    # ------------------------------------------------------------------
    # The model of a turn
    # ------------------------------------------------------------------
    def choose(self, surface: str, person_id: int | None) -> Chosen:
        """The model a person is answered by on a surface, what a token of it costs and its window."""
        if self.models is None:
            return Chosen(None)
        return self.models.choose(surface, person_id)

    def _chosen_for(self, conversation: str) -> Chosen:
        """The model of a conversation's owner on its surface (the server's own when it has several people)."""
        if self.models is None:
            return Chosen(None)
        people = self.memory.people_in_conversation(conversation)
        return self.models.choose(conversation.partition(":")[0], people[0] if len(people) == 1 else None)

    # ------------------------------------------------------------------
    # A turn
    # ------------------------------------------------------------------
    async def turn(self, request: ChatRequest, owner: str = "") -> AsyncIterator[dict]:
        """Run one turn.

        Events: `turn` (its id), `token`, `tool` (a server tool ran), `tool_requests` (the client
        must run these and answer through `submit_results`), `usage` (one model round),
        `compacted`, `warning`, then a final `done`.
        """
        if not self.accepting:
            raise ServerStopping("The server is stopping and takes no new question.")
        self.stats.active += 1
        self.stats.turns += 1
        started = time.monotonic()
        try:
            # aclosing: if the client goes away, the turn is closed now (its lock and its
            # pending tool request released), not whenever the garbage collector gets to it
            async with contextlib.aclosing(self._turn(request, owner)) as events:
                async for event in events:
                    if event["type"] == "done":
                        self.stats.prompt_tokens += event["usage"]["prompt_tokens"]
                        self.stats.completion_tokens += event["usage"]["completion_tokens"]
                        self._long_turn_done(request, event, time.monotonic() - started)
                    yield event
        finally:
            self.stats.active -= 1

    # ------------------------------------------------------------------
    # Notifications of the server's own long work
    # ------------------------------------------------------------------
    def _notify(self, person_id: int, text: str, title: str, targets: tuple[str, ...], conversation: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier.notify(person_id, text, title, targets, SERVER, conversation, limited=False)
        except NotificationError as error:
            log.warning("could not notify person %s: %s", person_id, error)

    def notify_threshold(self, person_id: int) -> float:
        """Seconds a task of this person takes before it notifies them when done (0: never): what they set,
        else the server's default."""
        own = self.memory.notify_after(person_id)
        return self.long_turn_seconds if own is None else own

    def _long_turn_done(self, request: ChatRequest, done: dict, seconds: float) -> None:
        """A turn that took long is finished: its person may have gone to do something else, tell them."""
        if request.ephemeral or request.quiet:
            return
        threshold = self.notify_threshold(done["person"]["id"])
        if not threshold or seconds < threshold:
            return
        reply = " ".join(done["reply"].split())
        if len(reply) > NOTIFIED_REPLY:
            reply = reply[: NOTIFIED_REPLY - 1] + "…"
        minutes, rest = divmod(int(seconds), 60)
        took = f"{minutes} min {rest:02d} s" if minutes else f"{rest} s"
        text = f"Clara has finished answering in {done['conversation']} (it took {took})."
        if reply:
            text += f"\n{reply}"
        self._notify(done["person"]["id"], text, "Answer ready", (), done["conversation"])

    def _compaction_done(self, conversation: str, before: float, after: float) -> None:
        """Tell the person a conversation of theirs was summarised (not a conversation of several people)."""
        people = self.memory.people_in_conversation(conversation)
        if len(people) != 1:
            return
        surface = conversation.partition(":")[0]
        targets = (surface,) if SURFACE_RE.match(surface) else ()
        self._notify(
            people[0],
            f"The conversation {conversation} was summarised: its context went from {before:.0f}% to "
            f"{after:.0f}% of the model's window. The older messages are now replaced by a summary.",
            "Conversation summarised",
            targets,
            conversation,
        )

    async def _turn(self, request: ChatRequest, owner: str) -> AsyncIterator[dict]:
        self.validate(request)
        known = self.memory.find_person(request.surface, request.user_id)
        chosen = self.choose(request.surface, known.id if known else None)
        window = chosen.window or self.window
        new_tokens = estimate_tokens(request.message) + estimate_tokens(request.prefix)
        if new_tokens > MAX_MESSAGE_SHARE * window:
            raise PromptTooLarge(
                f"This message is too long for the model: about {new_tokens:,} tokens, and at most "
                f"{int(MAX_MESSAGE_SHARE * window):,} fit (the context window is {window:,})."
            )
        person = self.memory.resolve(request.surface, request.user_id, request.user_name)
        conversation = request.conversation_id
        ephemeral = request.ephemeral
        if self.limits is not None and request.mode != "observe":
            try:
                self.limits.check(person.id)
            except UsageLimitReached:
                if request.mode != "maybe":
                    raise
                request = replace(request, mode="observe")  # Clara was not asked: she just does not chime in
        if request.mode == "observe":
            async for event in self._observe(request, person, chosen):
                yield event
            return
        context = ToolContext(
            person, self.memory, self.reminders, request.timezone, request.surface, request.user_id, conversation,
            self.notifier, roster=tuple(p for p in request.roster if p.id != person.id),
            projects=self.projects, project_id=request.project, markdown=self.markdown, tasks=self.tasks,
        )
        client_tools = {schema["function"]["name"] for schema in request.tools}
        hidden = set() if context.roster else {ABOUT_PERSON}
        if not self._project_tools_needed(request, window):
            hidden |= PROJECT_TOOLS
        if request.surface not in QCM_SURFACES:
            hidden.add(QCM)  # the other clients have no form to show
        if self.markdown is None:
            hidden |= MARKDOWN_TOOLS
        server_tools = [] if ephemeral or request.no_tools else self.toolbox.schemas_without(hidden)
        schemas = server_tools + list(request.tools)
        turn_id = uuid.uuid4().hex
        yield {"type": "turn", "id": turn_id}

        lock = contextlib.nullcontext() if ephemeral else self._conversation_lock(conversation)
        async with lock:
            state = ConversationState() if ephemeral else self.memory.state(conversation)
            if not ephemeral and self._history_overflows(conversation, state):
                # Older turns would fall out of the prompt without ever being summarised: summarise
                # them now, and keep half the history so this does not happen at every turn.
                try:
                    before, after = await self._compact_locked(
                        conversation, keep_recent_turns=max(1, self.history_turns // 2), chosen=chosen
                    )
                    yield {"type": "compacted", "before": before, "after": after}
                except Exception as error:
                    log.warning("compaction of %s before the turn failed: %s", conversation, error)
                    yield {"type": "warning", "message": f"Could not compact the conversation: {error}"}
                state = self.memory.state(conversation)  # also after a failure: it may have advanced
            messages = self._build_messages(request, person, state, window)
            rows: list[TurnRow] = []
            reply_parts: list[str] = []
            tools_used: list[str] = []
            prompt_tokens = completion_tokens = context_tokens = credits = 0
            text_parts: list[str] = []  # what the model wrote in the round in progress
            round_open = False  # that text is not in `rows` yet
            finished = False
            reason = "the connection to the client was lost"
            try:
                for round_number in range(self.max_tool_rounds + 1):
                    offer_tools = round_number < self.max_tool_rounds  # the last round must answer
                    async for event in self._fit(
                        request, person, messages, schemas if offer_tools else None,
                        compact=round_number == 0, chosen=chosen,
                    ):
                        yield event
                    text_parts = []
                    thinking_parts: list[str] = []
                    round_open = True
                    calls = []
                    round_prompt = round_completion = 0
                    # a round that follows one that said something starts a new paragraph
                    separate = bool("".join(reply_parts).strip())
                    # aclosing: if the client goes away at a yield, the model task stops now
                    try:
                        async with contextlib.aclosing(
                            self._model(messages, schemas if offer_tools else None, chosen.ref)
                        ) as model:
                            async for chunk in model:
                                round_prompt += chunk.prompt_tokens
                                round_completion += chunk.completion_tokens
                                calls.extend(chunk.tool_calls)
                                if chunk.thinking:  # shown to the client; kept only with tool calls (see below)
                                    thinking_parts.append(chunk.thinking)
                                    yield {"type": "thinking", "text": chunk.thinking}
                                if chunk.text:
                                    if separate and chunk.text.strip():
                                        separate = False
                                        reply_parts.append(ROUND_SEPARATOR)
                                        yield {"type": "token", "text": ROUND_SEPARATOR}
                                    text_parts.append(chunk.text)
                                    yield {"type": "token", "text": chunk.text}
                    except BaseException as error:
                        # The client left: what the model did is still counted against the person's day (a model
                        # that failed costs them nothing)
                        if isinstance(error, (GeneratorExit, asyncio.CancelledError)):
                            credits += self._count_usage(
                                person, round_prompt + round_completion, messages,
                                schemas if offer_tools else None, text_parts, chosen.weight,
                            )
                        raise
                    else:
                        credits += self._count_usage(
                            person, round_prompt + round_completion, messages,
                            schemas if offer_tools else None, text_parts, chosen.weight,
                        )
                    prompt_tokens += round_prompt
                    completion_tokens += round_completion
                    text = "".join(text_parts)
                    reported = round_prompt + round_completion
                    estimated = estimate_prompt_tokens(messages, schemas if offer_tools else None) + estimate_tokens(text)
                    log.debug("context of %s: model reported %d tokens, estimated %d", conversation, reported, estimated)
                    # With a prompt cache the model may report only what it evaluated this time
                    context_tokens = max(reported, estimated)
                    yield {"type": "usage", "prompt_tokens": round_prompt, "completion_tokens": round_completion}
                    reply_parts.append(text)

                    if not (offer_tools and calls):
                        if text.strip():
                            rows.append(TurnRow("assistant", text))
                        round_open = False
                        break

                    ids = [f"call_{round_number}_{index}" for index in range(len(calls))]
                    call_dicts = [
                        {"function": {"name": c.name, "arguments": c.arguments}}
                        | ({"extra_content": c.extra} if c.extra else {})
                        for c in calls
                    ]
                    # The reasoning that led to the calls goes back with them (DeepSeek refuses them without it)
                    thinking = "".join(thinking_parts)
                    assistant = {"role": "assistant", "content": text, "tool_calls": call_dicts}
                    if thinking:
                        assistant["thinking"] = thinking
                    messages.append(assistant)
                    rows.append(TurnRow("assistant", text, call_dicts, thinking=thinking))
                    round_open = False

                    results: dict[str, str] = {}
                    remote = []
                    for call_id, call in zip(ids, calls):
                        tools_used.append(call.name)
                        if call.name in client_tools:
                            remote.append((call_id, call))
                        else:
                            results[call_id] = await self.toolbox.arun(call.name, context, call.arguments)
                            yield {
                                "type": "tool",
                                "name": call.name,
                                "arguments": call.arguments,
                                "result": _shorten(results[call_id], TOOL_EVENT_RESULT),
                            }
                            for extra in context.events:  # what the tool asks the client to show (a QCM)
                                yield extra
                            context.events.clear()
                    if remote:
                        self._pending[turn_id] = _Pending(
                            owner, frozenset(call_id for call_id, _ in remote),
                            asyncio.get_running_loop().create_future(),
                        )
                        try:
                            yield {
                                "type": "tool_requests",
                                "turn": turn_id,
                                "calls": [
                                    {"id": call_id, "name": call.name, "arguments": call.arguments}
                                    for call_id, call in remote
                                ],
                            }
                            results.update(await self._wait_for_client(turn_id))
                        finally:
                            self._pending.pop(turn_id, None)
                    for call_id, call in zip(ids, calls):
                        messages.append({"role": "tool", "tool_name": call.name, "content": results[call_id]})
                        rows.append(TurnRow("tool", results[call_id], tool_name=call.name))
                finished = True
            except BaseException as error:
                if not isinstance(error, (GeneratorExit, asyncio.CancelledError)):
                    reason = str(error) or type(error).__name__
                raise
            finally:
                if not finished and not ephemeral and request.mode == "answer":
                    # What was done stays known: the files a client tool changed are changed for good
                    self._keep_interrupted(request, person, rows, "".join(text_parts) if round_open else "", reason)

            reply = "".join(reply_parts).strip()
            declined = request.mode == "maybe" and passed(reply)
            if declined:  # nothing to add: the message is kept as context, without the model's work
                reply, rows = "", []
                self.memory.add_turn(conversation, person.id, request.message, [], request.prefix, request.project)
            elif not ephemeral and (reply or rows):  # an empty answer would only pollute the history
                self.memory.add_turn(conversation, person.id, request.message, rows, request.prefix, request.project)
                self.memory.set_context_tokens(conversation, context_tokens)
                if self.compact_percent and 100 * context_tokens / window >= self.compact_percent:
                    try:
                        before, after = await self._compact_locked(conversation, chosen=chosen)
                        context_tokens = self.memory.state(conversation).context_tokens
                        yield {"type": "compacted", "before": before, "after": after}
                    except Exception as error:
                        log.warning("automatic compaction of %s failed: %s", conversation, error)
                        yield {"type": "warning", "message": f"Could not compact the conversation: {error}"}

        yield {
            "type": "done",
            "reply": reply,
            "conversation": conversation,
            "person": {"id": person.id, "name": person.name},
            "tools": tools_used,
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            "context": self._context_info(context_tokens, window),
            **self._model_fields(chosen),  # the model may change between turns (/provider, a person's choice)
            "credits": credits,  # what this turn cost: its tokens times the weight of the model
            "passed": declined,  # a "maybe" message Clara chose not to answer (reply is "")
            "quota": self.limits.quota(person.id).describe() if self.limits is not None else None,
        }

    def _model_fields(self, chosen: Chosen) -> dict:
        """What a `done` event says of the model: its name, its provider, and (with a catalogue) its reference
        `provider:model` and what a token of it costs."""
        if chosen.ref is None:
            return {"model": getattr(self.backend, "model", ""), "provider": getattr(self.backend, "active", "")}
        provider, _, model = chosen.ref.partition(":")
        return {"model": model, "provider": provider, "model_ref": chosen.ref, "weight": chosen.weight}

    def _count_usage(
        self, person: Person, reported: int, messages: list[dict], tools: list[dict] | None, text_parts: list[str],
        weight: float = 1.0,
    ) -> int:
        """Count what a round cost the person, in credits; returns them."""
        if self.limits is None:
            return 0
        tokens = reported or estimate_prompt_tokens(messages, tools) + estimate_tokens("".join(text_parts))
        try:
            return self.limits.record(person.id, tokens, weight)
        except Exception:
            log.exception("could not count %d tokens for person %s", tokens, person.id)
            return 0

    def _keep_interrupted(
        self, request: ChatRequest, person: Person, rows: list[TurnRow], partial: str, reason: str
    ) -> None:
        """Store a turn that did not finish (the client went away, the model failed...): the question, what
        was done (tool calls left without a result get a note) and what was written, then why it stopped.
        Nothing is stored when nothing happened."""
        if not rows and not partial.strip():
            return
        rows = list(rows)
        last = next((i for i in range(len(rows) - 1, -1, -1) if rows[i].role == "assistant"), None)
        if last is not None and rows[last].tool_calls:
            answered = sum(1 for row in rows[last + 1 :] if row.role == "tool")
            for call in rows[last].tool_calls[answered:]:
                rows.append(TurnRow("tool", NOT_RUN, tool_name=call["function"]["name"]))
        note = f"[This answer was interrupted: {reason}.]"
        rows.append(TurnRow("assistant", f"{partial.strip()}\n\n{note}" if partial.strip() else note))
        try:
            self.memory.add_turn(
                request.conversation_id, person.id, request.message, rows, request.prefix, request.project
            )
        except Exception:
            log.exception("could not store the interrupted turn of %s", request.conversation_id)

    async def _observe(self, request: ChatRequest, person: Person, chosen: Chosen) -> AsyncIterator[dict]:
        """A message that was not for Clara: stored in the conversation, so that she knows what was said."""
        conversation = request.conversation_id
        yield {"type": "turn", "id": uuid.uuid4().hex}
        async with self._conversation_lock(conversation):
            self.memory.add_turn(conversation, person.id, request.message, [], request.prefix)
        yield {
            "type": "done",
            "reply": "",
            "conversation": conversation,
            "person": {"id": person.id, "name": person.name},
            "tools": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "context": self._context_info(self.memory.state(conversation).context_tokens, chosen.window),
            **self._model_fields(chosen),
            "observed": True,
        }

    async def _model(
        self, messages: list[dict], tools: list[dict] | None, ref: str | None = None
    ) -> AsyncIterator[LlmChunk]:
        """One model round (of the model `ref`, `provider:model`; by default the backend's own). A task reads
        the model and holds a slot while it works, and passes the chunks on through a queue: a client that
        reads slowly (or not at all) cannot keep a slot busy, and a model that hangs times out."""
        queue: asyncio.Queue = asyncio.Queue()

        async def produce() -> None:
            try:
                async with self._llm_slots:
                    stream = (
                        self.backend.stream(messages, tools)
                        if ref is None
                        else self.backend.stream_ref(ref, messages, tools)
                    )
                    async for chunk in with_idle_timeout(stream, self.first_token_timeout, self.idle_timeout):
                        queue.put_nowait(chunk)
                queue.put_nowait(_END)
            except Exception as error:
                queue.put_nowait(error)

        producer = asyncio.ensure_future(produce())
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            producer.cancel()  # the consumer left (or failed): stop reading, free the slot
            with contextlib.suppress(BaseException):
                await producer

    async def _fit(
        self, request: ChatRequest, person: Person, messages: list[dict], schemas: list[dict] | None, compact: bool,
        chosen: Chosen,
    ) -> AsyncIterator[dict]:
        """Make the prompt fit in the window before it is sent, editing `messages` in place:
        summarise the older turns (`compact`, only possible before this turn's own messages exist),
        else leave the oldest replayed turns out; if it still does not fit, PromptTooLarge."""
        conversation = request.conversation_id
        window = chosen.window or self.window
        limit = int(PROMPT_LIMIT * window)
        size = estimate_prompt_tokens(messages, schemas)
        if size <= limit:
            return
        if compact and not request.ephemeral:
            for keep in dict.fromkeys((self.keep_recent_turns, 0)):  # the recent turns first, then all
                try:
                    before, after = await self._compact_locked(conversation, keep_recent_turns=keep, chosen=chosen)
                except NothingToCompact:
                    break
                except Exception as error:
                    log.warning("compaction of %s to fit the context failed: %s", conversation, error)
                    yield {"type": "warning", "message": f"Could not compact the conversation: {error}"}
                    break
                yield {"type": "compacted", "before": before, "after": after}
                messages[:] = self._build_messages(request, person, self.memory.state(conversation), window)
                size = estimate_prompt_tokens(messages, schemas)
                if size <= limit:
                    return
        dropped = False
        while size > limit and self._drop_oldest_turn(messages):
            dropped = True
            size = estimate_prompt_tokens(messages, schemas)
        if dropped:
            yield {"type": "warning", "message": "Older messages were left out of the prompt to fit the context."}
        omitted = False
        while size > limit and self._omit_oldest_output(messages):
            omitted = True
            size = estimate_prompt_tokens(messages, schemas)
        if omitted:
            yield {"type": "warning", "message": "Older tool outputs of this answer were left out to fit the context."}
        if size > limit:
            raise PromptTooLarge(
                f"The prompt needs about {size:,} tokens but the model's context window is {window:,}: "
                "a message, a tool result or the instructions are too large."
            )

    @staticmethod
    def _drop_oldest_turn(messages: list[dict]) -> bool:
        """Remove the oldest replayed turn (the system prompt and the newest user message stay)."""
        newest = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
        first = 1 if messages and messages[0]["role"] == "system" else 0
        if newest <= first:
            return False  # no history left
        end = next((i for i in range(first + 1, newest) if messages[i]["role"] == "user"), newest)
        del messages[first:end]
        return True

    @staticmethod
    def _omit_oldest_output(messages: list[dict]) -> bool:
        """Replace the oldest tool output of the turn in progress by a note; the outputs of its last round
        (what the model asked for just now) stay. False when there is none left to omit."""
        newest_user = max((i for i, m in enumerate(messages) if m["role"] == "user"), default=-1)
        last_call = max((i for i, m in enumerate(messages) if m["role"] == "assistant"), default=-1)
        for index in range(newest_user + 1, last_call):
            message = messages[index]
            if message["role"] == "tool" and message["content"] != OMITTED_IN_TURN:
                messages[index] = {**message, "content": OMITTED_IN_TURN}
                return True
        return False

    def _history_overflows(self, conversation: str, state: ConversationState) -> bool:
        """Are there more turns waiting than the prompt takes back (`history_turns`)?"""
        return bool(self.compact_percent) and (
            self.memory.turns_after(conversation, state.upto_id) > self.history_turns
        )

    def _context_info(self, tokens: int, window: int | None = None) -> dict:
        window = window or self.window
        return {"tokens": tokens, "window": window, "percent": round(100 * tokens / window, 1)}

    # ------------------------------------------------------------------
    # Compaction
    # ------------------------------------------------------------------
    def context(self, conversation: str) -> dict:
        """Size of a conversation's context, and its summary if it has one."""
        state = self.memory.state(conversation)
        return {
            "conversation": conversation,
            "summary": state.summary,
            "messages": len(self.memory.messages_after(conversation, state.upto_id)),
            **self._context_info(state.context_tokens, self._chosen_for(conversation).window),
        }

    async def compact(self, conversation: str, focus: str = "") -> tuple[float, float]:
        """Replace the older messages of a conversation by a summary.

        The last `keep_recent_turns` turns stay as they are. Returns the context usage in
        percent before and after. NothingToCompact if there is nothing to summarise.
        """
        if not self.accepting:
            raise ServerStopping("The server is stopping and takes no new request.")
        self._compactions += 1
        try:
            async with self._conversation_lock(conversation):
                return await self._compact_locked(conversation, focus, chosen=self._chosen_for(conversation))
        finally:
            self._compactions -= 1

    async def title(self, conversation: str) -> str:
        """Clara's title for a listed conversation nobody has titled yet, written from its first question and
        answer (or its summary, when they were purged), and kept. The title it has if it has one."""
        info = self.memory.conversation_info(conversation)
        if info is None:
            raise NothingToTitle("No such conversation")
        if info.title:
            return info.title
        if not self.accepting:
            raise ServerStopping("The server is stopping and takes no new request.")
        shown, _ = self.memory.transcript(conversation, limit=1_000_000)
        question = next((m.content for m in shown if m.role == "user"), "")
        answer = next((m.content for m in shown if m.role == "assistant"), "")
        if question:
            excerpt = f"Question: {question[:TITLE_EXCERPT]}"
            excerpt += f"\n\nAnswer: {answer[:TITLE_EXCERPT]}" if answer else ""
        else:
            excerpt = self.memory.state(conversation).summary[: 2 * TITLE_EXCERPT]
        if not excerpt.strip():
            raise NothingToTitle("The conversation has nothing to title yet")
        messages = [{"role": "system", "content": TITLE_INSTRUCTIONS}, {"role": "user", "content": excerpt}]
        self._compactions += 1  # keeps the server from stopping under it, like a compaction
        try:
            parts: list[str] = []
            async with contextlib.aclosing(self._model(messages, None)) as model:
                async for chunk in model:
                    parts.append(chunk.text)
        finally:
            self._compactions -= 1
        title = clean_title("".join(parts))
        if not title:
            raise RuntimeError("the model returned an empty title")
        return self.memory.title_if_untitled(conversation, title)

    async def _summarise(self, transcript: str, previous: str, focus: str, ref: str | None = None) -> str:
        parts: list[str] = []
        async with contextlib.aclosing(self._model(summary_request(transcript, previous, focus), None, ref)) as model:
            async for chunk in model:
                parts.append(chunk.text)
        summary = "".join(parts).strip()
        if not summary:
            raise RuntimeError("the model returned an empty summary")
        return summary

    @staticmethod
    def _split_recent(rows: list[StoredMessage], keep_turns: int) -> tuple[list[StoredMessage], list[StoredMessage]]:
        """(rows to summarise, rows kept as they are): the kept ones start at the user message
        `keep_turns` from the end. With fewer turns than that, everything is summarised."""
        user_rows = [index for index, row in enumerate(rows) if row.role == "user"]
        if keep_turns <= 0 or len(user_rows) <= keep_turns:
            return rows, []
        boundary = user_rows[-keep_turns]
        return rows[:boundary], rows[boundary:]

    async def _compact_locked(
        self, conversation: str, focus: str = "", keep_recent_turns: int | None = None, chosen: Chosen | None = None
    ) -> tuple[float, float]:
        chosen = chosen or Chosen(None)
        window = chosen.window or self.window
        keep = self.keep_recent_turns if keep_recent_turns is None else keep_recent_turns
        state = self.memory.state(conversation)
        rows = self.memory.messages_after(conversation, state.upto_id)
        if not rows:
            raise NothingToCompact("the conversation is empty: nothing to compact")
        old, kept = self._split_recent(rows, keep)

        # Summarise chunk by chunk. Each step is saved, so a failure halfway leaves a coherent
        # conversation (the summary so far, then the messages not yet summarised).
        summary = state.summary
        for chunk in chunk_messages(old, self._transcript_chars or transcript_budget(window)):
            transcript = build_transcript(chunk)
            if transcript:
                summary = await self._summarise(transcript, summary, focus, chosen.ref)
            self.memory.set_summary(conversation, summary, chunk[-1].id, state.context_tokens)

        # What remains in the context: the fixed part (system prompt, tools), the summary, and the
        # messages kept. The fixed part is what the last measure had beyond summary and messages.
        measured = estimate_tokens(state.summary) + sum(estimate_message_tokens(row) for row in rows)
        fixed = max(0, state.context_tokens - measured)
        after_tokens = fixed + estimate_tokens(summary) + sum(estimate_message_tokens(row) for row in kept)
        self.memory.set_summary(conversation, summary, old[-1].id, after_tokens)
        if self.purge_summarised:
            self.memory.purge_summarised(conversation, old[-1].id)
        before, after = 100 * state.context_tokens / window, 100 * after_tokens / window
        self._compaction_done(conversation, before, after)
        return before, after


@dataclass
class AgentStats:
    """Counters since the server started (shown by the console's /status)."""

    turns: int = 0
    active: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
