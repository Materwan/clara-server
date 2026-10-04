"""Clara on Discord. One bot, two ways to run it:

* inside clara-server (service.py): started with the server (AUTO_START_DISCORD_BOT) or by an administrator, it
  reaches Clara through direct calls (local.py);
* on its own (standalone.py, `clara-discord`): it reaches a Clara server over HTTP (remote.py).

Needs discord.py: `pip install clara-server[discord]`. Only service.py and standalone.py load without it.

    routing.py    which messages are answered, observed or ignored (no Discord in it)
    handler.py    a message: what Clara is given (roster, people mentioned, the replied-to message), her answer
    commands.py   slash commands, sign-in forms, /me
    events.py     reminders and notifications -> private messages
    accounts.py   who is signed in (a copy of the server's), the language of each person
    mentions.py   <@id> -> @Name, @Name -> pings, long answers split
    texts.py      the bot's own texts, in French and English
    backend.py    what the bot needs from Clara; local.py and remote.py provide it
    bot.py        the Discord client
"""
