# Inkly Desktop: install or update full Inkly on this HPC account.
#
# Inkly Desktop sends this script inside its SSH command and sets:
#   C  the commit to install (40 hex digits)   V  its Inkly version
#   R  the repository URL                      M  auto | repair
#   S  1 keeps the configured shared-logging default; 0 turns shared logging
#      off in a new ~/.inkly/config.toml (an existing one is never changed)
# It runs the repository's own setup.sh, exactly like a manual install, and
# prints one "INKLY-SETUP {json}" line per step. When it finishes, the same
# connection continues as a normal Desktop session. It never deletes,
# downgrades or overwrites anything, runs no Slurm commands and needs no
# administrator rights. A full log is kept in ~/.inkly/logs.
# Messages show ~ paths to the user; "a && b || fail" means fail if either fails.
# shellcheck disable=SC2088,SC2015
set -uo pipefail
export LC_ALL=C GIT_TERMINAL_PROMPT=0
INKLY=$HOME/.inkly
SRC=$INKLY/src/hpc-ink-setup
INK=$INKLY/bin/ink
PYPI=${INKLY_SETUP_PYPI:-https://pypi.org/simple/pip/}
STAGE=preflight
LOG=
tmp=

json() {
    local s
    s=$(printf '%s' "$1" | tr -d '\000-\037')
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    printf '"%s"' "$s"
}
frame() {
    printf 'INKLY-SETUP %s\n' "$1"
    if [ -n "$LOG" ]; then printf 'INKLY-SETUP %s\n' "$1" >>"$LOG"; fi
}
say() { frame "{\"stage\":$(json "$STAGE"),\"line\":$(json "${1:0:400}")}"; }
fail() {
    local message=$1
    if [ -n "$LOG" ]; then message="$message Details: ~/.inkly/logs/${LOG##*/}"; fi
    frame "{\"done\":true,\"ok\":false,\"stage\":$(json "$STAGE"),\"error\":$(json "$message")}"
    exit 1
}
# Only the main shell cleans up: bash also runs this trap in a subshell that
# fails, for example on an unset variable inside a pipeline, and there the
# first test of an "&&" list can be ignored. Return first, then test.
cleanup() {
    if [ "$BASHPID" != "$$" ]; then return 0; fi
    if [ -n "$tmp" ] && [ -d "$tmp" ]; then rm -rf -- "$tmp"; fi
}
trap cleanup EXIT
newer() { [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -n 1)" = "$1" ]; }
version_in() { sed -n 's/^__version__ = "\([^"]*\)".*/\1/p' | head -n 1; }
same_repo() {
    local a=${1%/} b=${2%/}
    a=${a%.git} b=${b%.git}
    [ "$(printf '%s' "$a" | tr '[:upper:]' '[:lower:]')" = "$(printf '%s' "$b" | tr '[:upper:]' '[:lower:]')" ]
}
log() { "$@" >>"$LOG" 2>&1 </dev/null; }

# 1. Preflight: checks only, nothing is created yet.
[[ ${C:-} =~ ^[0-9a-f]{40}$ ]] || fail "Desktop sent an invalid commit."
[[ ${V:-} =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "Desktop sent an invalid version."
[[ ${M:-} == auto || ${M:-} == repair ]] || fail "Desktop sent an invalid setup mode."
[[ ${S:-} == 0 || ${S:-} == 1 ]] || fail "Desktop sent an invalid shared-logging choice."
[ -n "${R:-}" ] || fail "Desktop sent no repository."
[ "${BASH_VERSINFO[0]}" -ge 4 ] || fail "bash 4 or newer is needed (found $BASH_VERSION)."
for tool in git python3 flock timeout sort sed awk grep tr od mktemp df date; do
    command -v "$tool" >/dev/null 2>&1 || fail "The command '$tool' is not available on this HPC login node."
done
say "bash $BASH_VERSION; $(git --version </dev/null 2>&1)"
py=$(python3 -c 'import sys, venv, ensurepip; print("%d.%d.%d" % sys.version_info[:3])' </dev/null 2>&1 | tail -n 1)
python3 -c 'import sys, venv, ensurepip; raise SystemExit(sys.version_info < (3, 9))' </dev/null >/dev/null 2>&1 ||
    fail "Python 3.9 or newer with the venv and ensurepip modules is needed (python3 gave: $py)."
say "python3 $py with venv"
free=$(df -Pk "$HOME" </dev/null 2>/dev/null | awk 'NR == 2 {print $4}')
[[ $free =~ ^[0-9]+$ ]] && [ "$free" -ge 1048576 ] ||
    fail "At least 1 GB of free space is needed in your home folder (free: ${free:-unknown} KB)."
say "$((free / 1048576)) GB free in your home folder"
timeout 60 git ls-remote "$R" HEAD </dev/null >/dev/null 2>&1 ||
    fail "Cannot reach the Inkly repository ($R) from this login node."
timeout 30 python3 -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=20)' "$PYPI" </dev/null >/dev/null 2>&1 ||
    fail "Cannot reach the Python package index (pypi.org) from this login node."
say "GitHub and the Python package index are reachable"

# 2. One setup at a time per account.
STAGE=lock
mkdir -p "$INKLY/logs" "$INKLY/src" || fail "Cannot create ~/.inkly."
LOG=$INKLY/logs/desktop-setup-$(date -u +%Y%m%dT%H%M%SZ)-$$.log
: >>"$LOG" || { LOG=; fail "Cannot write a log in ~/.inkly/logs."; }
{ exec 9>>"$INKLY/desktop-setup.lock"; } 2>/dev/null || fail "Cannot create ~/.inkly/desktop-setup.lock."
flock -n 9 || fail "Another Inkly setup is already running on this account. Wait for it to finish, then try again."
say "Inkly $V, commit ${C:0:12}, mode $M"

# 3. Check what is installed, then get the code. Never downgrade or overwrite.
STAGE=code
installed=$(version_in 2>/dev/null <"$INKLY/lib/inkly/__init__.py")
if [ -n "$installed" ] && newer "$installed" "$V"; then
    fail "This HPC account has Inkly $installed, newer than this Desktop ($V). Update Inkly Desktop instead; nothing was changed."
fi
at=
if [ -e "$SRC" ] || [ -L "$SRC" ]; then
    top=$(git -C "$SRC" rev-parse --show-toplevel 2>/dev/null </dev/null)
    [ -d "$SRC" ] && [ ! -L "$SRC" ] && [ "$top" = "$(cd "$SRC" && pwd -P)" ] ||
        fail "~/.inkly/src/hpc-ink-setup exists but is not an Inkly checkout. Move it aside, then try again."
    origin=$(git -C "$SRC" remote get-url origin 2>/dev/null </dev/null)
    same_repo "$origin" "$R" ||
        fail "~/.inkly/src/hpc-ink-setup is a checkout of a different repository ($origin). Move it aside, then try again."
    [ -z "$(git -C "$SRC" status --porcelain 2>&1 </dev/null)" ] ||
        fail "~/.inkly/src/hpc-ink-setup has local changes. Nothing was changed; commit or move them, then try again."
    at=$(git -C "$SRC" rev-parse HEAD 2>/dev/null </dev/null)
    current=$(git -C "$SRC" show HEAD:inkly/__init__.py 2>/dev/null </dev/null | version_in)
    if [ -n "$current" ] && newer "$current" "$V"; then
        fail "~/.inkly/src/hpc-ink-setup has Inkly $current, newer than this Desktop ($V). Update Inkly Desktop instead; nothing was changed."
    fi
fi
changed=1
if [ "$M" = auto ] && [ "$at" = "$C" ] && [ "$installed" = "$V" ] && [ -x "$INK" ]; then
    changed=0
    say "Inkly $V is already installed; nothing to change"
elif [ "$at" = "$C" ]; then
    say "The checkout is already at commit ${C:0:12}"
elif [ -n "$at" ]; then
    say "Updating ~/.inkly/src/hpc-ink-setup to Inkly $V"
    if ! git -C "$SRC" cat-file -e "$C^{commit}" 2>/dev/null </dev/null; then
        depth=(--depth 1)
        [ "$(git -C "$SRC" rev-parse --is-shallow-repository 2>/dev/null </dev/null)" = true ] || depth=()
        log git -C "$SRC" fetch -q "${depth[@]}" origin "$C" ||
            fail "Could not download commit ${C:0:12} from $R. If this Desktop runs from source, push that commit first."
    fi
    [ "$(git -C "$SRC" show "$C:inkly/__init__.py" 2>/dev/null </dev/null | version_in)" = "$V" ] ||
        fail "Commit ${C:0:12} is not Inkly $V; nothing was changed."
    log git -C "$SRC" -c advice.detachedHead=false checkout -q --detach "$C" ||
        fail "Could not switch ~/.inkly/src/hpc-ink-setup to commit ${C:0:12}."
else
    say "Downloading Inkly $V into ~/.inkly/src/hpc-ink-setup"
    tmp=$(mktemp -d "$INKLY/src/.hpc-ink-setup.XXXXXX") || fail "Cannot create a temporary folder in ~/.inkly/src."
    log git -C "$tmp" init -q && log git -C "$tmp" remote add origin "$R" ||
        fail "Could not prepare the download folder."
    log git -C "$tmp" fetch -q --depth 1 origin "$C" ||
        fail "Could not download commit ${C:0:12} from $R. If this Desktop runs from source, push that commit first."
    log git -C "$tmp" -c advice.detachedHead=false checkout -q --detach FETCH_HEAD ||
        fail "Could not check out commit ${C:0:12}."
    [ "$(git -C "$tmp" rev-parse HEAD 2>/dev/null </dev/null)" = "$C" ] ||
        fail "The download did not match commit ${C:0:12}; nothing was installed."
    [ "$(version_in 2>/dev/null <"$tmp/inkly/__init__.py")" = "$V" ] ||
        fail "Commit ${C:0:12} is not Inkly $V; nothing was installed."
    mv -T "$tmp" "$SRC" || fail "Could not move the download into place."
    tmp=
fi
[ "$(git -C "$SRC" rev-parse HEAD 2>/dev/null </dev/null)" = "$C" ] ||
    fail "~/.inkly/src/hpc-ink-setup is not at commit ${C:0:12}."

# 4. Install with the repository's own setup.sh, as a manual install does.
STAGE=install
if [ "$changed" = 1 ]; then
    if [ "$S" = 0 ] && [ ! -e "$INKLY/config.toml" ]; then
        awk '/^\[/ {section = $0}
             section == "[logging.shared]" && /^enabled[ \t]*=/ && !done {print "enabled = false"; done = 1; next}
             {print}' "$SRC/config.toml" >"$INKLY/.config.toml.new" &&
            grep -q '^enabled = false$' "$INKLY/.config.toml.new" &&
            mv "$INKLY/.config.toml.new" "$INKLY/config.toml" ||
            fail "Could not write ~/.inkly/config.toml with shared logging turned off."
        say "Shared logging is turned off in the new ~/.inkly/config.toml"
    fi
    say "Running setup.sh (this downloads Python packages; it can take a few minutes)"
    (cd "$SRC" && exec bash ./setup.sh) </dev/null 2>&1 | while IFS= read -r line || [ -n "$line" ]; do
        printf '%s\n' "$line" >>"$LOG"
        say "$line"
    done
    status=${PIPESTATUS[0]}
    [ "$status" -eq 0 ] ||
        fail "Inkly's setup.sh stopped with exit code $status. Nothing was deleted; running setup again is safe."
fi

# 5. Check the installed launcher answers as this Desktop's version.
STAGE=verify
bundle=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
reply=$(printf '{"action":"version","bundle":"%s"}\n' "$bundle" | "$INK" tensor 2>>"$LOG")
got=$(printf '%s' "$reply" | python3 -c '
import json, sys
try:
    r = json.loads(sys.stdin.read())
    ok = r.get("ok") is True and r.get("bundle") == sys.argv[1]
    print(r.get("version", "") if ok else "")
except ValueError:
    print("")' "$bundle" 2>>"$LOG")
[ "$got" = "$V" ] || fail "The installed ~/.inkly/bin/ink did not report Inkly $V (got: ${got:-no answer})."
say "~/.inkly/bin/ink reports Inkly $V"

# 6. Hand this connection to the installed launcher.
STAGE=session
frame "{\"done\":true,\"ok\":true,\"version\":$(json "$V"),\"commit\":$(json "$C"),\"changed\":$([ "$changed" = 1 ] && echo true || echo false),\"log\":$(json "~/.inkly/logs/${LOG##*/}")}"
trap - EXIT
exec 9>&-
exec "$INK" tensor --session
