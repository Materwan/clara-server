"""Users with a password, and the tokens they get by logging in.

A user is a login name, a password (kept only as a salted scrypt hash) and the *person* Clara knows them as.
Logging in on a surface (`web`, `app`, `cli`...) gives a token that is bound to that user and that surface:
the server then knows who is speaking, instead of believing whatever `user_id` a client claims. Tokens are
random, only their SHA-256 is stored, each can be revoked (sign out) and one unused for `session_days`
days stops working.

The tables are in `memory.py` (so that erasing or merging a person keeps them consistent); this is the code.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .memory import Memory, Person

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
MIN_PASSWORD = 10
MAX_PASSWORD = 256
TOKEN_PREFIX = "clu_"  # tells a user token from a client token (CLARA_TOKENS) at a glance
TOUCH_SECONDS = 600  # `last_used_at` is not written more often than this
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class UserError(ValueError):
    """The operator or user asked for something impossible; the text says what."""


@dataclass(frozen=True)
class User:
    name: str
    person_id: int
    is_admin: bool
    disabled: bool
    created_at: str
    last_login_at: str | None


@dataclass(frozen=True)
class Session:
    id: int
    user: str
    surface: str
    device: str
    address: str
    created_at: str
    last_used_at: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **SCRYPT)
    return "scrypt${n}${r}${p}${salt}${digest}".format(
        **SCRYPT, salt=base64.b64encode(salt).decode(), digest=base64.b64encode(digest).decode()
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=len(expected),
            maxmem=128 * int(n) * int(r) * 2 + 2**20,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # checked when the user is unknown: same time as a real one


def generate_password() -> str:
    return secrets.token_urlsafe(12)  # 16 characters


def check_name(name: str) -> str:
    """The user name as stored (trimmed, lower case), or a UserError."""
    name = name.strip().lower()
    if not NAME_RE.match(name):
        raise UserError("A user name has 1 to 32 characters: a-z, 0-9, '.', '_' or '-', not starting with a symbol.")
    return name


def check_password_rules(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise UserError(f"The password needs at least {MIN_PASSWORD} characters.")
    if len(password) > MAX_PASSWORD:
        raise UserError(f"The password is too long (at most {MAX_PASSWORD} characters).")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Users:
    def __init__(self, memory: Memory, session_days: int = 90, clock: Callable[[], datetime] = _now):
        self._memory = memory
        self.session_days = session_days  # 0: tokens never expire
        self._clock = clock

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    @staticmethod
    def _user(row) -> User:
        return User(
            row["name"], row["person_id"], bool(row["is_admin"]), bool(row["disabled"]), row["created_at"],
            row["last_login_at"],
        )

    def get(self, name: str) -> User | None:
        with self._memory.lock:
            row = self._memory.database.execute("SELECT * FROM users WHERE name = ?", (name.strip().lower(),)).fetchone()
        return self._user(row) if row else None

    def list(self) -> list[User]:
        with self._memory.lock:
            rows = self._memory.database.execute("SELECT * FROM users ORDER BY name").fetchall()
        return [self._user(row) for row in rows]

    def admin_count(self) -> int:
        with self._memory.lock:
            return self._memory.database.execute(
                "SELECT COUNT(*) FROM users WHERE is_admin = 1 AND disabled = 0"
            ).fetchone()[0]

    def create(self, name: str, password: str, admin: bool = False, person: Person | None = None) -> User:
        """A new user. They are `person` if given, else the person already known by an account with this
        name (`cli:erwan`, `app:erwan`...), or a new one."""
        name = check_name(name)
        check_password_rules(password)
        db = self._memory.database
        with self._memory.lock, db:
            if db.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
                raise UserError(f"There already is a user called {name}.")
            if person is not None:
                person_id = person.id
            else:
                row = db.execute(
                    "SELECT person_id FROM accounts WHERE external_id = ? ORDER BY person_id LIMIT 1", (name,)
                ).fetchone()
                person_id = row["person_id"] if row else self._memory.resolve("web", name, name).id
            db.execute(
                "INSERT INTO users (name, person_id, password_hash, is_admin, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, person_id, hash_password(password), int(admin), _stamp(self._clock())),
            )
        return self.get(name)  # type: ignore[return-value]

    def register(self, name: str, password: str) -> User:
        """A user who made themselves (the web site's sign-up). Unlike `create`, they never take over the
        memories of an account that already has their name (`cli:erwan`, `discord:1234`...): anybody could
        claim them, so such a name is refused and the person is always a new one."""
        name = check_name(name)
        check_password_rules(password)
        with self._memory.lock:
            if self._memory.database.execute("SELECT 1 FROM accounts WHERE external_id = ?", (name,)).fetchone():
                raise UserError(f"The name {name} is already taken.")
            person = self._memory.create_person(name)
        try:
            return self.create(name, password, person=person)
        except UserError:
            self._memory.delete_person(person.id)
            raise

    def person_of(self, user: User) -> Person:
        person = self._memory.person_by_id(user.person_id)
        if person is None:  # cannot happen: the foreign key keeps it
            raise UserError("This user has no person.")
        return person

    def set_password(self, name: str, password: str, keep_session: int | None = None) -> int:
        """Change a password; every session but `keep_session` is signed out. Returns how many."""
        check_password_rules(password)
        self._must_exist(name)
        with self._memory.lock, self._memory.database as db:
            db.execute("UPDATE users SET password_hash = ? WHERE name = ?", (hash_password(password), name))
        return self.revoke_all(name, except_id=keep_session)

    def set_admin(self, name: str, admin: bool) -> None:
        user = self._must_exist(name)
        if user.is_admin and not admin and not user.disabled and self.admin_count() <= 1:
            raise UserError("This is the last administrator: make someone else one first.")
        with self._memory.lock, self._memory.database as db:
            db.execute("UPDATE users SET is_admin = ? WHERE name = ?", (int(admin), name))

    def set_disabled(self, name: str, disabled: bool) -> None:
        user = self._must_exist(name)
        if disabled and user.is_admin and not user.disabled and self.admin_count() <= 1:
            raise UserError("This is the last administrator: it cannot be disabled.")
        with self._memory.lock, self._memory.database as db:
            db.execute("UPDATE users SET disabled = ? WHERE name = ?", (int(disabled), name))
        if disabled:
            self.revoke_all(name)

    def delete(self, name: str) -> None:
        """Remove the login; the person, their facts and conversations stay."""
        user = self._must_exist(name)
        if user.is_admin and not user.disabled and self.admin_count() <= 1:
            raise UserError("This is the last administrator: it cannot be removed.")
        with self._memory.lock, self._memory.database as db:
            db.execute("DELETE FROM users WHERE name = ?", (name,))  # their sessions go with them

    def _must_exist(self, name: str) -> User:
        user = self.get(name)
        if user is None:
            raise UserError(f"No user called {name}.")
        return user

    # ------------------------------------------------------------------
    # Passwords and tokens
    # ------------------------------------------------------------------
    def authenticate(self, name: str, password: str) -> User | None:
        """The user, if the password is right (and the account not disabled); the same work is done
        for an unknown name, so the time does not say whether it exists."""
        user = self.get(name)
        stored = None
        if user is not None:
            with self._memory.lock:
                row = self._memory.database.execute("SELECT password_hash FROM users WHERE name = ?", (user.name,)).fetchone()
            stored = row["password_hash"] if row else None
        right = verify_password(password[:MAX_PASSWORD * 2], stored or _DUMMY_HASH)
        return user if (right and stored and user and not user.disabled) else None

    def open_session(self, user: User, surface: str, device: str = "", address: str = "") -> tuple[str, Session]:
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        now = _stamp(self._clock())
        with self._memory.lock, self._memory.database as db:
            created = db.execute(
                "INSERT INTO sessions (token_hash, user, surface, device, address, created_at, last_used_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_token_hash(token), user.name, surface, device[:80], address[:64], now, now),
            )
            db.execute("UPDATE users SET last_login_at = ? WHERE name = ?", (now, user.name))
            row = db.execute("SELECT * FROM sessions WHERE id = ?", (created.lastrowid,)).fetchone()
        return token, self._session(row)

    @staticmethod
    def _session(row) -> Session:
        return Session(
            row["id"], row["user"], row["surface"], row["device"], row["address"], row["created_at"],
            row["last_used_at"],
        )

    def lookup(self, token: str, touch: bool = True) -> tuple[User, Session] | None:
        """The user and session behind a token, or None (unknown, expired, user disabled)."""
        if not token.startswith(TOKEN_PREFIX):
            return None
        with self._memory.lock:
            row = self._memory.database.execute(
                "SELECT * FROM sessions WHERE token_hash = ?", (_token_hash(token),)
            ).fetchone()
        if row is None:
            return None
        session = self._session(row)
        now = self._clock()
        last = datetime.fromisoformat(session.last_used_at)
        if self.session_days and now - last > timedelta(days=self.session_days):
            return None
        user = self.get(session.user)
        if user is None or user.disabled:
            return None
        if touch and (now - last).total_seconds() > TOUCH_SECONDS:
            with self._memory.lock, self._memory.database as db:
                db.execute("UPDATE sessions SET last_used_at = ? WHERE id = ?", (_stamp(now), session.id))
        return user, session

    def sessions_of(self, name: str) -> list[Session]:
        with self._memory.lock:
            rows = self._memory.database.execute(
                "SELECT * FROM sessions WHERE user = ? ORDER BY last_used_at DESC", (name,)
            ).fetchall()
        return [self._session(row) for row in rows]

    def revoke_session(self, name: str, session_id: int) -> bool:
        with self._memory.lock, self._memory.database as db:
            return db.execute("DELETE FROM sessions WHERE id = ? AND user = ?", (session_id, name)).rowcount > 0

    def revoke_all(self, name: str, except_id: int | None = None) -> int:
        """Sign a user out of every device but `except_id`, and out of the accounts clients signed in for
        them (Discord). Returns how many."""
        with self._memory.lock, self._memory.database as db:
            sessions = db.execute("DELETE FROM sessions WHERE user = ? AND id IS NOT ?", (name, except_id)).rowcount
            return sessions + db.execute("DELETE FROM account_logins WHERE user = ?", (name,)).rowcount

    # ------------------------------------------------------------------
    # Accounts a client signed in (CLARA_LOGIN_SURFACES)
    # ------------------------------------------------------------------
    # A client that speaks for many people (the Discord bot) has no token per person: it sends their
    # password once, and from then on the account (`discord:1234`) is *signed in* as that user until it
    # signs out, the user is signed out everywhere, disabled or removed.
    def sign_in_account(self, user: User, surface: str, external_id: str, client: str = "") -> None:
        with self._memory.lock, self._memory.database as db:
            db.execute(
                "INSERT INTO account_logins (surface, external_id, user, client, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (surface, external_id) DO UPDATE SET user = excluded.user, client = excluded.client,"
                " created_at = excluded.created_at",
                (surface, external_id, user.name, client, _stamp(self._clock())),
            )
            db.execute("UPDATE users SET last_login_at = ? WHERE name = ?", (_stamp(self._clock()), user.name))

    def sign_out_account(self, surface: str, external_id: str) -> bool:
        with self._memory.lock, self._memory.database as db:
            return db.execute(
                "DELETE FROM account_logins WHERE surface = ? AND external_id = ?", (surface, external_id)
            ).rowcount > 0

    def account_user(self, surface: str, external_id: str) -> User | None:
        """The user an account is signed in as, or None (not signed in, or the user is disabled)."""
        with self._memory.lock:
            row = self._memory.database.execute(
                "SELECT u.* FROM account_logins l JOIN users u ON u.name = l.user"
                " WHERE l.surface = ? AND l.external_id = ? AND u.disabled = 0",
                (surface, external_id),
            ).fetchone()
        return self._user(row) if row else None

    def signed_in_accounts(self, surface: str) -> dict[str, User]:
        """Every account of a surface that is signed in (external id -> user), disabled users left out."""
        with self._memory.lock:
            rows = self._memory.database.execute(
                "SELECT l.external_id, u.* FROM account_logins l JOIN users u ON u.name = l.user"
                " WHERE l.surface = ? AND u.disabled = 0",
                (surface,),
            ).fetchall()
        return {row["external_id"]: self._user(row) for row in rows}

    def accounts_signed_in_as(self, name: str) -> list[str]:
        """The accounts signed in as a user ("discord:1234")."""
        with self._memory.lock:
            rows = self._memory.database.execute(
                "SELECT surface, external_id FROM account_logins WHERE user = ? ORDER BY surface, external_id", (name,)
            ).fetchall()
        return [f"{row['surface']}:{row['external_id']}" for row in rows]

    def prune(self) -> int:
        """Forget the sessions that have expired."""
        if not self.session_days:
            return 0
        oldest = _stamp(self._clock() - timedelta(days=self.session_days))
        with self._memory.lock, self._memory.database as db:
            return db.execute("DELETE FROM sessions WHERE last_used_at < ?", (oldest,)).rowcount
