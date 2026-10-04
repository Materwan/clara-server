#!/usr/bin/env bash
# Update Clara and run it: git pull, refresh the venv, bring .env up to date with .env.example
# (keeping your values), then start the server. Extra arguments go to clara-server.
#
#   ./update-and-run.sh [clara-server options]
#   PYTHON=python3.12 ./update-and-run.sh      # a specific Python (3.11 or newer) for a new venv

set -euo pipefail

# Everything is in a function, called on the last line: `git pull` may rewrite this very file,
# and bash reads a script as it runs, so nothing may be read from it after `main` starts.
main() {
    cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

    echo "==> Pulling the repository"
    git pull --ff-only

    echo "==> Updating the virtual environment"
    local python=${PYTHON:-python3}
    if [ ! -x .venv/bin/python ]; then
        "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
            || { echo "Python 3.11 or newer is required (set PYTHON=...)." >&2; exit 1; }
        "$python" -m venv .venv \
            || { echo "Could not create .venv; on Ubuntu: sudo apt install python3-venv" >&2; exit 1; }
    fi
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet --upgrade -e ".[discord]"  # with the Discord bot

    echo "==> Updating .env"
    update_env

    echo "==> Starting clara-server"
    exec .venv/bin/clara-server "$@"
}

# Rebuild .env from .env.example, so that new options appear, with the value of every setting
# of the old .env. A setting the old .env has and the example no longer has is kept at the end.
# The old file is saved as .env.bak.<date> whenever the result differs from it.
update_env() {
    if [ ! -f .env.example ]; then
        echo "    no .env.example, .env left as it is"
        return
    fi
    if [ ! -s .env ]; then
        cp .env.example .env
        echo "    .env created from .env.example: put real tokens in it (the server refuses \"change-me\")"
        return
    fi

    local merged
    merged=$(mktemp)
    awk '
        FNR == NR {                       # the old .env
            sub(/\r$/, "")
            line = $0
            if (line ~ /^[ \t]*(export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*=/) {
                sub(/^[ \t]+/, "", line)
                sub(/^export[ \t]+/, "", line)
                key = line
                sub(/=.*/, "", key)
                if (!(key in old)) order[++count] = key
                old[key] = line           # the last one wins, as in dotenv
            }
            next
        }
        {                                 # .env.example: an option, set or commented out
            sub(/\r$/, "")
            probe = $0
            sub(/^[ \t]*#?[ \t]*/, "", probe)
            if (probe ~ /^[A-Za-z_][A-Za-z0-9_]*=/) {
                key = probe
                sub(/=.*/, "", key)
                if ((key in old) && !(key in done)) {
                    print old[key]
                    done[key] = 1
                    next
                }
            }
            print
        }
        END {
            for (i = 1; i <= count; i++) {
                key = order[i]
                if (key in done) continue
                if (!extra++) {
                    print ""
                    print "# Kept from the previous .env (not in .env.example any more)"
                }
                print old[key]
            }
        }
    ' .env .env.example > "$merged"

    if cmp -s .env "$merged"; then
        rm -f "$merged"
        echo "    .env already up to date"
        return
    fi
    local backup=.env.bak.$(date +%Y%m%d-%H%M%S)
    cp -p .env "$backup"
    cat "$merged" > .env       # not mv: keeps the permissions of .env
    rm -f "$merged"
    echo "    .env updated, your values kept (previous file: $backup)"
}

main "$@"
