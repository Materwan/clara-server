# Clara server

One AI, one memory, many clients. Clara runs as a small HTTP server and is the
**only** process that touches the memory. A terminal, a Discord bot or any app
are thin clients: they send a message, they show the answer.

```
 terminal (clara-chat) ─┐                     ┌─ Ollama (local or ollama.com)
 Discord adapter ───────┼─►  Clara server  ─►─┤
 your other app ────────┘     │               └─ Gemini, DeepSeek, Mistral
                          SQLite memory
```

Runs the same on Linux and Windows (pure Python, SQLite, no native extension).

It also serves a **web site** (open the server's address in a browser): sign in with a user name and a password,
chat, keep **projects** (files, folders and GitHub repositories Clara uses in their conversations), see what Clara
remembers, and, for administrators, manage users and the server. See *Users*, *Projects* and *The web site*.

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev,discord]"   # Windows: .venv\Scripts\pip; "discord": the Discord bot (optional)
cp .env.example .env                    # then put real tokens in CLARA_TOKENS (the server
                                        # refuses the "change-me" placeholders)
.venv/bin/clara-server                  # listens on 127.0.0.1:8765
```

In another terminal:

```bash
export CLARA_TOKEN=<one of the tokens>
.venv/bin/clara-chat --user erwan --name Erwan
```

Tests: `pytest`. Lint: `ruff check .` (both run in CI on Linux and Windows, Python 3.11 and 3.12).

## The console

Started in a terminal, `clara-server` shows a prompt beside the server logs
(history, Tab completion). Without a terminal (systemd) there is no prompt.

Two options change how it starts (they exclude each other):

| Option | |
| --- | --- |
| `--headless` (alias `--no-console`) | No prompt, and **the server survives the end of its terminal**: closing an SSH session no longer kills it. It ignores SIGHUP, lets go of the terminal's input and output, and logs to `data/logs/clara-server.log` (5 files of 5 MB). Start it with `clara-server --headless &` (or `nohup`, tmux, systemd: it works the same), stop it with `kill <pid>` (SIGTERM stops it the careful way, see *Stopping the server*) or `clara-admin /stop`. Windows has no SIGHUP: there it only drops the console and logs to the file. |
| `--test` | Before the prompt appears, checks the installation and shows the state of each check (`PASS`, `WARN`, `FAIL`, `SKIP`): configuration, data directory writable, database sound, system prompt, web site files, port free, model provider reachable and model installed, Tailscale. Nothing is changed. When nothing failed it goes on to start normally; when a check failed it asks `Start anyway? [y/N]` (and does not start when there is no terminal to ask on, exit code 1). |

To use the same console **from another computer**, set `CLARA_ADMIN_TOKENS` on
the server and run:

```bash
clara-admin --url http://my-server:8765 --token <admin token>   # or CLARA_URL / CLARA_ADMIN_TOKEN
clara-admin /status                                              # one command, then exit
```

Both consoles run the same commands (the `/` is optional):

| Command | |
| --- | --- |
| `/provider [local\|cloud\|gemini\|deepseek\|mistral]` | show the providers, or switch where the model runs |
| `/model [name]` | list the active provider's models, or change its model (the server's own, see *Models*) |
| `/models [list\|refresh\|enable\|disable\|weight\|discord …]` | the models users may choose, what a token of each costs, and Discord's model (see *Models*) |
| `/status` | provider, model, uptime, running turns, tokens, memory size |
| `/people` | everybody Clara knows, with accounts and fact counts |
| `/facts <person> [add <text>\|del <id>]` | read or edit what Clara knows (person = id, `surface:user` or name) |
| `/forget-person <person> [confirm]` | erase a person: accounts, facts and what they said (without `confirm`, only shows what would go) |
| `/link <surface:user> <person>` | make an account belong to a person (merges them) |
| `/remember <person> <text>`, `/forget <person> <fact id>` | the same as `/facts <person> add` / `del` |
| `/relation [<person> [<0-100>\|+n\|-n\|reset]]` | Clara's relationship with each person (see *Relationship*) |
| `/chime [default on\|off \| <space> on\|off\|default]` | where Clara may answer Discord messages that are not for her (see *Discord*) |
| `/discord [status\|start\|stop\|restart]` | the Discord bot built into the server (see *Discord*) |
| `/stop [now]` | stop the server: tell every client, refuse new questions, wait for running replies and agents, exit (`now`: do not wait); works from `clara-admin` too |
| `/restart [now]` | pull, update, stop the careful way and start again (see *Restarting the server*); works from `clara-admin` too |
| `/help [command]`, `/quit` | `/quit` stops the server like `/stop` from the embedded console, and only closes a remote one |

### Providers

| id | Name | What it is |
| --- | --- | --- |
| `local` | Local host | Ollama on this machine, or any host in `OLLAMA_HOST`. Model: `CLARA_LOCAL_MODEL` |
| `cloud` | Ollama API key | `ollama.com` with `OLLAMA_API_KEY`. Model: `CLARA_CLOUD_MODEL` |
| `gemini` (or `google`) | Google Gemini | `GEMINI_API_KEY` (aistudio.google.com). Model: `CLARA_GEMINI_MODEL`, default `gemini-flash-latest`, 1M tokens |
| `deepseek` | DeepSeek | `DEEPSEEK_API_KEY` (platform.deepseek.com). Model: `CLARA_DEEPSEEK_MODEL`, default `deepseek-flash`, 1M tokens |
| `mistral` | Mistral | `MISTRAL_API_KEY` (console.mistral.ai). Model: `CLARA_MISTRAL_MODEL`, default `mistral-large-latest`, 128k tokens |

Gemini, DeepSeek and Mistral are reached through their OpenAI-compatible chat API (streaming and tool calls), with
no extra package. A provider whose key is not set is listed but cannot be chosen. Each one's address, model and
context window can be changed (`CLARA_<ID>_HOST`, `CLARA_<ID>_MODEL`, `CLARA_<ID>_CONTEXT_WINDOW`); `/model` lists
what the key gives access to. Two things they need back with a tool call are kept with it in the history: the
model's reasoning (DeepSeek refuses a tool call sent back without it) and Gemini's *thought signature*; a call made
by another provider is sent to Gemini with the stand-in value Google documents. DeepSeek thinks before it answers
(shown like the thinking of Ollama models); `CLARA_DEEPSEEK_THINKING=false` turns it off. The web tools
(`web_search`, `web_fetch`) still need `OLLAMA_API_KEY`, whatever the provider.

Models whose name ends in `-cloud` are not local: a local Ollama forwards them to ollama.com (they need
`ollama signin`), and the window Clara requests from a local server (`num_ctx`) cannot be relied on for them,
so set `CLARA_LOCAL_CONTEXT_WINDOW` to the window such a model really has.

`/provider cloud` switches every client at once, without a restart, then checks
that the provider answers (and, for those with a key, that the key is accepted).
The choice and the models picked with `/model` are saved in `data/runtime.json`
and survive restarts. The API keys only ever live in the environment: they are not
saved, printed, or reachable through the API.

Remote admin tokens are separate from chat tokens: a Discord adapter cannot
switch the provider or edit memory, and an admin token cannot chat.

## How the memory works

| What | Follows | Example |
| --- | --- | --- |
| **Facts** ("likes jazz") | the *person*, on every surface | told in the terminal, known on Discord |
| **History** (last messages) | the *conversation* | a Discord channel and a terminal session are separate threads |

A client identifies the speaker as `surface` + `user_id` (`cli`/`erwan`,
`discord`/`1234`). Each pair is an *account*; accounts are tied to a *person*.
Two accounts become one person in two steps, so nobody can claim someone else's account: the
account to attach asks its own client for a code (`POST /v1/accounts/link-code`, or `/linkcode` in
`clara-chat`; valid 10 minutes, usable once), then the client of the person it joins sends it with
`POST /v1/accounts/link` (or `/link discord 1234 <code>`). Facts and history are merged, but only
if at most one of the two already has memories: merging two filled accounts cannot be undone, so
an operator does it from the server console (`/link`).

With an Ollama API key (`OLLAMA_API_KEY`), the model can also search the web and read pages: `web_search` and
`web_fetch` use ollama.com's web API (results cut to a few thousand tokens). `CLARA_WEB_TOOLS=false` turns them off.

With `MUSIC_ASSISTANT_URL`, `MUSIC_ASSISTANT_PLAYER` and `MUSIC_ASSISTANT_TOKEN` set (see `.env.example`), the model can
also search a Music Assistant library and play, queue or stop on one of its players: `music_search`, `music_play`,
`music_queue`, `music_stop` and `music_now_playing`. Those five only ever control the player of `MUSIC_ASSISTANT_PLAYER`,
and they run without asking.

The model saves and removes facts itself through three tools, `remember`, `forget` and
`recall_facts`, which can only touch the person who is talking. The prompt shows the newest facts that fit
`CLARA_FACTS_TOKEN_BUDGET` tokens (2000) and says how many older ones are left out; `recall_facts` searches
those by words (any case, any accent: "Élan" and "élan" are one fact). Duplicates are detected on the folded
text (Unicode NFKC + case folding + spaces), and databases from earlier versions are migrated at startup.

## API

All routes except `/health` need `Authorization: Bearer <token>`.

| Route | |
| --- | --- |
| `POST /v1/chat` | `{surface, user_id, user_name?, message, conversation?, quiet?, space?, roster?, focus?, mode?}` → `{reply, conversation, person, tools, usage, passed}`; 413 if it cannot fit the model's window, 504 if the model hangs, 503 if the server is stopping. `quiet`: never notify the person about this turn; the group fields are described in *Discord* |
| `POST /v1/chat/stream` | same body; Server-Sent Events `turn` / `thinking` / `token` / `tool_start` / `tool` / `qcm` / `tool_requests` / `usage` / `compacted` / `warning` / `done` / `error` |
| `POST /v1/turns/{id}/tool-results` | `{results: [{id, content}]}`: a client's answer to a `tool_requests` event (see below) |
| `GET /v1/conversations?surface=&user_id=&q=&limit=` | `{conversations: [{id, title, titled_by, pinned, created_at, updated_at, preview}]}`: the conversations the account's person started on that surface, pinned first, then the last written in; `q` keeps those whose title, messages or summary contain it (see *Conversation history*) |
| `GET /v1/conversations/{id}/messages?surface=&user_id=&limit=&calls=` | the same fields, and `{messages: [{id, role, content, created_at}], summary, earlier}`: its last questions and answers (200), to show it again. With `calls=true` an answer also has `calls: [{name, arguments}]`, the tools it called (arguments cut to 300 characters), and one that only called tools is given with an empty `content` |
| `PATCH /v1/conversations/{id}` | `{surface, user_id, title?, pinned?}`: rename (`""`: no title) and/or pin |
| `POST /v1/conversations/{id}/title` | `{surface, user_id}` → `{id, title}`: Clara writes its title if it has none; 409 nothing to title yet, 502 the model failed |
| `GET /v1/conversations/{id}` | `{tokens, window, percent, summary, messages}`: how full the context is |
| `POST /v1/conversations/{id}/compact` | `{focus?}` → `{before_percent, after_percent, summary}`: summarise the older messages |
| `GET /v1/memory/facts?surface=&user_id=` | list a person's facts |
| `POST /v1/memory/facts` | `{surface, user_id, text}` |
| `DELETE /v1/memory/facts/{id}?surface=&user_id=` | |
| `POST /v1/reminders` | `{surface, user_id, user_name?, text, at, repeat?, timezone?, conversation?, targets?}` → `{id, text, due_at, repeat, targets}`; 422 if `at` is past or not ISO 8601 (see *Reminders*); `conversation` (default: the account's own) is where Clara writes the announcement; `targets`: the surfaces it is shown on (default: all of the person's) |
| `GET /v1/reminders?surface=&user_id=` | the person's reminders that have not fired yet |
| `DELETE /v1/reminders/{id}?surface=&user_id=` | cancel one of the person's own reminders |
| `POST /v1/tasks` | `{surface, user_id, user_name?, title, description?, due?, reminders?, timezone?, conversation?, targets?, parent_id?}` → the task: add one to the person's to-do list (see *Tasks*); `due` and `reminders` are ISO 8601 (a time without offset is read in `timezone`); **without `reminders` Clara picks them**; 422 for a past time or a bad field |
| `GET /v1/tasks?surface=&user_id=&status=` | the person's tasks (`status`: `open` by default, `done` or `all`): each with `reminders_sent`, `next_reminder`, `reminders` (all those to come), `max_reminders`, `parent_id`, `subtasks` (`{total, done}`) and `due_limit` (see *Sub tasks*); `GET /v1/tasks/{id}` one task |
| `PATCH /v1/tasks/{id}` | `{surface, user_id, title?, description?, due?, reminders?, targets?, status?, parent_id?, before_id?}`: only what is given changes; `parent_id` makes it a sub task of that task (`null`: a main task again) and `before_id` puts it before that task among those with the same parent (`null`: last), see *Sub tasks*; `due: null` removes the deadline, `reminders` replaces those to come (`[]`: stop reminding), `status` `done` or `open` closes or reopens it; `DELETE /v1/tasks/{id}?surface=&user_id=` deletes it |
| `POST /v1/notifications` | `{surface, user_id, user_name?, text, title?, targets?, conversation?}` → `{id, sent_at, targets}`: notify that person now (see *Notifications*); 429 when too many |
| `GET /v1/settings?surface=&user_id=` | the person's settings: `{notify_after, notify_after_default, notify_after_effective}`, the seconds a task takes before it notifies them when done (see *Notifications*); `notify_after` is `null` while they have not set one |
| `PATCH /v1/settings` | `{surface, user_id, user_name?, notify_after}`: `notify_after` is 0 (never) to 604800 seconds, or `null` for the server's default; 422 out of range |
| `GET /v1/notifications/stream?surface=&user_id=` | Server-Sent Events of that account: its person's `reminder` and `notification` events for that surface, and `server` (running, stopping, stopped). Also served as `/v1/reminders/stream`. With `?surface=discord&all=true` (a client of the whole surface): the events of every account of that surface, each with `accounts` |
| `POST /v1/accounts/register`, `POST /v1/accounts/login` | `{surface, user_id, user_name?, username, password}`: a client signs one of its accounts in (see *Discord*); 401 wrong password, 409 name taken or already signed in, 422 rules, 429 too many |
| `POST /v1/accounts/logout`, `GET /v1/accounts/me?surface=&user_id=`, `GET /v1/accounts/signed-in?surface=` | sign it out; who it is signed in as, with its facts and relationship; every signed-in account of a surface |
| `PUT /v1/spaces` | `{surface, spaces: [{id, name}]}`: the group spaces (Discord servers) the client is in now |
| `GET /v1/admin/spaces`, `PATCH /v1/admin/spaces` `{default_chime}`, `PATCH /v1/admin/spaces/{id}` `{chime?, instructions?}`, `PATCH /v1/admin/people/{id}` `{relation}` | chime in, and the relationship (administrators) |
| `POST /v1/accounts/link-code` | `{surface, user_id}` → `{code, expires_in}`: proof of control of that account |
| `POST /v1/accounts/link` | `{surface, user_id, code, to_surface, to_user_id}`; 403 bad code, 409 both accounts have memories |
| `DELETE /v1/conversations/{id}?surface=&user_id=` | forget a thread, keep the facts; with an account, only one its person started (404 otherwise) |
| `POST /v1/auth/login` | `{username, password, surface?, device?}` → `{token, user, surface}`; with the header `X-Clara-Web: 1` (the web site) the token goes in an HttpOnly cookie instead; 401 wrong, 403 surface not allowed, 409 accounts cannot be merged, 429 too many tries (see *Users*) |
| `GET /v1/auth/signup`, `POST /v1/auth/register` | `{open, privacy}`: whether the web site lets people make their own user, and what it does with their words; `{username, password}` makes a user (never an administrator) and logs them in on `web`, as login does. 403 when `CLARA_WEB_SIGNUP` is off, 409 name taken, 422 rules, 429 too many (see *Users*) |
| `POST /v1/auth/logout`, `GET /v1/auth/me`, `POST /v1/auth/password` `{current_password, new_password}`, `GET /v1/auth/sessions`, `DELETE /v1/auth/sessions/{id}` | the signed-in user's own account and devices |
| `GET /v1/auth/export`, `POST /v1/auth/delete-account` `{password}` | download everything the server keeps about me; erase my user, my memories and my lines of the traffic log |
| `POST /v1/documents/extract` | the bytes of a PDF as the body → `{text, pages, truncated}` |
| `GET /health` | no auth; shows the active provider and model, and `restarted`: the id of the restart that started this server (see *Restarting the server*) |
| `GET /v1/admin/commands`, `POST /v1/admin/command` | `{line}` → `{output, quit}`; an **admin token** or an administrator user (used by `clara-admin`) |
| `GET/POST /v1/admin/users`, `PATCH/DELETE /v1/admin/users/{name}`, `POST .../sign-out`, `GET /v1/admin/status`, `GET /v1/admin/models`, `GET /v1/admin/people`, `GET/POST .../people/{id}/facts`, `DELETE .../facts/{fact}`, `GET .../people/{id}/footprint` | what the web site's administration page uses; same rights as above |

`conversation` defaults to `<surface>:<user_id>` (a private thread). A group
client such as a Discord channel should pass its own id (`discord:channel:42`);
messages from other people in that thread reach the model prefixed with their name.

Interactive docs: `http://127.0.0.1:8765/docs`.

### Conversation history

The server keeps a list of conversations, so that a client can show them as other chat apps do (the desktop app
has it at the side of its window). A conversation joins the list with its first turn: who started it (the
person), the surface it was started on (the start of its id, `app` in `app:erwan:4f2a…`), and when it was last
written in. Conversations from before this list existed are not in it; their messages stay where they were.

- **Listing** is per person and surface: `GET /v1/conversations?surface=app&user_id=erwan` gives what that
  account's person started in the app, not their terminal or Discord conversations. The **web site and the app
  share one list**: asked on either of them, it holds the conversations started on both (`web:erwan:…` and
  `app:erwan:…`), and either can read, continue, rename, pin, move or delete any of them (a client token limited by
  `CLARA_CLIENT_SURFACES` sees only the surfaces it may use). Projects are the person's, so they were already
  the same. Each comes with the start of its first message (`preview`), for those without a title yet.
- **Reading one back** (`/messages`) gives its questions and answers, not the tool calls and their results,
  except the QCM Clara asked (see *QCM*). When older messages were deleted after a compaction (`CLARA_PURGE_SUMMARISED`), the summary that stands for
  them comes with it.
- **Titles**: a client asks for one after an answer (`POST .../title`). Clara writes 3 to 6 words from the
  first question and answer (or from the summary), in a separate model call that is not stored in the
  conversation. A title given by the person (`PATCH`) is never replaced; an empty one lets Clara title it again.
- **Search** (`q`) looks in titles, the questions and answers, and summaries; case is ignored for ASCII
  letters only (SQLite).
- **Only its person** can list, read, rename, pin or title a conversation, or delete it when the request
  names an account. `CLARA_CLIENT_SURFACES` applies as everywhere.

### Tools that run on the client

A client can give Clara tools of its own: files, a shell, a calendar... whatever lives on *its*
machine. The request of `/v1/chat/stream` takes more fields:

| Field | |
| --- | --- |
| `tools` | tools the client runs itself, as function schemas (`{"type": "function", "function": {"name", "description", "parameters"}}`). Names must not collide with the server's (`remember`, `forget`, `web_search`...) |
| `instructions` | text added to the system prompt (what this client is for, how to use its tools) |
| `prefix` | text shown to the model before the message, kept in the history but left out of summaries (e.g. the date) |
| `ephemeral` | a one-shot job: no Clara persona, no memory, no stored history, no server tools; the system prompt is just `instructions`. Used for sub-agents |
| `timezone` | IANA name (`Europe/Paris`) for the date and time the model is told; default: the server's own |

When the model calls one of the client's tools, the stream sends

```
event: tool_requests
data: {"type": "tool_requests", "turn": "<id>", "calls": [{"id": "call_0_0", "name": "read_file", "arguments": {...}}]}
```

and waits. The client runs the tools, then posts `{"results": [{"id": "call_0_0", "content": "..."}]}` (one entry
per call, no more, no fewer) to `/v1/turns/<id>/tool-results`; the same stream goes on with the next model round. The
connection stays open meanwhile (a `: keepalive` comment is sent every 15 s). Only the client that opened
the turn can answer it. Closing the stream gives the turn up; a client silent for `CLARA_TOOL_TIMEOUT` seconds
ends it with an `error` event. A model slot is held only while the model works, never while a client does.
`/v1/chat` (no stream) cannot carry client tools.

The tool calls and their results are stored with the conversation, so the model remembers what it did. When the
history is replayed, only the newest outputs are given in full: at most 8, and only while they fit in a quarter of
the window (the newest one always); the older ones are replaced by a short note.

A turn that does not finish (the client closes the stream, its tools time out, the model fails) is stored all the
same as far as it went: the question, the tool calls made (a call left without its result gets
`[not run: the answer was interrupted]`), what the model had written, then `[This answer was interrupted: <why>.]`.
A client tool may have changed files before the turn stopped: the model must know it did. Nothing is stored when
the model had not started.

Other events of the stream: `tool_start` (`{name, arguments}`) says a server tool is about to run and `tool` that it ran
(`{name, arguments, result, truncated}`, the result cut to 8000 characters: `truncated` says so), and `thinking` carries the reasoning of the models that show it apart (it is never stored nor sent
back to the model).

The system prompt only holds the date, and the time of day is added to the newest user message (not
stored), so the prompt and the replayed history stay identical from one turn to the next and Ollama can
reuse its cache of them.

### Privacy

`/forget-person <person> confirm` erases a person: their accounts, facts and messages. A conversation only they
took part in goes entirely, answers, summary and title included. In a conversation shared with other people only their own
messages go, and the answers and summary that remain may still mention them. Messages are kept after a compaction
(the summary stands for them) unless `CLARA_PURGE_SUMMARISED=true`, which deletes them: the summary, which can
contain personal data too, is then the only record. There is no retention limit otherwise.

### Long conversations

The `done` event reports `context: {tokens, window, percent}`. When a conversation fills `CLARA_COMPACT_PERCENT`
of the provider's window, the server asks the model for a summary of the older messages (a `compacted` event says
so) and from then on sends the summary instead of them; the messages stay in the database. `POST
/v1/conversations/{id}/compact` does it on demand, with an optional focus. The last
`CLARA_KEEP_RECENT_TURNS` turns (2) are not summarised: the model goes on from them word for word. A
conversation that is too long for one summary request is summarised in several steps, each continuing
the previous one, so no message is skipped. The same happens when a conversation has more turns than `CLARA_HISTORY_TURNS`: the oldest
are summarised (half of the history is kept) instead of silently falling out of the prompt, as long
as `CLARA_COMPACT_PERCENT` is not 0. The summary is not meant to shrink conversations that are
already short.

A prompt is never left to be truncated silently. Before each model round the server estimates its size
(messages, tool calls and schemas, whatever the model reports): above 95% of the window it summarises the
older turns, then leaves the oldest replayed turns out, then the oldest tool outputs of the answer in progress
(those of its last round stay; each with a `warning` event), and if it still does not fit
the turn ends with an error: HTTP 413 on `/v1/chat`, an `error` event on the stream. A single message
taking more than half of the window is refused at once. The context size reported to clients is the larger of
the model's figure and the estimate, since a prompt cache can make Ollama report only what it evaluated.

### Writing a client

```python
import httpx, json
with httpx.stream("POST", "http://127.0.0.1:8765/v1/chat/stream",
                  headers={"Authorization": "Bearer <token>"},
                  json={"surface": "web", "user_id": "42", "message": "hello"}) as r:
    for line in r.iter_lines():
        if line.startswith("data: "):
            event = json.loads(line[6:])   # {"type": "token", "text": "..."} ...
```

`src/clara/client.py` is a complete example.

## Discord

### The bot

The Discord bot is part of the server (`src/clara/discord_bot/`) and needs the optional `discord` extra:
`pip install -e ".[discord]"`. Set in `.env`:

| Setting | |
| --- | --- |
| `DISCORD_BOT_TOKEN` | the bot's token (Discord developer portal > your application > *Bot*). Like the API key it never leaves `.env`: it is not shown, saved elsewhere, or reachable through the API |
| `AUTO_START_DISCORD_BOT` | `true`: the bot starts with the server (in the background: the server does not wait for Discord). Default `false` |
| `DISCORD_BOT_INVIT_URL` | the invite link shown on the web site; empty: one is built from the bot's id (scopes `bot` and `applications.commands`, the permissions to read and send messages) |

On the *Bot* page of the developer portal, turn on the **Message Content** and **Server Members** intents (without
them Discord refuses the connection, and the web page says so).

An administrator starts and stops it at any time with `/discord start|stop|restart` (`/discord` shows its state), or
on the web site's **Discord** page. That lasts until the server restarts: `AUTO_START_DISCORD_BOT` decides then. When
the server stops, the bot stops with it. The built-in bot reaches Clara by calling the server's own code (no token,
no HTTP), under the client name `discord-bot`, through the same checks as the HTTP routes below.

**On another machine.** The same bot runs on its own with `clara-discord` (or the `bot-discord` project, which starts
it), and reaches the server over HTTP with a client token (`CLARA_TOKENS=discord:<token>`,
`CLARA_CLIENT_SURFACES=discord=discord`; on its side `DISCORD_BOT_TOKEN`, `CLARA_URL`, `CLARA_TOKEN`, optionally
`CLARA_TIMEZONE`). Do not run both with the same Discord token: both would answer.

### What it does

On Discord, people make an account with `/register` (or `/login` to an existing one) before Clara answers them. She
answers private messages, and on a server the messages that mention her or reply to her; she reads the other messages
of signed-in members as context, and may answer them where chime in is allowed. Their slash commands are `/register`,
`/login`, `/logout`, `/me`, `/remember`, `/forget`, `/tasks` (their to-do list, each task with the reminders sent and the
next one), `/task` (one in full), `/reset` and `/help`; they add or change tasks by asking Clara. **Reminders, task
reminders and notifications** reach them as **private messages** only, never in a server's channels.

### What the server provides

What follows is generic, but Discord is what uses it.

**Signing in.** On the surfaces of `CLARA_LOGIN_SURFACES` (`discord` by default; `none` turns it off) an account
must be signed in as a user before it can talk to Clara or touch its memory: the server answers 403 (`... is not
signed in ...`) otherwise. The bot has no token per person: in a private Discord form it asks for a user name and a
password, and sends them once.

- `register` makes a new **user** (the same kind as `/user add`: it signs in on the web site, the app and the
  terminal too, with the same memories) and signs the account in. The account's earlier memories come along. An
  account that makes 3 users within a minute may make no other for an hour.
- `login` signs the account in as an existing user; wrong passwords are limited per account
  (`CLARA_AUTH_MAX_FAILURES`). The account joins the user's person; an account that was another user's before is
  only *moved*, nobody is merged.
- An administrator can also sign an account in, with no password: `/user link <name> <discord:id>`, `/user add <name>
  discord:<id>`, or on the web site (the Discord page, or a user's menu and *Add user* under Administration, with a
  search among the people in the bot's servers). The account replaces whoever it was signed in as and is *moved* if
  it was another user's; otherwise its memories are **merged** into the user's, even when both have some. A new user
  made this way starts as the person Clara already knows from the account. The built-in bot knows at once; a bot that
  runs apart reads the signed-in accounts again within 5 minutes.
- The account stays signed in until `logout`, or until the user is signed out everywhere (`/user logout`, a new
  password), disabled or removed. `/user list` shows the accounts signed in for each user.

**Group spaces.** A Discord server is a *space* (`discord:guild:<id>`); each of its channels is a conversation
(`discord:channel:<id>`), and a private conversation is the person's own (`discord:<id>`). With a `space`, a turn
may carry:

| Field | |
| --- | --- |
| `roster` | `[{user_id, name}]`: the members of the space. Those signed in are listed in the system prompt, and Clara can read what she remembers about any of them with her `about_person` tool (read-only; only offered when someone else is listed). Accounts Clara does not know, or not signed in, are left out |
| `focus` | the ids the message is about (mentioned, replied to): what Clara remembers about them (15 newest facts, 5 people) is in the prompt |
| `mode` | `answer` (default: the message is for Clara), `observe` (it was not: it is stored as context, no model call, `observed: true`) or `maybe` (it was not; Clara answers only if she has something worth adding, else she replies `<pass>`: the message is stored without her work, `passed: true` and `reply: ""`) |

In a group, every message reaches the model prefixed with its author's name. The server turns `maybe` into
`observe` unless **chime in** is allowed in that space: an administrator switches it per space (`/chime`, or the
web site's *Discord* page), with a default for the spaces nobody set (off). The client tells the server which
spaces it is in (`PUT /v1/spaces`, when it starts, and when it joins or leaves one). Mind that with chime in, every
message of a signed-in member costs a model call.

**A personality per server.** An administrator gives a space a list of short instructions (`instructions`: `{text,
enabled}`, at most 30 of 500 characters; the web site's *Discord* page edits them). While at least one is enabled,
they *replace* the personality file for every message of that space (`ChatRequest.space`): the prompt starts with
one line naming Clara and the instructions as bullets, and keeps the rest (what she remembers, the relationship,
the roster, tools). Private messages keep the usual personality; nothing enabled, nothing changes.

**Privacy.** In a space, Clara can read what she remembers about a member while she talks to the others (that is
what `about_person` and `focus` are for); the prompt asks her to be discreet. Registering on Discord means accepting
that. People who are not signed in are not stored at all: the bot does not send their messages.

**Reminders and notifications** reach Discord as private messages: the bot listens to
`/v1/notifications/stream?surface=discord&all=true`, which gives each event of any person with a signed-in Discord
account (meant for `discord`, or for every surface) with the `accounts` to send it to. What is for everybody (a
change of model) is not sent there.

## Relationship

Clara keeps a relationship score with each person, 0 to 100 (none at first). It is in the system prompt on every
surface and sets her tone: from warm complicity (85 and more) to dry and curt, but still helpful, under 25. She
moves it herself with her `adjust_relation` tool when someone is clearly friendly or rude (at most once per answer,
from -25 to +10; the first move starts from 50). Administrators read and set it with `/relation`, or on the web site
(*People & memory*). Merging two people keeps a relationship; `/forget-person` erases it.

## Reminders

A reminder is a text and a moment. When the moment comes, the server announces it **to the person who set
it, and nobody else**: on every client of theirs, or only on the *surfaces* the reminder names (`targets`:
`app`, `cli`, `console`, `discord`...). "Their clients" means every account linked to that person (see
*How the memory works*): a reminder set in the terminal reaches the desktop app only once `app:<you>` and
`cli:<you>` are the same person.

- **Setting one.** `POST /v1/reminders` with `at` as ISO 8601: `2026-10-05T09:00+02:00`, or without an
  offset (`2026-10-05T09:00`), read in `timezone` (an IANA name) or else the server's own timezone.
  `repeat` is `daily`, `weekly` or `monthly`: the same wall-clock time again, and the same day of the
  month, clamped to the month's length (the 31st is the 28th in February, then the 31st again). With a
  `timezone` a repeat follows daylight saving; with only an offset it keeps that offset. `targets` is a
  list of surfaces (empty: all of them). The model can do it too (`remind`, `list_reminders`,
  `cancel_reminder`; it reads `when` in the request's `timezone`) and **chooses the surfaces itself**
  (`targets`; it is warned when the person has no account on one). `clara-chat` and the console have
  `/remind [daily] [@app,discord] <when> <text>`. At most 100 per person, 500 characters each.
- **Clara writes the announcement.** When a reminder comes due, the server has Clara write the message that
  is shown, instead of just the reminder's text: an ordinary turn in the conversation where the reminder was
  set, as the person who set it. She knows what she knows about them, and the exchange (`[Reminder due] …` and
  her answer) is kept in their history. She has no tools in that turn, so a reminder cannot create reminders.
  If she cannot write it within `CLARA_REMINDER_AI_TIMEOUT` seconds (60; the model is down or slow), the
  reminder's own text is announced and **a notification tells the person why**. A reminder set before the
  server kept where is announced as typed. `0` turns this off.
- **Receiving them** is the event stream of *Notifications* below: a reminder arrives as
  `{"type": "reminder", "id", "text", "message", "due_at", "fired_at", "from", "targets"}` (UTC times;
  **show `message`**, which is what Clara wrote, or `text` when `message` is null).
- **The server was down** at the moment: the reminder fires when it is back. A repeating one that missed
  several occurrences fires once, then waits for its next.
- **Privacy.** `/forget-person` also erases the person's reminders and what they announced. Only the person
  who set a reminder can list or cancel it.

## Tasks

Each person has a **to-do list**, the same on every surface: a task has a title, a description, an optional
deadline (`due_at`), a queue of **reminders** still to come (`reminders`, the first being `next_reminder`) and
the number of reminders already sent (`reminders_sent`). A task is changed by command (`/tasks` and `/task` in
`clara-chat` and the console, the Tasks page of the web site, the desktop app's *Tasks…* dialog, `/tasks` and
`/task` on Discord, `/v1/tasks`) or by asking Clara in natural language (her tools `add_task`, `list_tasks`,
`update_task`, `delete_task`: "add a task: send the invoice by Friday", "what is on my list?", "when will you
remind me about the taxes?", "I did the taxes"). Only its person can see or change a task.

- **Every task has reminders, and Clara chooses them when you give none.** A task created without a reminder
  is planned by Clara (a one-shot turn on the model of the surface where it was made, that answers
  `{"reminders": [...]}`): mornings for a chore, a day and an hour before and at a deadline, and so on. When
  she cannot (the model is down or too slow, `CLARA_REMINDER_AI_TIMEOUT`, at most 30 s for this one), the rules
  do: a day before the deadline, an hour before and at it, or tomorrow at 09:00 on the person's clock. Through
  the model's tool she picks them herself, as part of the conversation.
- **At each reminder she looks at the task again.** When a reminder comes due, Clara is given the task's title,
  description, deadline, the number of reminders already sent and those still queued, and answers
  `{"message": "...", "next": [...]}`: the notification to show, and (optional) the reminders to come
  **instead** of the queued ones. She may move them closer to a deadline, space them out when the person keeps
  not doing it, add some, or stop (`[]`). Left out, the queue is kept; when it is empty the rules queue the next
  one (halfway to the deadline, then the deadline, then each morning once it is past; a task without a deadline
  waits 2, 4 then 7 days). If the person changed the task while she was writing, theirs wins. These turns keep
  nothing in the conversation and cost the person credits like any answer.
- **A task is not nagged for ever**: after `CLARA_TASK_MAX_REMINDERS` reminders (10) the queue is emptied
  (the last notification says so), and a done task is never reminded. At most 100 open tasks (500 in all) per
  person, 10 reminders queued per task, 200 characters of title, 2000 of description.
- **Sub tasks.** A task can be divided into sub tasks (`parent_id` when adding one; the tool `add_task` has it
  too; `/task sub <id> ...` in the consoles), and those into sub tasks again: each has its own title, description,
  deadline and reminders (Clara picks the reminders of a sub task like any other). The rule: **a sub task's
  deadline and reminders can never be after the deadline of the tasks it is part of** (the earliest of the chain
  counts; a task without a deadline sets none). Adding or changing one past it is refused (422, with the limit),
  and so is moving a deadline before one of its sub tasks, which is named: change that one first. The reminders
  the rules or Clara pick are cut at the limit, and a follow-up never queues one after it. **Completion goes both
  ways**: finishing a task finishes its open sub tasks, finishing the last open sub task finishes the task it is
  part of (and so on up; so does deleting the last open one), reopening a sub task reopens the done tasks above it,
  and a done task takes no new sub task. Deleting a task deletes its sub tasks. **Moving**: an open task, with its sub tasks, can be moved under another open task (or back to a main task) and put in an order (`position`; the web page does it by dragging, the tool `update_task` has `parent_id`, the consoles `/task move <id> <parent-id>|none`): nothing of it or of its sub tasks may be after the deadline of the tasks it is then part of, and the task it leaves is done if all that is left of it is. At most 50 sub tasks directly under
  one task; they count in the 100 / 500 limits. There is no way to move a task under another one yet.
- **Receiving them** is the event stream of *Notifications*: a task reminder is a `notification` titled
  `Task: <title>`, with the source `tasks`, for the surfaces the task names (`targets`, empty: all of the
  person's clients). A missed one arrives when the client is back, like the others.
- **Done and reopened.** `status: done` drops the queue; reopening has Clara pick new reminders (or takes
  those given). A task's `timezone` is the person's clock when it was made (an IANA name, or an offset).
- **Privacy.** `/forget-person` erases the person's tasks; merging two people merges their lists.

## Scheduled tasks

A scheduled task is a prompt that Clara runs by herself at a set time (`schedule.py`, routes in `scheduleapi.py`,
the *Schedule* page of the web site). It has a name, a prompt, documents (read by the browser like in the chat and put
after the prompt), the integrations attached to it, and optionally a project. It runs **once**, or as a **routine**:
every day, on some days of the week, or every month (on the day of the first run, the last day of a shorter month), at
the time of day of its first run in the clock it was made in (an IANA name, so summer time is followed).

- **One conversation per task.** Every run is a turn in the same conversation (`<surface>:<user>:schedule-<id>`),
  as the person's account, so the runs read one after the other in their history; the integrations of the task are
  attached to that conversation (and those of its project are inherited). The turn is told it is a scheduled task: it
  asks nothing, and begins its final answer with a summary of at most three sentences.
- **Permissions work as in a chat.** A run never waits: an action set to *ask* is stored, pushed to the person's
  surfaces, and Clara carries on and says what is waiting; when they answer, the usual follow-up turn happens in the
  same conversation.
- **The summary.** When a run is over the person gets a private Discord message (a notification to the surface
  `discord`, title `Scheduled task: <name>`): the first paragraph of Clara's answer, or why it failed, and how many
  requests wait for them. Without a linked Discord account nobody is messaged; the result is in the conversation and
  on the page (`last_status`: ok / failed / missed, `last_summary`).
- **A missed run.** The next run is set before a run starts, so nothing runs twice. If the server was off and the due
  time is more than an hour past, the run is skipped (and the person told); a run that was cut by a stop starts again
  at the next start. A run due while the previous one still goes is skipped too.
- **Limits.** 50 per person; prompt 20 000 characters, documents 150 000 in all.

## Notifications

A notification is a message pushed to a person's clients **now**: a pop-up in the desktop app, a line with
a bell in `clara-chat` and in the console. Like a reminder, it is for one person (on all their surfaces,
or the ones in `targets`). Three kinds of senders:

| Who | How | `source` |
| --- | --- | --- |
| **Clara** | her `notify` tool (`text`, `title?`, `targets?`), e.g. when a long task is done; at most 3 per answer | `clara` |
| **a client** | `POST /v1/notifications` `{surface, user_id, text, title?, targets?, conversation?}` → `{id, sent_at, targets}` (the person of `surface:user_id`; 422 invalid, 429 too many) | the client's name in `CLARA_TOKENS` |
| **the server** | by itself, see below | `server` |

The server notifies when:

- **a turn took long**: one that lasted as many seconds as its person's delay tells them it is done, with the
  start of the answer (not for sub-agents, nor for reminder announcements). A turn of the console
  counts the time its tools ran on its side. The delay is `CLARA_NOTIFY_LONG_TURN` (120; `0`: never) until the
  person sets their own (`0`: never): with `/notify-after` in `clara-chat` and in the console, in the Account
  page of the web site, in the Settings of the desktop app, or `PATCH /v1/settings`. It is kept with the person,
  so it is the same on every client;
- **a conversation was summarised** (automatically or on demand): its person is told how full the context
  was and is now, on the conversation's surface. A conversation with several people (a Discord channel) tells
  nobody;
- **a reminder could not be written by Clara** (see *Reminders*);
- **the provider or the model changed** (`/provider`, `/model`): this one is for **everybody**.

A person may be sent at most 10 notifications a minute by Clara and the clients (the server's own are not
counted); texts are at most 2000 characters, titles 100.

### The event stream

A client opens `GET /v1/notifications/stream?surface=<its surface>&user_id=<the user>` and keeps it open (a
`: keepalive` comment every 15 s; reconnect when it drops). It gets, for **that account's person and that
surface**:

- `{"type": "reminder", ...}`, see *Reminders*;
- `{"type": "notification", "id", "title", "text", "sent_at", "source", "targets", "conversation", "everyone"}`
  (`conversation`: what it is about, if anything; a client showing that conversation may skip the pop-up);
- `{"type": "server", "state", "message"}`: the state of the server (see *Stopping the server*).

Without `surface` and `user_id`, a stream only gets the state of the server and what is for everybody.
`CLARA_CLIENT_SURFACES` applies: a client listens only as an account of its own surfaces (403 otherwise).
`GET /v1/reminders/stream` is the same stream, under its earlier name.

**Missing the moment.** Events are stored for 7 days, and the server remembers how far each *listener* (a
token + an account) has read. A listener that connects after something fired is sent what it missed,
oldest first: compare `fired_at` / `sent_at` with the clock. A listener the server has never seen starts
from now, with no backlog. An event is delivered at least once: if the connection drops while it is being
sent, it comes again on reconnection. Two connections of the same listener both get what fires while they
are open, but share what they missed.

## Users

A **user** is a name and a password. Signing in gives a token that is bound to that user and to one *surface*, so
the server knows who is speaking and refuses anything that claims otherwise (HTTP 403): a user cannot read
another's conversations or facts, whatever the request says. One user is **one person on every surface**: signing in
on the web, in the desktop app and in the terminal gives the same memories, with no link codes; the web site and the
desktop app also show and continue each other's conversations (the terminal's and Discord's stay apart).

| Surface | Used by |
| --- | --- |
| `web` | the web site |
| `app` | the desktop app (`clara-app`) |
| `cli` | `clara-chat` |
| `console` | `custom-console`, and `clara-admin` |
| `discord` | not a sign-in surface: the Discord bot signs its accounts in for its people (see *Discord*) |

`CLARA_USER_SURFACES` lists the surfaces a user may sign in on (those four by default).

**Making users** is the administrator's job, in the server's console or from another computer
(`clara-admin "/user add erwan admin"`):

| | |
| --- | --- |
| `/user add <name> [admin] [discord:<id>]` | makes the user and **a password, shown once** (it never passes through the traffic log); with a Discord account, signs it in as them |
| `/user link <name> <discord:id \| member>`, `/user unlink <name> <discord:id>` | signs a Discord account in as the user with no password (`1234`, `discord:1234`, `<@1234>`, or a name the running bot sees), or out |
| `/user passwd <name>` | a new password; the user is signed out everywhere |
| `/user admin <name> on\|off`, `/user disable <name>`, `/user enable <name>`, `/user remove <name>` | roles and access. The last administrator cannot be demoted, disabled or removed |
| `/user logout <name>` | signs a user out of all their devices |
| `/user list` | users, roles, devices, last sign-in |

**Letting people make their own.** Off by default. With `CLARA_WEB_SIGNUP=true` the web site's sign-in page offers
*Create one*: a user name, a password (repeated), and the new user is signed in at once. Such a user is never an
administrator, and always a **new person**: unlike `/user add`, it does not take over the memories of an account that
has their name (anyone could claim `discord:1234` otherwise), so a name an account already uses is refused. An
address (other than this machine) may make 5 users a minute, then none for an hour. Leave it off on a server that
strangers can reach (Tailscale Funnel); if you turn it on there anyway: such a user starts with every integration
(GitHub, Google Drive, folders) **off** until an administrator allows them (`CLARA_SIGNUP_INTEGRATIONS=true` allows
them at once), names that pass for the operator's (`admin`, `root`, `clara`...) are refused, one user may have
`CLARA_MAX_TURNS_PER_USER` (2) answers running at once, and each has the daily credits of `/limit default`.

The same things are on the web site's *Admin* page, where a password can also be typed instead of generated. A user
changes their own password on the *Account* page, which signs out their other devices. A device that logs in
again (same user, surface, device name and address: a console launched twice) replaces its old session instead of
adding a line to the list of devices.

A user takes over the memories of accounts that already have their name: `/user add erwan` finds `cli:erwan`,
`app:erwan`... and the user is that person. Signing in on a surface joins its account (`app:erwan`) to them; if both
already have memories, signing in is refused (409) and an operator merges them with `/link`.

**Tokens.** Signing in returns a random token (`clu_...`). The server keeps its hash only, and it works until it is
revoked (sign out, a new password, the user being disabled or removed), has been unused for `CLARA_SESSION_DAYS`
days (90; `0` = never) or is `CLARA_SESSION_MAX_DAYS` old (365; `0` = no limit), used or not. Programs keep the token, not the password: `clara-chat` and `clara-admin` ask for the password
once and save the token (`%APPDATA%\clara\sessions.json`, or `~/.config/clara/`; `clara-chat --logout` forgets it);
the desktop app asks in its settings; `custom-console` signs in by itself with `CLARA_USER` / `CLARA_PASSWORD`.
Wrong passwords are limited per address (see *Security*).

**The first administrator.** The server's own console can do it. Under systemd there is none, so use
`CLARA_ADMIN_TOKENS` once: `clara-admin --token <admin token> "/user add erwan admin"`.

## Projects

A project is what other chat services call a project: files given once, with instructions of its own, that every
conversation of the project can use. It belongs to a person and is the same on every surface (the web site and the
desktop app both manage them).

- **What goes in**: text and code files, PDF (text extracted with `pypdf`), Word `.docx` (read without any extra
  package), `.zip` archives (unpacked into a folder named after them), whole folders (the clients send their text
  files), and **GitHub repositories**: the server downloads a snapshot of a branch, tag or commit (the archive
  GitHub makes, no git needed) into a folder named after the repository; *Sync* downloads it again. Public
  repositories need nothing; a private one is downloaded with the GitHub account the person connected (*Integrations*),
  or, for **administrators only**, with `GITHUB_TOKEN` (a fine-grained token with read access to their contents) in the
  server's `.env`: it is the operator's own access, so it is not lent to other users unless
  `CLARA_GITHUB_TOKEN_SHARED=true`. It never leaves the server. A branch, tag or commit is a name (letters, digits,
  `._+@-` and `/`, never `..`).
- **What is left out**: anything that is not text (images, programs, fonts, archives inside archives), dependencies
  and build output (`node_modules`, `.venv`, `.git`, `dist`, `build`, `target`…), lock files and minified files, and
  any file of more than 1 million characters of text. Each one left out is listed with the reason.
- **Limits**: `CLARA_PROJECT_MAX_MB` (default 20) million characters of text and `CLARA_PROJECT_MAX_FILES`
  (default 5 000) files per project; what would go beyond is left out.
- **How Clara sees them**: the project's name, description and instructions are in the system prompt of its
  conversations. When all its files fit in `CLARA_PROJECT_INLINE_PERCENT` (default 40) percent of the context window
  of the provider in use, they are put there whole, in the same order every time (so a model's cache is reused);
  otherwise the prompt lists them and Clara gets three tools: `list_project_files`, `read_project_file` (with line
  numbers, 400 lines at a time) and `search_project` (text or a regular expression, case ignored). The same project
  can be read whole with Gemini's window and searched with a local model's.
- **Conversations**: a conversation is put in a project by its first message (`project` in `POST /v1/chat`), and
  can be moved to another one or out of it (`PATCH /v1/conversations/{id}` with `project`, null for none); from its
  next message on, it uses the files of its new project. `GET /v1/conversations?project=<id>` lists those of a
  project, `?project=none` those in none. Deleting a project deletes its files; its conversations stay, in no
  project. Erasing a person (`/forget-person`) erases their projects.

| Route | |
| --- | --- |
| `GET /v1/projects?surface=&user_id=` | the person's projects (files, size, conversations, how they reach the model now) |
| `POST /v1/projects` | `{surface, user_id, name, description?, instructions?}` |
| `GET`, `PATCH`, `DELETE /v1/projects/{id}` | one project, with its repositories and the list of its files; `PATCH` takes `pinned` too (only pinned projects have their conversations in the web site's history; they come first in the list) |
| `POST /v1/projects/{id}/files` | `{files: [{path, data}]}`, `data` in base64: text, code, PDF, `.docx`, `.zip` (80 MB per request) |
| `GET /v1/projects/{id}/file?path=` | a file's text |
| `DELETE /v1/projects/{id}/files?path=&folder=` | a file, or every file of a folder |
| `POST /v1/projects/{id}/github` | `{repo, ref?}`: `owner/name`, `owner/name@branch` or a github.com address |
| `POST /v1/projects/{id}/sources/{sid}/sync`, `DELETE /v1/projects/{id}/sources/{sid}` | download it again, remove it and its files |

## Integrations

Projects keep a *copy* of files. Integrations are the other way: a person connects their own **GitHub** and **Google
Drive** accounts and adds **folders** (on the server, or on their own computer through the desktop app), attaches them
to a project or to one conversation, and Clara reads and works on them **live**, with tools, as that person. What she
may do is decided per kind of action, and anything that replaces or deletes asks first, **without stopping her**.

- **What can be attached**: a GitHub repository (and optionally a branch), a Google Drive folder or file, a folder of
  the server, a folder of the person's computer. They are added once in the *Integrations* page (web site and app:
  *Settings*, next to Account) and then attached to a **project** (every conversation of it has them) or to one
  **conversation** (the *Connections* button of the chat). A conversation may give a resource other permissions than
  its project does.
- **Her tools** (offered only where something is attached): `resources`, `res_list`, `res_read` (with line numbers),
  `res_search`, `res_write` (`create`, `overwrite`, `append`), `res_delete`, `res_move`, and for GitHub
  `github_branch`, `github_pr` and `github_issue`. Her prompt lists what is attached, with the permissions.
- **Permissions**: three *levels* of action: **look** (list, read, search), **change** (add something new, or change what
  stays recoverable: a new file, a commit on a branch, a pull request, an issue, a branch) and **replace or delete**
  (overwrite, delete, move, commit to the default branch, merge, close, delete a branch). Each one is *allow*, *ask* or
  *deny*. Where a level's answer comes from, most specific first: the project's or the conversation's own setting for
  that resource, the resource's, the account's, then the default (look: allow; the others: ask). A call the connector
  cannot classify counts as *replace or delete*. Overwriting a file is *replace*, creating one is *change*; on GitHub a
  commit to the repository's default branch is *replace*, to another branch *change*.
- **Asking without blocking**: when a call needs permission the tool answers at once, "waiting for permission (request
  #12), this was NOT done", and she carries on with whatever does not depend on it. The request shows as a card in
  the conversation (web and app), and in the rail ("Waiting for you"). If it is not answered within
  `CLARA_APPROVAL_NOTIFY_AFTER` seconds (60; each person can change it, or turn it off, on the Integrations page)
  it is pushed to the person's **other surfaces**: a notification in the app, a private message with
  **Approve / Deny buttons** on Discord (the buttons keep working after the bot restarts), the badge on the web site.
  Whoever answers first decides; the others are told it is settled. A request nobody answers lapses after
  `CLARA_APPROVAL_EXPIRE_AFTER` seconds (24 h) and she is told it was not done.
- **When it is answered**: *Approve* runs the action on the server exactly as it was asked (the arguments are frozen),
  records it, and a short **follow-up turn** tells her how it ended so that she can go on; a denial, or a failure, is
  told the same way. *Approve and do not ask again* (in this conversation, or for the resource) changes the setting.
  Only a signed-in person can answer, through the API (`POST /v1/approvals/{id}/decide`) from an account of theirs;
  **she has no tool to approve her own request**.
- **Connecting**: *GitHub*: paste a fine-grained personal access token (github.com/settings/personal-access-tokens)
  with access to the repositories she may use: "Contents: read and write" to commit, plus "Pull requests" and
  "Issues" if wanted. *Google Drive*: the administrator creates an OAuth client in a Google Cloud project, enables the
  Drive API, and puts `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`; the redirect URI to register is
  `<the server's address>/v1/integrations/google/callback` (Google wants `https` or `localhost`: the Tailscale address
  does) and `CLARA_PUBLIC_URL` is that address when the server is reached through a proxy. Keep the app **In
  production** in Google's console: in *Testing* mode Google ends the connection after 7 days (the page then says
  "Connect it again"). Each person then clicks *Connect Google Drive* and confirms on the page they come back to,
  which names the Clara user the Drive would be given to.
- **Folders on the server** are only reachable inside the roots an administrator lists (Integrations page, bottom): a
  person picks a folder under one, and Clara cannot leave it: paths are resolved (links followed, `..` refused) and
  checked at every call. Remove a root and what was added from it stops working. They are off until the administrator
  turns them on.
- **Folders on a computer** are added from the desktop app (*Integrations*, *Add*, *Folder on this computer*). The
  server never learns the path: it only knows the computer's id and the folder's alias. A call becomes a *job* that
  the app fetches (a `job` event wakes it), does **inside the folders that were added on that computer**, and answers.
  Whatever a job says, the app refuses a path that leaves them, so even a server that was taken over cannot read the
  rest of the disk. If the app is not running a read answers "offline" at once (and she carries on without it); a change
  is queued until it is back, and given up after a day.
- **Secrets** (a GitHub token, a Google refresh token) are encrypted in the database with `CLARA_SECRET_KEY` (a Fernet
  key or any long text; else a key is made in `data/secret.key`). They are never returned by the API, never shown,
  and kept out of the traffic log. Whoever has both the database and the key can read them.
- **Administrator**: each integration can be turned on or off for the server and, per person; the most open a level may
  be set (*ceiling*: "always ask" or "never"; it beats what a person chose, at every call); the folders of the
  server; and a **log** of everything she asked and did (who, what, where, how it ended). Finished requests are kept 30
  days. Erasing a person erases their accounts, resources and requests.

| Route | |
| --- | --- |
| `GET /v1/integrations?surface=&user_id=` | the kinds that are on, the person's accounts and resources (with their effective permissions), their settings |
| `PUT /v1/integrations/settings` | `{approval_notify_after}` seconds (0: never; null: the server's) |
| `POST /v1/integrations/github` | `{token}`: connect a GitHub account (checked, then kept encrypted) |
| `POST /v1/integrations/google/start` | the address to send the person to; Google then calls `GET …/google/callback`, which asks them to confirm (`POST …/google/confirm`) |
| `PATCH`, `DELETE /v1/integrations/accounts/{id}` | `{levels}` default permissions; disconnect (its resources go) |
| `GET /v1/integrations/browse/github`, `…/drive`, `…/server` | repositories, Drive folders, folders of the server, to pick from |
| `POST /v1/integrations/resources` | `{kind, account?, repo?, ref?, file_id?, path?, device?, alias?, label?, levels?}` (`kind`: `github_repo`, `drive_folder`, `drive_file`, `server_path`, `computer_path`) |
| `PATCH`, `DELETE /v1/integrations/resources/{id}` | `{label?, levels?}`; remove |
| `GET`, `PUT /v1/integrations/attachments` | what is attached to `?project=` or `?conversation=` (with what a conversation inherits); `{resource, project \| conversation, levels?}` |
| `DELETE /v1/integrations/attachments/{id}` | detach |
| `GET /v1/approvals?status=pending\|all&conversation=` | the requests for permission |
| `POST /v1/approvals/{id}/decide` | `{approve, remember?}`; 409 if it was answered already |
| `GET /v1/integrations/jobs?device=`, `POST /v1/integrations/jobs/{id}/result` | for the desktop app: what Clara asked of this computer's folders, and the answer |
| `GET`, `PUT /v1/admin/integrations`, `GET /v1/admin/integrations/log` | the administrator's switches, ceilings and folders; the log |

Notification events `approval` (a request to answer, with its summary and buttons) and `approval_resolved` come on the
same stream as reminders, for the surfaces `app`, `discord` and `web`; the chat stream carries an `approval` event when
she asks during a turn.

## Models

`/provider` and `/model` set the **server's own model**, but every person can be answered by another one, and every
provider that has its key works at the same time. A model is named `provider:model` (`local:llama3.2`,
`cloud:gpt-oss:120b`, `gemini:gemini-flash-latest`).

- **The administrators select the models users may choose**: the web site's *Admin, Models* tab lists every model of
  every provider (with a search, a provider filter, *Select shown* / *Unselect shown*, *Refresh*), or `/models` in the
  console. Users only ever see the selected ones. Nothing is selected at first: everybody uses the server's model.
- **Each user chooses per surface**: a model for the web site, another for the desktop app, the terminal
  (`clara-chat`, `/model`) and the console (`/model`). On the web it is the *Model* card of the *Account* page, and a
  picker next to the message box of the chat; in the app, *Settings*. A user with no valid choice (or whose choice
  an administrator took away) gets the server's model.
- **Discord has one model for everybody**, set by an administrator (*Admin, Models*, or `/models discord <model>`);
  it need not be one of those users may choose. `/provider` and `/model` stay the default of everything else
  (users who chose nothing, summaries and titles).
- **A token costs the weight of the model**, in credits (see *Usage limits*). The weight is 1 for a model of
  `CLARA_WEIGHT_REFERENCE_B` (default 8) billion parameters, proportional to the size above and below it (120B: 15,
  1B: 0.125; never below 0.01). The size is what the provider says (Ollama lists it), else what the name says
  (`gpt-oss:120b`, `mixtral:8x7b`); the Gemini, DeepSeek and Mistral APIs say nothing, so their models weigh 1 until
  an administrator sets a weight by hand (a weight set by hand beats the size; *Use the size* goes back). Users see
  each model's cost wherever they choose. Every `done` event says the `model_ref`, its `weight` and the `credits` the
  turn cost.
- Each model has the context window of its provider (`CLARA_<ID>_CONTEXT_WINDOW`).

```
/models                      every model, which ones users may choose (*), sizes and weights
/models refresh              ask the providers again (what they offer is kept a minute)
/models enable other-model   a model of the active provider; or cloud:gpt-oss:120b, cloud (a whole provider), all
/models disable all
/models weight cloud:gpt-oss:120b 20     credits per token by hand (auto: from the size)
/models discord cloud:gpt-oss:120b       the model of Discord (default: the server's own)
```

| | |
| --- | --- |
| `GET /v1/models` | `?surface=&user_id=`: `models` the person may choose (`ref`, `name`, `provider_label`, `weight`), `personal` (every model of the providers they brought an API key for, `weight` 0), the server's `default`, their `choices` by surface, the model `current`ly used on that surface |
| `PUT /v1/models/choice` | `{surface, user_id, model, for_surface?}`: choose a model for a surface (null: the server's); not for Discord |
| `GET /v1/admin/catalog` | `?refresh=true`: every model of every usable provider, with `enabled`, `weight`, `size_b`, the model of `discord`, the providers that did not answer (`problems`) |
| `PATCH /v1/admin/catalog` | `{refs, enabled?, weight?, auto_weight?}` (a weight for one model at a time) |
| `PUT /v1/admin/catalog/discord` | `{model}` (null: the server's own) |

## Usage limits

Each person has a limit of **credits a day**: what the model reports for every round of their answers (the prompt it
was given and what it wrote), times the weight of the model that answered (see *Models*: a token of a model of weight
1 is one credit), added up per person over a calendar day (UTC; it starts again at midnight UTC). The numbers are
credits wherever the environment variable (`CLARA_DEFAULT_DAILY_TOKENS`) or the database still say tokens.

- An **administrator** (a user flagged administrator) has no limit, whatever is set.
- Another user has the limit an administrator set for them, or the **default** when none is set. A person with no
  user (a terminal or app using a client token) follows the default.
- `0` (or `off`) means no limit. Nothing is limited until `CLARA_DEFAULT_DAILY_TOKENS` or an administrator says so.
  Limits set before credits existed keep their number: it now counts credits (the same, on a model of weight 1).
- The limit is checked when an answer starts: the answer that crosses it is finished, the next one is refused with
  `429` (`Retry-After` says when), or an `error` event with `"reason": "usage_limit"` on a stream. A Discord message
  not addressed to Clara is only kept as context when its author is out of credits. Compactions and titles are not counted.
- Every answer's `done` event has a `quota` (`used`, `limit`, `remaining`, `resets_at`; `null` limit: none).

Set it from the console (`/limit`, also in `clara-admin`), the web site (Admin, Users: a column, *Set the daily
credit limit…*, *Default limit*) or the API. Credits are written `500000`, `500k`, `2m` or `off`.

```
/limit                       everybody's usage today and limit
/limit default 2m            the default, for users with no limit of their own
/limit erwan 500k            erwan's own limit  (off: none, default: follow the default)
```

| | |
| --- | --- |
| `GET /v1/admin/users` | each user has `usage: {used, limit, remaining, resets_at, own_limit, default_limit}` (also in `GET /v1/auth/me`) |
| `PATCH /v1/admin/users/{name}` | `{token_limit}` (0: none) or `{follow_default_limit: true}` |
| `GET /v1/admin/limits`, `PUT /v1/admin/limits/default` | `{default}`; `{tokens}` (0: none) |

## Usage statistics

Every answer is written to a **usage log** (one row per answer, the model rounds of a tool loop added up): when, who
asked, the surface, the conversation, the model that answered, the **tokens in and out** (as the model reported them;
estimated, and marked `~` on the web site, when it reported nothing) and the credits it cost. Answers Clara gives on
her own (reminders, scheduled tasks, task helpers) are logged as `scheduled`, and so are the server's own
**compactions** and **titles** (`compaction`, `title`; they cost no credit and go to the one person who wrote in the
conversation, or to nobody when several did). No message text is kept in the log. It is a record only: the daily
credit limit (*Usage limits*) is counted apart and does not read it. A person's rows go when they are erased, and
follow them when two accounts are linked.

**Discord is kept apart from every other surface** everywhere: two totals for the server, and for each person a
Discord column and an "other surfaces" column (the web site, the app, the terminal and so on, detailed per surface).

For administrators, on the web site, *Admin, Usage*: totals of Discord and of the rest, a table per person (answers,
tokens in and out, Discord, other surfaces, **preferred models**: the three they use most, last use) over a period
(24 hours to all time), and the **history** of every call, filtered by person, surface (Discord or not) and kind.

Everybody else has a **Usage** page of their own (the avatar menu, next to *Account*): the same figures about
themselves only: Discord and the other surfaces, their tokens in and out per surface, their preferred models, today's
credits against their limit, and the history of their own calls.

| | |
| --- | --- |
| `GET /v1/me/usage`, `GET /v1/me/usage/history` | the same, for the signed-in user only (all their accounts: the web site, the app, Discord once linked): `{days, usage, quota}` (`usage` is the person's entry above, `null` before their first call; `quota` is today's credits) and `{calls, next, totals}` with the same filters but `person` |
| `GET /v1/me/api-keys`, `PUT /v1/me/api-keys/{provider}`, `DELETE /v1/me/api-keys/{provider}`, `GET /v1/me/api-keys/history` | the user's own API keys (Usage page): the providers that take one (`cloud`, `gemini`, `deepseek`, `mistral`) with `saved` and a `hint` (the last 4 characters; the key is never given back), `PUT {api_key}` checks the key with the provider and keeps it encrypted (`CLARA_SECRET_KEY`), and the tokens used on those keys, counted apart from the server's. A user with a key for a provider is answered by every model of it on their key (web, app, terminal, console; never Discord): no credits are counted and the daily limit does not stop them |
| `GET /v1/admin/usage` | `?days=` (0: ever): `{users: [{person, user, is_admin, answers, prompt_tokens, completion_tokens, credits, discord, other, surfaces, models, first_at, last_at}], totals: {discord, other}}` |
| `GET /v1/admin/usage/history` | `?person=&group=discord\|other&surface=&kind=message\|scheduled\|compaction\|title&model=&days=&before=&limit=`: `{calls: [{id, at, kind, surface, group, conversation, person, user, model_ref, model, provider, prompt_tokens, completion_tokens, credits, rounds, estimated}], next, totals}`, newest first; pass `next` as `before` for the following page |

## Markdown files

Clara can write a markdown file for a person (notes, a summary, a README...) and change it later. The files belong to
the person, are the same on every surface, and are not put in the prompt: Clara reads one when she needs it. Her tools:
`create_markdown_file` (a new file; `overwrite` replaces all the text of one that exists), `edit_markdown_file`
(replace a passage: `old_text` must be in the file once, so only the passage travels, not the whole file; `replace_all`
for every place), `append_markdown_file`, `read_markdown_file` and `list_markdown_files`. A name has no folder (`notes.md`,
`.md` is added), a file at most 200,000 characters, a person 200 files.

The web site shows a card in the chat when she makes or changes a file (open, copy, download) and has a **Files** page
with all of them. The other clients cannot show one: she tells the person to look on the web site. The tools send a
`markdown_file` event `{action: created|updated|replaced, file: {id, name, size, updated_at}}` after the tool call.

| | |
| --- | --- |
| `GET /v1/markdown-files` | `?surface=&user_id=` the person's files, newest first |
| `GET /v1/markdown-files/{id}`, `DELETE /v1/markdown-files/{id}` | `?surface=&user_id=` a file with its text; delete it |

## QCM

On the web site and in the desktop app (surfaces `web` and `app`; the tool is not offered on the others), Clara can
ask a **QCM** with the `qcm` tool: a form the client displays. It is not blocking: the turn ends with the form
shown, and the user's answers come back as their next message, which Clara then comments.

- **Questions**: `type` is `single` (one option, radio buttons), `multiple` (check boxes) or `text` (a free
  answer, typed). Limits: **20 questions** per QCM, **2 to 10 options** per choice question, one QCM per answer;
  a form that breaks them is refused with a reason Clara reads and fixes (`qcm.py` has the other limits: lengths).
- **Quiz**: a choice question may carry `correct` (the numbers of the right options, the first being 0); when every
  choice question has it the client scores the QCM and shows the corrections, with the optional `explanation`
  of each question and the `answer` expected of a text question. Without `correct` it is a questionnaire. Text
  answers are not scored by the client: Clara reads them.
- **Formulas**: questions, options, explanations and titles may hold LaTeX (`$x^2$`, `$$…$$`, `\(…\)`, `\[…\]`): the web site and the
  app typeset them (KaTeX), as in the chat.
- **Event**: the stream carries `{"type": "qcm", "form": {"ref", "title", "graded", "questions": [...]}}` after
  the `tool` event of the call. `ref` identifies the form (a hash of its questions).
- **Answers**: the client sends `[QCM answers <ref>] <title>`, then for each question `n. <question>` and
  `Answer: B. Lyon; C. Paris` (the letter of each option chosen; the text for a text question; `(no answer)`).
  `qcm.format_answers` writes it, `clara-app` and the web site write the same.
- **Opening a conversation again** (`GET .../messages`): an answer that asked a QCM carries `qcm`, the forms,
  each with `answers` (read back from the message that answered it) or `null` while it waits.

## The web site

`http://127.0.0.1:8765/` (or the Tailscale address) opens Clara in a browser, with the surface `web`:

- **Sign in**, or *Create an account* when `CLARA_WEB_SIGNUP` allows it (see *Users*).
- **Chat**: answers stream in as Markdown, with LaTeX formulas (`$x^2$`, `\(x^2\)`, `$$…$$`, `\[…\]`) typeset by KaTeX
  (vendored in `web/katex/`, so it works offline; the script is only loaded once an answer has a formula); your conversations at the side (search, pin, rename, delete, titles
  written by Clara); a bar showing how full the context is and *Summarise* to compact it; documents with 📎, by
  drag and drop or by pasting: PDFs are read by the server (`pypdf`), text and code in the browser, as in the desktop app.
  The list shows what is pinned, then each *pinned project* with its five newest conversations (*Show 5 more*, and
  *Show less*, in the group), then the others by date; the conversations of a project that is not pinned are on its
  page, and a search finds them all. Every group folds (these choices are kept in the browser). What you typed in a
  conversation and did not send stays there when you change page or reload (the text is kept in the browser, the
  attached documents until a reload). There is no *Chat* link: the list is the way back to a
  conversation and *New chat* starts one. A chip next to the title names the project of the conversation shown, and
  its menu moves it.
- **Projects**: your projects as cards; one project shows its conversations (*New chat* starts one in it), its
  instructions, and its files: add files, a folder, or a GitHub repository (or drop files and folders on the page),
  read a file, remove it, sync or remove a repository. It says whether Clara reads all the files with every message
  or searches them, for the model in use.
- **Tasks**: your to-do list as a list (to do, done, all) and as a month calendar: each task with its deadline,
  the reminders sent and the next one; add, edit, mark as done, reopen, delete, read one in full. A task given no
  reminder gets them chosen by Clara, who also moves the next ones each time one is sent: the page reads the list
  again every 30 seconds.
- **Schedule**: prompts Clara runs by herself (see *Scheduled tasks*): each with when it runs next and how the last run
  went; plan one, edit, pause, *Run it now*, delete, open its conversation.
- **Memory**: what Clara remembers about you, to add to or remove from.
- **Account**: your usage, the model Clara answers you with on each surface (among those an administrator offers, with
  their cost in credits), change your password, see and sign out your devices, link an account that has no password
  (Discord) with a link code. The chat has a model picker too.
- **Discord** (administrators): the bot built into the server (its state, Discord account, servers, latency, last
  error; *Start*, *Stop*, *Restart*; the invite link), the Discord servers it is in with where Clara may chime in, and
  the Discord accounts signed in, each with *Sign out*.
- **Admin** (administrators): *Users* (add, reset a password, make administrator, disable, sign out, remove),
  *Models* (select the models users may choose, set what a token of each costs, choose Discord's model),
  *Server* (status, switch the server's own provider and model, stop), *People & memory* (everybody Clara knows, their facts,
  the relationship, linking, erasing a person) and a *Console* box with every server command.

Reminders and notifications are not shown on the web site (the desktop app, the terminal, the console and Discord
show them; the Tasks page lists what is still to come), and it runs no tools on your computer.

It is plain HTML, CSS and JavaScript in `src/clara/web/`, with no build step and nothing loaded from elsewhere. It works on computers and phones (on a phone, the navigation slides in from the menu button), and follows
the system's light or dark setting; a switch at the bottom of the navigation forces *Light* or *Dark*, remembered in
that browser. Its two fonts (Hanken Grotesk and Epilogue, SIL Open Font License) are served from `web/fonts/`. The
browser keeps your sign-in in an HttpOnly cookie (out of reach of scripts) and the server only honours it when the
request has the header `X-Clara-Web: 1`, which a page of another site cannot add. Everything it shows is built from
text nodes; the page's Content-Security-Policy allows only its own files.

## Traffic log

Every request that comes in or goes out is written to `data/logs/traffic-<date>.jsonl` (one file per UTC day,
one JSON object per line). Entries of one exchange share an `id`:

| `dir` | `kind` | what |
| --- | --- | --- |
| `in` | `request` | a client's HTTP request: `peer` (`client:<name>`, `admin:<name>`, `anonymous`, `unknown-token`), method, path, query, address, body |
| `out` | `response` | its answer: status, body, duration, bytes, `outcome` (`complete`, or why it was cut) |
| `out` | `sse` | each event sent on a stream (a turn, the event stream). The pieces of an answer (`token`) are only counted in the `response`: the `done` event holds the whole answer |
| `out` | `llm_request` / `llm_response` | each call to the model: provider (`peer` = `ollama:local`, `ollama:cloud`, `gemini`, `deepseek` or `mistral`), host, model, the messages and tools sent; then the text, tool calls, token counts, duration and error |
| `out` | `llm_call` | the other calls to the provider: listing its models, checking the API key |

Never written: the bearer tokens (only the client's name), the API key, and the values of fields called
`code`, `token`, `password`, `api_key`... (a link code, for instance). Bodies are cut at
`CLARA_TRAFFIC_LOG_MAX_BODY` characters (100 000). Files older than `CLARA_TRAFFIC_LOG_DAYS` (7) are deleted;
`CLARA_TRAFFIC_LOG=false` turns the log off. The writing happens in a background thread.

**Mind that** the log holds everything people say and, through the prompts, what Clara knows about them. The files and
the data directory are readable by the account that runs the server only (`rw-------`, set each time it starts).
`/forget-person` and a person's own *Erase my account* delete their lines from the log too (the lines of their
accounts, their conversations and the prompts that name them). Read it with any JSON tool, e.g.
`jq 'select(.kind=="llm_response") | .text' data/logs/traffic-2026-10-02.jsonl`.

## Stopping the server

`/stop` (in the server's console, or `clara-admin /stop` from another computer with an admin token),
Ctrl+C or SIGTERM stop the server **without cutting anybody off**:

1. every client connected to `GET /v1/notifications/stream` is told: `{"type": "server", "state": "stopping"}`
   (a client that connects meanwhile is told at once);
2. new questions are refused with **HTTP 503** (`Clara is stopping and takes no new question`), and so are
   new compactions; reminders that come due wait for the next start;
3. the server **waits, with no time limit**, for what is running: replies being written, agents waiting for
   their client's tools (`tool-results` are still accepted), summaries, announcements being written;
4. clients get `{"type": "server", "state": "stopped"}`, their streams end, and the process exits.

To stop without waiting: `/stop now`, or a second Ctrl+C. Running turns are then cut, and say so on their
side. `/status` shows `STOPPING` and how many turns are awaited, and the log says so every 30 s. In the
embedded console `/quit` and Ctrl+D do the same as `/stop`. Clients keep trying to reconnect: when the
server is back they get `{"type": "server", "state": "running"}`.

A client that has no event stream open (a script, a bot) is not told; it only sees the 503, or the
connection closing. The three clients of this repository (`clara-chat`, the console, the desktop app) all
listen and say "Clara is stopping / is not running / is running again".

## Restarting the server

`/restart [now]` (in the console, or `clara-admin /restart`) and the **Restart…** button of the web site's
*Admin → Server* page do, in this order:

1. **Update, while the old server still runs**: `git pull --ff-only`; then `pip install --upgrade -e ".[discord]"` when
   the code changed since the server started (or `discord.py` is missing); then `.env` is brought up to date with
   `.env.example`, keeping your values (the old file is saved as `.env.bak.<date>` when it changes), as
   `update-and-run.sh` does. **If a step fails, nothing is stopped**: the server goes on and the answer (HTTP 500)
   holds what the step printed. Another restart or a stop under way is refused (409).
2. **Stop the careful way**, as `/stop` does (`now`: without waiting for the answers that are running).
3. **Start again** with the same arguments: the process executes itself again (same PID, so systemd is not
   disturbed), or, on Windows, which cannot do that, starts a new process (in a new window when there is a console).
   What `.env` had when the server started is renewed from the file as it is now; a setting the environment itself
   sets (systemd, your shell) is not touched.

`POST /v1/admin/restart` `{now?}` does the same: `202 {id, steps, message}`. The new server reads `data/restart.json`,
and `GET /health` says `"restarted": "<id>"` from then on, which is how the page that asked knows it worked: it shows
"Restarting…", waits for that id, says "The server restarted successfully" and reloads itself (so that it gets the new
pages). A browser that was reloaded meanwhile goes on waiting. The page gives up after 3 minutes and says so.

`GET /v1/admin/restart[?refresh=true]` → `{needed, reasons, in_progress, last}`. A restart is **proposed** to
administrators (a *Restart needed* button in the navigation rail, on every page) when:

- `.env` changed since the server started;
- the code on disk is newer than what runs (somebody ran `git pull`);
- the remote has new commits (`git fetch`, at most every 10 minutes; *Check for updates* asks at once).

The pages that say "then restart the server" (the Discord bot's token, `discord.py` missing) carry the button too.

## Model overload and retries

A model that is overloaded (HTTP 429, 500, 502, 503, 504, `Retry-After` honoured up to a minute) or that cannot be
reached (connection refused or cut) is asked again, **only while it has said nothing yet**: once a piece of the answer
was sent on, asking again would repeat it, so that failure ends the turn as before. The wait starts at
`CLARA_LLM_RETRY_DELAY` seconds (2) and doubles, with a little jitter, for at most `CLARA_LLM_RETRIES` new tries (3;
`0` never retries). The model's slot is free while waiting. Clients get `{"type": "retrying", "message": "The model is
busy, trying again (1/3)…"}` events (the web site shows them as a notice; the other clients ignore them). A model that
stays silent (`CLARA_LLM_FIRST_TOKEN_TIMEOUT`) and a refused key are not retried.

## Simultaneous use

- Turns in the **same conversation** are answered one after the other.
- Turns in **different conversations** run in parallel, up to
  `CLARA_MAX_CONCURRENT_LLM` model calls at once.
- A model slot is held only while the model works: a client that reads its stream slowly, or not at
  all, never keeps one busy. A model that stops answering is given up on after
  `CLARA_LLM_FIRST_TOKEN_TIMEOUT` / `CLARA_LLM_IDLE_TIMEOUT` seconds (an `error` event, or 504).
- One person may have `CLARA_MAX_TURNS_PER_USER` (2) answers running at once (a third is refused with 429), so that one
  account cannot hold every model slot. The server's own turns (reminders, schedules) are not counted.
- A request body is refused (413) over 16 MB (90 MB for project files, 31 MB for a PDF), counted as it arrives.
- Memory is written only by the server process, so clients never conflict.

## Security

- **Making an account on the web site is off** unless `CLARA_WEB_SIGNUP=true`; turn it on only where everyone who can
  reach the server may use it (it spends your model's tokens). Open sign-ups never inherit another account's memories.
- **Everything the server keeps is private to the account that runs it**: the data directory is `rwx------` and its
  files `rw-------` (database, logs, the key of the connected accounts, `.env` backups: only the newest three are
  kept). `.env` itself should be `chmod 600`.
- **`web_fetch` only reads addresses the person wrote** (in this conversation) **or that `web_search` returned**: a page
  or a file Clara reads can tell her to fetch an address that carries what she knows about the person. Set
  `CLARA_WEB_FETCH_ANY_URL=true` to let her read any address.
- **A person can take their data and leave**: *Account → Your data* downloads everything the server keeps (JSON,
  `GET /v1/auth/export`) or erases the user, the person and their traffic-log lines (`POST /v1/auth/delete-account`,
  password asked again). The sign-up page tells what is stored, which model services read it and how long it is logged.
- **Limit what client tokens may do**: set `CLARA_CLIENT_SURFACES` (a client with no entry may speak for anybody on any
  surface; the server warns at start). The Discord bot built into the server needs no token at all.
- **People sign in with a password** (see *Users*); the server then knows who is speaking, and a token it
  gives cannot act as anybody else. Passwords are kept only as salted scrypt hashes (N=2¹⁵, made again at the next login when they were weaker); the tokens only as
  SHA-256 hashes; neither is ever written to the traffic log.
- Client tokens (`CLARA_TOKENS`) are required; the server refuses to start without any. They are for programs
  (the Discord bot) that speak for several people. Use one token per client so one can be revoked alone.
- Clients with such a token are **trusted** within their surfaces: a token proves which client calls, and the client
  says who is speaking. `CLARA_CLIENT_SURFACES` (`terminal=cli|console,discord=discord`) limits each
  token to its surfaces: facts, conversations and accounts of other surfaces answer 403. A client
  with no entry may use any surface (the server warns at startup). Do not hand a token to anything
  you don't control.
- It listens on `127.0.0.1` by default. To reach it from another machine, use Tailscale
  (see *Reaching the server with Tailscale*) or an HTTPS reverse proxy rather than exposing the port.
- An address that sends `CLARA_AUTH_MAX_FAILURES` (10) wrong tokens or passwords within a minute is refused with
  HTTP 429 for `CLARA_AUTH_BLOCK_SECONDS` (300), even with the right one. This machine itself is never counted.
- Facts are given to the model as *data*; the prompt says they are not instructions.

## Reaching the server with Tailscale

When you cannot forward a port (CGNAT, a router you do not control), Tailscale gives the server an HTTPS
address without opening anything. Set in `.env`:

```
CLARA_TAILSCALE=serve      # or funnel
```

| Mode | Who can reach it | Clients need |
| --- | --- | --- |
| `serve` | the devices of your tailnet | Tailscale installed and logged in |
| `funnel` | the whole internet | nothing, only the URL and a token |

At startup the server finds its name (`tailscale status --json`) and runs `tailscale serve|funnel --bg
--https=<port> http://127.0.0.1:8765`; tailscaled terminates HTTPS and forwards to the server, which keeps
listening on localhost only. The address (`https://<machine>.<tailnet>.ts.net`) is in the log and in `/status`.
When the server exits it removes the mapping. If Tailscale cannot be used (not installed, not logged in,
Funnel not allowed...) the server **starts anyway**, on localhost, and says why in the log and in `/status`.
The first DNS lookup of a new address can take several minutes to work from outside.

**Once, on the machine** (Ubuntu):

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Then in the admin console (https://login.tailscale.com/admin/dns) turn on **MagicDNS** and **HTTPS
certificates**; for `funnel`, the machine must also be allowed to use Funnel (policy `nodeAttrs` with
`funnel`, which new tailnets have for all members). If Clara runs as its own user (the systemd unit), let that
user drive Tailscale: `sudo tailscale set --operator=clara`.

**The web site** is at that same address: open `https://box.tail1234.ts.net` in a browser. With `funnel` its sign-in
page is public too; with `serve` only your tailnet sees it. On the machine itself it is `http://127.0.0.1:8765`.

**On the clients**, use the HTTPS address as the server: `clara-chat --url https://box.tail1234.ts.net`
(`CLARA_URL`), the same for `clara-admin`, the Server field of the desktop app.

Settings (`.env.example`): `CLARA_TAILSCALE` (`off`, `serve`, `funnel`), `CLARA_TAILSCALE_PORT` (443, 8443 or
10000: the only HTTPS ports Tailscale offers; Clara owns it while this is on), `CLARA_TAILSCALE_BIN`.

**With `funnel`**, anyone who finds the address can try tokens, so:

- every token (chat and admin) must have at least 32 characters, or the server refuses to start. Users are
  protected by their passwords (10 characters or more) and by the limit on wrong ones, so give them long ones;
  `python -c "import secrets; print(secrets.token_urlsafe(32))"` makes one;
- the remote admin console (`CLARA_ADMIN_TOKENS`) is public as well: leave it empty unless you need it;
- wrong tokens are limited per address (see *Security*), and the traffic log shows the real address of each
  request in `from`: the server trusts `X-Forwarded-For` from `127.0.0.1` and `::1` only, which is where
  tailscaled connects from.
- the traffic log holds everything people say (see *Traffic log*).

Keep `CLARA_HOST=127.0.0.1`: with `0.0.0.0` the plain HTTP port stays reachable on your network too (the
server warns).

## Running on a Linux server

See `deploy/clara.service` (systemd). Ollama must be reachable from the server
(`OLLAMA_HOST`), or use a `-cloud` model. To publish it with Tailscale, see above; the service starts after
`tailscaled`.

## Layout

```
src/clara/
  settings.py   environment configuration
  memory.py     SQLite: people, accounts, facts, history, the list of conversations
  linking.py    single-use codes that prove control of an account before it is linked
  llm.py        LlmBackend interface; Ollama, and the OpenAI-compatible API (Gemini, DeepSeek, Mistral)
  providers.py  local / cloud / gemini / deepseek / mistral, switchable live, saved in runtime.json; any model of
                any of them can run at the same time (models.py)
  projects.py   projects: their files (the tables are in memory.py), what the prompt says of them, their tools
  ingest.py     the text of uploaded files: text, code, PDF, .docx, .zip; what is left out
  github.py     downloads a snapshot of a GitHub repository
  projectapi.py the routes of projects
  integrations/ GitHub, Google Drive and folders that Clara works on live: store.py (tables), permissions.py, vault.py
                (encrypted secrets), broker.py (every call is checked here), approvals.py (requests for permission,
                pushing, follow-up), service.py, and connectors/ (github_live, gdrive, serverfs, computer)
  integrationapi.py the routes of integrations and approvals
  tools.py      tools the model can call
  reminders.py  reminders: parsing, repeats, the scheduler
  tasks.py      the to-do list: tasks, their reminders, the rules, the scheduler (storage: taskstore.py)
  taskai.py     Clara picks the reminders of a task and decides the next ones at each reminder
  taskapi.py    the routes of tasks
  schedule.py   scheduled tasks: prompts Clara runs by herself, once or as a routine, and the Discord summary
  scheduleapi.py the routes of scheduled tasks
  users.py      users, password hashes, login tokens (the tables are in memory.py)
  auth.py       who is calling (client token, user token or web cookie) and what they may touch
  webapi.py     login, sign-up, account, administration and PDF routes, and the web site's files
  clientapi.py  what a client of many people (the Discord bot) uses: signing accounts in, spaces; chime in, the
                relationship and the Discord page for administrators
  discord_bot/  the Discord bot: service.py runs it in the server (local.py: direct calls), standalone.py on its
                own (remote.py: HTTP); see its __init__.py
  web/          the web site: index.html, favicon.ico, style.css and ES modules (chat, memory, account, admin, markdown...)
  session.py    for clara-chat / clara-admin: sign in once, keep the token
  tailscale.py  publishes the server with `tailscale serve|funnel`, removes it on exit
  ratelimit.py  refuses an address that sends too many wrong tokens
  notifications.py  notifications, and each listener's stream of events (reminders, notifications, server state)
  announce.py   Clara writes the announcement of a reminder that came due
  traffic.py    the traffic log: every request in and out, in data/logs
  prompt.py     personality file + per-request context
  agent.py      one conversation turn (locks, server and client tools, streaming, storage, compaction), titles
  compaction.py transcript and summary request for long conversations
  commands.py   the console commands (/provider, /status...), shared by both consoles
  console.py    the interactive prompt (history, completion)
  headless.py   `--headless`: ignore SIGHUP, leave the terminal, log to a rotating file
  selftest.py   `--test`: the checks run before the server starts
  server.py     FastAPI routes, auth, embedded console
  limits.py     the daily credits of each person: counting, who has which limit, the refusal
  models.py     the models users may choose, their weights (credits a token), each person's choice, Discord's model
  modelapi.py   the routes of models
  markdownfiles.py / markdownapi.py  the markdown files Clara writes (tools in tools.py), and their routes
  qcm.py        the QCM form Clara asks (tool `qcm`): limits, checking, the answers message
  client.py     clara-chat (also shows reminders and notifications, and has /remind, /notify, /tasks and /task)
  admin.py      clara-admin (remote console)
config/system_prompt.md   Clara's personality, re-read when edited
```

## Next steps

- Semantic recall of facts (embeddings).
- Integrations: merging pull requests with their checks, Drive sharing, Docs/Sheets editing as documents, a shell on the
  computer (only reading and file changes are there for now).
- Semantic search in projects (embeddings), for the projects too big to be read whole.
- Scheduled / proactive tasks.
