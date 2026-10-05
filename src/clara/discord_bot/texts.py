"""What the bot itself says (Clara's answers come from the server, in the language of the person).

French or English, after the Discord language of the person: the one of their last command, or the server's
preferred language for a plain message. Everything else is English.
"""

from __future__ import annotations

FRENCH, ENGLISH = "fr", "en"

TEXTS: dict[str, dict[str, str]] = {
    # -- talking --------------------------------------------------------------------------------------
    "need_account": {
        FRENCH: "Pour parler avec moi, crée d'abord un compte avec `/register` (ou connecte-toi avec `/login`).",
        ENGLISH: "To talk with me, first create an account with `/register` (or sign in with `/login`).",
    },
    "use_slash": {
        FRENCH: "Mes commandes sont des commandes slash : tape `/` puis choisis-en une (par exemple `/help`).",
        ENGLISH: "My commands are slash commands: type `/` and pick one (for instance `/help`).",
    },
    "unreachable": {
        FRENCH: "Je n'arrive pas à joindre mon serveur pour le moment. Réessaie dans un instant.",
        ENGLISH: "I cannot reach my server right now. Try again in a moment.",
    },
    "stopping": {
        FRENCH: "Je suis en train de m'arrêter. Réessaie dans un instant.",
        ENGLISH: "I am shutting down. Try again in a moment.",
    },
    "too_long": {
        FRENCH: "Ce message est trop long pour moi.",
        ENGLISH: "That message is too long for me.",
    },
    "busy": {
        FRENCH: "Trop de demandes d'un coup : réessaie dans un instant.",
        ENGLISH: "Too many requests at once: try again in a moment.",
    },
    "failed": {
        FRENCH: "Désolée, je n'ai pas réussi à répondre ({detail}).",
        ENGLISH: "Sorry, I could not answer ({detail}).",
    },
    # -- accounts -------------------------------------------------------------------------------------
    "register_title": {FRENCH: "Créer un compte Clara", ENGLISH: "Create a Clara account"},
    "login_title": {FRENCH: "Se connecter à Clara", ENGLISH: "Sign in to Clara"},
    "username": {FRENCH: "Nom d'utilisateur", ENGLISH: "User name"},
    "username_hint": {FRENCH: "minuscules, chiffres, . _ -", ENGLISH: "lowercase letters, digits, . _ -"},
    "password": {FRENCH: "Mot de passe (10 caractères ou plus)", ENGLISH: "Password (10 characters or more)"},
    "password_hint": {
        FRENCH: "Discord ne masque pas ce champ : attention à ton écran",
        ENGLISH: "Discord does not hide this field: mind your screen",
    },
    "password_again": {FRENCH: "Confirme le mot de passe", ENGLISH: "Password again"},
    "password_mismatch": {
        FRENCH: "Les deux mots de passe sont différents. Recommence avec `/register`.",
        ENGLISH: "The two passwords differ. Try `/register` again.",
    },
    "registered": {
        FRENCH: "Compte **{user}** créé, et tu es connecté. Le même compte marche sur le site web et l'application Clara.",
        ENGLISH: "Account **{user}** created, and you are signed in. The same account works on Clara's web site and app.",
    },
    "logged_in": {FRENCH: "Connecté en tant que **{user}**.", ENGLISH: "Signed in as **{user}**."},
    "wrong_password": {
        FRENCH: "Nom d'utilisateur ou mot de passe incorrect.",
        ENGLISH: "Wrong user name or password.",
    },
    "refused": {FRENCH: "Refusé : {detail}", ENGLISH: "Refused: {detail}"},
    "logged_out": {
        FRENCH: "Tu es déconnecté. Ce que je sais de toi reste attaché à ton compte.",
        ENGLISH: "You are signed out. What I know about you stays with your account.",
    },
    "not_logged_in": {FRENCH: "Tu n'étais pas connecté.", ENGLISH: "You were not signed in."},
    # -- /me ------------------------------------------------------------------------------------------
    "me_title": {FRENCH: "Ton compte Clara", ENGLISH: "Your Clara account"},
    "me_user": {FRENCH: "Utilisateur", ENGLISH: "User"},
    "me_accounts": {FRENCH: "Comptes liés", ENGLISH: "Linked accounts"},
    "me_relation": {FRENCH: "Relation", ENGLISH: "Relationship"},
    "me_relation_none": {FRENCH: "pas encore", ENGLISH: "none yet"},
    "me_facts": {FRENCH: "Ce que je retiens de toi", ENGLISH: "What I remember about you"},
    "me_no_facts": {FRENCH: "Rien pour l'instant.", ENGLISH: "Nothing yet."},
    "me_more_facts": {FRENCH: "… et {count} de plus", ENGLISH: "… and {count} more"},
    "me_forget_placeholder": {FRENCH: "Oublier un souvenir…", ENGLISH: "Forget something…"},
    # -- memory and conversations ---------------------------------------------------------------------
    "remembered": {FRENCH: "C'est noté.", ENGLISH: "Noted."},
    "already_known": {FRENCH: "Je le savais déjà.", ENGLISH: "I already knew that."},
    "forgotten": {FRENCH: "Oublié : {text}", ENGLISH: "Forgotten: {text}"},
    "no_such_fact": {
        FRENCH: "Je n'ai pas ce souvenir-là (vois `/me`).",
        ENGLISH: "I have no such memory (see `/me`).",
    },
    "reset_done": {
        FRENCH: "Conversation effacée ({count} messages). Ce que je sais de chacun est gardé.",
        ENGLISH: "Conversation cleared ({count} messages). What I know about each person is kept.",
    },
    "reset_admins_only": {
        FRENCH: "Sur un serveur, seuls ceux qui peuvent gérer le serveur effacent la conversation d'un salon.",
        ENGLISH: "On a server, only people who can manage it may clear a channel's conversation.",
    },
    # -- the to-do list -------------------------------------------------------------------------------
    "tasks_title": {FRENCH: "Tes tâches", ENGLISH: "Your tasks"},
    "tasks_done_title": {FRENCH: "Tes tâches terminées", ENGLISH: "Your finished tasks"},
    "tasks_none": {
        FRENCH: "Aucune tâche. Demande-moi d'en ajouter une, par exemple : « ajoute une tâche : envoyer la facture ».",
        ENGLISH: "No task. Ask me to add one, for instance: \"add a task: send the invoice\".",
    },
    "tasks_more": {FRENCH: "… et {count} de plus", ENGLISH: "… and {count} more"},
    "tasks_footer": {
        FRENCH: "`/task` donne le détail d'une tâche. Dis-moi ce qu'il faut changer.",
        ENGLISH: "`/task` shows one task in full. Tell me what to change.",
    },
    "task_due": {FRENCH: "à faire pour {when}", ENGLISH: "due {when}"},
    "task_done": {FRENCH: "terminée", ENGLISH: "done"},
    "task_sent_one": {FRENCH: "1 rappel envoyé", ENGLISH: "1 reminder sent"},
    "task_sent": {FRENCH: "{count} rappels envoyés", ENGLISH: "{count} reminders sent"},
    "task_next": {FRENCH: "prochain rappel {when}", ENGLISH: "next reminder {when}"},
    "task_no_next": {FRENCH: "plus de rappel prévu", ENGLISH: "no reminder to come"},
    "task_description": {FRENCH: "Description", ENGLISH: "Description"},
    "task_no_description": {FRENCH: "aucune", ENGLISH: "none"},
    "task_reminders": {FRENCH: "Rappels à venir", ENGLISH: "Reminders to come"},
    "no_such_task": {
        FRENCH: "Je n'ai pas cette tâche (vois `/tasks`).",
        ENGLISH: "I have no such task (see `/tasks`).",
    },
    # -- help -----------------------------------------------------------------------------------------
    "help": {
        FRENCH: (
            "**Clara** : mentionne-moi, réponds à un de mes messages ou écris-moi en privé.\n"
            "`/register` crée ton compte (le même que sur le site web), `/login` te connecte à un compte "
            "existant, `/logout` te déconnecte.\n"
            "`/me` montre ce que je sais de toi, `/remember` me fait retenir quelque chose, `/forget` me le fait "
            "oublier, `/reset` efface la conversation du salon (ou de nos messages privés).\n"
            "`/tasks` montre ta liste de tâches (demande-moi d'ajouter, de changer ou de finir une tâche), `/task` "
            "le détail de l'une d'elles.\n"
            "Tes rappels arrivent en message privé."
        ),
        ENGLISH: (
            "**Clara**: mention me, reply to one of my messages or write to me privately.\n"
            "`/register` makes your account (the same as on the web site), `/login` signs in to an existing one, "
            "`/logout` signs you out.\n"
            "`/me` shows what I know about you, `/remember` makes me remember something, `/forget` makes me "
            "forget it, `/reset` clears the channel's conversation (or our private one).\n"
            "`/tasks` shows your to-do list (ask me to add, change or finish a task), `/task` shows one in full.\n"
            "Your reminders arrive as private messages."
        ),
    },
    # -- private messages from the server -------------------------------------------------------------
    "reminder": {FRENCH: "⏰ **Rappel** : {text}", ENGLISH: "⏰ **Reminder**: {text}"},
    "notification": {FRENCH: "🔔 {text}", ENGLISH: "🔔 {text}"},
    # -- requests for permission (approvals.py) -----------------------------------------------------------
    "approval_ask": {
        FRENCH: "🛡️ **J'ai besoin de ta permission**\n{summary}\n_Sur {resource} · {level}_{reason}\nRien n'est fait tant que tu n'as pas approuvé.",
        ENGLISH: "🛡️ **I need your permission**\n{summary}\n_On {resource} · {level}_{reason}\nNothing is done until you approve.",
    },
    "approval_reason": {FRENCH: "\nJe dis : « {reason} »", ENGLISH: "\nI say: “{reason}”"},
    "approval_approve": {FRENCH: "Approuver", ENGLISH: "Approve"},
    "approval_deny": {FRENCH: "Refuser", ENGLISH: "Deny"},
    "approval_done": {FRENCH: "✅ Approuvé et fait : {summary}\n{result}", ENGLISH: "✅ Approved and done: {summary}\n{result}"},
    "approval_failed": {
        FRENCH: "⚠️ Approuvé, mais cela a échoué : {summary}\n{result}",
        ENGLISH: "⚠️ Approved, but it failed: {summary}\n{result}",
    },
    "approval_denied": {FRENCH: "⛔ Refusé : {summary}. Rien n'a été fait.", ENGLISH: "⛔ Denied: {summary}. Nothing was done."},
    "approval_expired": {FRENCH: "⌛ Expiré : {summary}. Rien n'a été fait.", ENGLISH: "⌛ Expired: {summary}. Nothing was done."},
    "approval_late": {FRENCH: "Cette demande a déjà été traitée.", ENGLISH: "This request was already answered."},
    "approval_unknown": {FRENCH: "Je ne retrouve pas cette demande.", ENGLISH: "I cannot find that request."},
    "approval_error": {
        FRENCH: "Je n'ai pas pu traiter ta réponse ({detail}).",
        ENGLISH: "I could not process your answer ({detail}).",
    },
    "level_read": {FRENCH: "lecture", ENGLISH: "look"},
    "level_write": {FRENCH: "ajout ou modification", ENGLISH: "add or change"},
    "level_destructive": {FRENCH: "remplacement ou suppression", ENGLISH: "replace or delete"},
}


def language(locale: object | None) -> str:
    """"fr" for any French Discord locale, else "en"."""
    value = str(getattr(locale, "value", locale) or "")
    return FRENCH if value.lower().startswith("fr") else ENGLISH


def t(lang: str, key: str, **values: object) -> str:
    text = TEXTS[key].get(lang) or TEXTS[key][ENGLISH]
    return text.format(**values) if values else text
