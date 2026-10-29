#!/usr/bin/env bash
#
# Deploy / update the folio_libris_rtac stack on bibliotekarien-vps.
#
# Run as root (or via sudo). git and docker run as root; the checkout and the
# directory above it must be owned by root and writable only by root. Nothing
# runs as a personal account.
#
# Why root and not the `rtac` service user: root runs files from the checkout
# (this script via sudo, the compose files at boot). If rtac owned them, that
# user could write code that root then ran — and rtac was in the docker group,
# which is root in all but name. So rtac owns nothing and is not in the docker
# group (bibliotekarien-platform SECURITY_REVIEW.md, Risk 1; same change as
# mark-rounding, teaball, grib-ranker and tOPAC).
#
# Flow: ownership checks -> fetch -> show target -> [signature check] ->
# ff-only merge -> build + recreate -> wait until healthy -> [per-library
# validation].
#
# Fetching is kept apart from updating the working tree, so --check never
# touches it, and the target commit is printed before it is applied: a
# compromised remote is root on a shared VPS. The repo has no signed releases
# yet and deploys unsigned by default (REQUIRE_SIGNED=0) — turn the check on
# once releases are tagged with `git tag -s` (GIT_REF=v1.x REQUIRE_SIGNED=1).
#
# No backup step: the app keeps no state. The per-library settings under
# libraries/ are configuration that lives only on the server, and are not
# touched by a deploy.
#
#   sudo scripts/deploy.sh               # fetch, ff, build, recreate, wait healthy
#   sudo scripts/deploy.sh --check       # fetch + show target + compose config; applies nothing
#   sudo scripts/deploy.sh --validate    # ...then check each library's FOLIO/edge API
#
# Options:
#   --check, --dry-run   fetch and show what would be deployed, apply nothing
#   --no-pull            deploy the current checkout (skip fetch/merge)
#   --no-build           recreate without rebuilding the image
#   --validate           after deploy, check each library via /validate-folio-connection
#   --strict             with --validate, exit non-zero if any library check fails
#   --logs               follow the container log after a successful deploy
#   -h, --help           show this help
#
# Environment:
#   APP_DIR          checkout to deploy (default: /opt/rtac/app)
#   GIT_REMOTE       remote to fetch from (default: origin)
#   GIT_REF          tag (preferably signed) or branch to deploy (default: main)
#   REQUIRE_SIGNED   1 = require a valid GPG signature on the tag/commit (default: 0)
#   WAIT_TIMEOUT     seconds to wait for the container to be healthy (default: 120)
#
# docker compose runs without -f from the checkout, so a COMPOSE_FILE line in
# .env (the metrics overlay, see DEPLOY.md) is honoured on every deploy.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/rtac/app}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_REF="${GIT_REF:-main}"
REQUIRE_SIGNED="${REQUIRE_SIGNED:-0}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-120}"
SERVICE=rtac   # compose service name (== container_name)

check_only=0
do_pull=1
do_build=1
do_validate=0
do_logs=0
strict=0

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \?//'; }

for arg in "$@"; do
    case "$arg" in
        --check|--dry-run) check_only=1 ;;
        --no-pull)  do_pull=0 ;;
        --no-build) do_build=0 ;;
        --validate) do_validate=1 ;;
        --strict)   strict=1 ;;
        --logs)     do_logs=1 ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "Unknown option: $arg (see --help)" >&2; exit 2 ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run as root (or via sudo): sudo $0 $*" >&2
    exit 1
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
    echo "No git checkout at $APP_DIR (set APP_DIR, or see DEPLOY.md)." >&2
    exit 1
fi

# The directory above first: whoever can write there can swap out the whole
# checkout (mv app x; mkdir app), this script included.
parent=$(dirname "$APP_DIR")
if [[ -L "$APP_DIR" || "$(stat -c %u "$parent")" != 0 \
      || -n "$(find "$parent" -maxdepth 0 -perm /022)" ]]; then
    echo "$parent must be owned by root and not writable by others, and" >&2
    echo "$APP_DIR must not be a symlink. Fix (DEPLOY.md, \"Root-owned checkout\"):" >&2
    echo "  chown root:root $parent && chmod 755 $parent" >&2
    exit 1
fi

# Refuse to run if anyone but root can write in the checkout; otherwise root
# runs code a less privileged user could have changed. Group ownership is
# free: libraries/*/settings.json is root:10001 so the container can read it.
# Symlinks are always 777 and skipped; what counts is the target.
# `|| true`: when head stops reading, find gets SIGPIPE, and with pipefail the
# script would otherwise die silently here instead of saying what is wrong.
unsafe=$(find "$APP_DIR" ! -type l \( ! -user root -o -perm /022 \) -print | head -5 || true)
if [[ -n "$unsafe" ]]; then
    echo "The checkout $APP_DIR is not root-owned and write-protected, e.g.:" >&2
    echo "$unsafe" >&2
    echo "Fix as in DEPLOY.md, \"Root-owned checkout\"." >&2
    exit 1
fi

cd "$APP_DIR"

log() { printf '\n=> %s\n' "$*"; }

# --- 1. update the code -----------------------------------------------------

if [[ $do_pull -eq 1 ]]; then
    log "Fetching $GIT_REMOTE/$GIT_REF (working tree untouched)"
    git fetch --quiet --tags "$GIT_REMOTE"

    # A tag wins over a branch of the same name; peel it to its commit.
    if ! target="$(git rev-parse --verify --quiet "refs/tags/$GIT_REF^{commit}")" \
       && ! target="$(git rev-parse --verify --quiet "$GIT_REMOTE/$GIT_REF^{commit}")"; then
        echo "Neither tag nor branch '$GIT_REF' found on $GIT_REMOTE." >&2
        exit 1
    fi
    echo "   now:    $(git --no-pager log -1 --format='%h  %s' HEAD)"
    echo "   target: $(git --no-pager log -1 --format='%h  %an  %s' "$target")"
    git --no-pager log --format='     + %h  %s' "HEAD..$target"

    if [[ "$REQUIRE_SIGNED" == "1" ]]; then
        log "Verifying GPG signature"
        if ! git verify-tag "$GIT_REF" &>/dev/null \
           && ! git verify-commit "$target" &>/dev/null; then
            echo "ABORTING: '$GIT_REF' ($target) has no valid GPG signature." >&2
            echo "Sign the release (git tag -s), import the release key into" >&2
            echo "root's keyring, or set REQUIRE_SIGNED=0 if you trust the remote." >&2
            exit 1
        fi
    fi
fi

if [[ $check_only -eq 1 ]]; then
    log "Dry run: validating the compose configuration (current checkout)"
    docker compose config --quiet && echo "compose config OK"
    exit 0
fi

if [[ $do_pull -eq 1 ]]; then
    log "Updating the working tree (fast-forward only)"
    if ! git merge --ff-only --quiet "$target"; then
        echo "Fast-forward failed — local changes or a diverged branch in $APP_DIR." >&2
        exit 1
    fi
else
    log "Skipping fetch (--no-pull); deploying the current checkout"
fi

# --- 2. build + (re)create the container, wait until healthy ----------------

build_flag=()
[[ $do_build -eq 1 ]] && build_flag=(--build)

log "Starting the stack (docker compose up${build_flag[*]:+ ${build_flag[*]}} -d --wait)"
if ! docker compose up "${build_flag[@]}" -d --wait --wait-timeout "$WAIT_TIMEOUT"; then
    echo "Deploy failed — recent logs:" >&2
    docker compose logs --tail 50 "$SERVICE" >&2 || true
    exit 1
fi

docker compose ps

# --- 3. optional per-library API check --------------------------------------

if [[ $do_validate -eq 1 ]]; then
    log "Validating each library's backend API(s) via /validate-folio-connection"

    sigels=()
    for s in "$APP_DIR"/libraries/*/settings.json; do
        [[ -e "$s" ]] || continue
        sigels+=("$(basename "$(dirname "$s")")")
    done

    if [[ ${#sigels[@]} -eq 0 ]]; then
        echo "  (no libraries configured under libraries/)"
    else
        failed=0
        for sigel in "${sigels[@]}"; do
            # Run inside the container so we don't depend on the host port (which
            # is optional) or on FOLIO being reachable from the host. The image
            # has python; the sigel is passed as argv (never interpolated into
            # code) and url-encoded.
            if ! docker compose exec -T "$SERVICE" python -c '
import json, sys, urllib.error, urllib.request
from urllib.parse import quote
sigel = sys.argv[1]
url = "http://localhost:5000/%s/validate-folio-connection" % quote(sigel)
try:
    with urllib.request.urlopen(url, timeout=20) as r:
        code, body = r.status, r.read().decode()
except urllib.error.HTTPError as e:
    code, body = e.code, e.read().decode()
except Exception as e:
    print("  %-12s ERROR %s" % (sigel, e)); sys.exit(1)
try:
    body = json.dumps(json.loads(body))
except ValueError:
    pass
print("  %-12s HTTP %s  %s" % (sigel, code, body))
sys.exit(0 if code == 200 else 1)
' "$sigel"; then
                failed=$((failed + 1))
            fi
        done

        if [[ $failed -gt 0 ]]; then
            echo "  $failed of ${#sigels[@]} library check(s) did not return 200." >&2
            [[ $strict -eq 1 ]] && exit 1
        fi
    fi
fi

# --- 4. done ----------------------------------------------------------------

log "Deploy OK — rtac is healthy."

if [[ $do_logs -eq 1 ]]; then
    log "Following logs (Ctrl-C to stop)"
    docker compose logs -f "$SERVICE"
fi
