#!/usr/bin/env bash
# Privacy gate for this public repo. Refuses to report a clean result it did
# not actually measure.
#
# Two phases, in this order, because the order is the whole point:
#
#   PHASE 1  CONTROLS. Every pattern is run against a planted string that it
#            MUST match. A pattern that finds nothing in the repo and nothing
#            in its own control is not a clean repo, it is a blind grep, and
#            the two are indistinguishable from an exit code alone.
#   PHASE 2  SCAN. Only patterns that passed phase 1 are run against the repo.
#            Each must return ZERO.
#
# Every grep here separates rc=1 (no matches) from rc>=2 (grep itself failed).
# `|| true` is banned in this file on purpose: it would turn a broken grep into
# a well formed zero, and a well formed zero is what "clean" looks like.
#
# The built in patterns are written with a bracketed character (/Use[r]s/) so
# this file does not match itself. scrub.sh scans the WHOLE repo including
# itself, with no self exemption, because an exempted file is exactly where a
# leak would survive.
#
# Patterns that would themselves leak (your own names, handles, internal repo
# names, customer names) are NOT stored in this repo. They are read at runtime
# from files you keep elsewhere:
#
#   SCRUB_PRIVATE_PATTERNS  a TSV file, one pattern per line:
#                           name<TAB>extended regex<TAB>planted control string
#                           (blank lines and lines starting with # are skipped)
#   SCRUB_CLIENTS_DIR       a directory whose subdirectory names are slugs to
#                           refuse (a customer list, for example)
#
# Either one unset means that check is SKIPPED, and a skipped check is reported
# and exits 3; it never degrades to a silent pass. Set SCRUB_ALLOW_SKIP=1 to
# accept a run with only the built in patterns (exit 0 is then labelled so).
#
# Exit 0 all clean, 1 a finding, 2 a pattern is blind or a grep failed,
# 3 a check could not run.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONTROL_DIR="$(mktemp -d)"
trap 'rm -rf "$CONTROL_DIR"' EXIT

PRIVATE_FILE="${SCRUB_PRIVATE_PATTERNS:-}"
CLIENTS_DIR="${SCRUB_CLIENTS_DIR:-}"
ALLOW_SKIP="${SCRUB_ALLOW_SKIP:-0}"

rc=0
blind=0
skipped=0

# Count matches in one file. Prints a number, or ERROR when grep itself failed.
count_matches() {
  local regex="$1" file="$2" out status
  set +e
  out=$(grep -Ec "$regex" "$file")
  status=$?
  set -e
  case "$status" in
    0) printf '%s' "$out" ;;
    1) printf '0' ;;
    *) printf 'ERROR' ;;
  esac
}

# List repo files matching a regex. Prints paths, empty for none, or the token
# GREP_ERROR when grep failed. -I skips binaries, -r walks the tree.
list_matching_files() {
  local regex="$1" out status
  set +e
  out=$(grep -EIlr \
    --exclude-dir=.git --exclude-dir=__pycache__ --exclude-dir=.pytest_cache \
    "$regex" "$REPO")
  status=$?
  set -e
  case "$status" in
    0) printf '%s' "$out" ;;
    1) printf '' ;;
    *) printf 'GREP_ERROR' ;;
  esac
}

NAMES=()
REGEXES=()
CONTROLS=()

add() {
  NAMES+=("$1")
  REGEXES+=("$2")
  CONTROLS+=("$3")
}

# Built in, generic: nothing here names a person, an account or a customer.
add "macos-home-path" '/Use[r]s/[A-Za-z]' "/Us""ers/somebody/code/thing"
add "linux-home-path" '/hom[e]/[a-z][a-z0-9_-]*/' "/ho""me/somebody/bin/x"
add "op-references" 'o[p]://' "token = o""p://Vault/Item/field"
add "private-key-block" 'BEGIN [A-Z ]*PRIVAT[E] KEY' "BEGIN RSA PRIVA""TE KEY"
add "env-file-secret" '^[A-Z_]*(TOKEN|SECRET|PASSWORD|API_KEY)=[^[:space:]]{8,}' "XAI_API_KEY=abcd""efgh1234"

if [ -n "$PRIVATE_FILE" ]; then
  if [ ! -r "$PRIVATE_FILE" ]; then
    echo "SCRUB_PRIVATE_PATTERNS=$PRIVATE_FILE is not readable"
    exit 3
  fi
  n_private=0
  while IFS=$'\t' read -r p_name p_regex p_control || [ -n "$p_name" ]; do
    case "$p_name" in ''|'#'*) continue ;; esac
    if [ -z "${p_regex:-}" ] || [ -z "${p_control:-}" ]; then
      echo "SCRUB_PRIVATE_PATTERNS: line for '$p_name' needs name<TAB>regex<TAB>control"
      exit 3
    fi
    add "private:$p_name" "$p_regex" "$p_control"
    n_private=$((n_private + 1))
  done < "$PRIVATE_FILE"
fi

echo "== PHASE 1: controls (each pattern must find its planted string) =="
for i in "${!NAMES[@]}"; do
  ctl="$CONTROL_DIR/control-$i.txt"
  printf '%s\n' "${CONTROLS[$i]}" > "$ctl"
  hits=$(count_matches "${REGEXES[$i]}" "$ctl")
  if [ "$hits" = "ERROR" ]; then
    printf '  BROKEN  %-28s grep itself failed on the control\n' "${NAMES[$i]}"
    blind=1
  elif [ "$hits" -ge 1 ]; then
    printf '  FIRES   %-28s %s hit(s) on control\n' "${NAMES[$i]}" "$hits"
  else
    printf '  BLIND   %-28s 0 hits on its OWN control, pattern is broken\n' "${NAMES[$i]}"
    blind=1
  fi
done
if [ -z "$PRIVATE_FILE" ]; then
  printf '  SKIPPED %-28s SCRUB_PRIVATE_PATTERNS unset\n' "private patterns"
  skipped=1
fi

# .bak is a filename check, not a content check, so it gets its own control.
printf 'planted\n' > "$CONTROL_DIR/planted.bak"
bak_ctl=$(find "$CONTROL_DIR" -name '*.bak' -type f | wc -l | tr -d ' ')
if [ "$bak_ctl" -ge 1 ]; then
  printf '  FIRES   %-28s %s hit(s) on control\n' "bak-files" "$bak_ctl"
else
  printf '  BLIND   %-28s find cannot see a planted .bak\n' "bak-files"
  blind=1
fi

# Client slugs: derived at runtime, never stored here, never printed.
SLUG_RE=""
if [ -n "$CLIENTS_DIR" ] && [ -d "$CLIENTS_DIR" ]; then
  # Keep only real slugs. A leading dot or underscore is not a client, and a
  # dotted entry would also corrupt the alternation: `\b(.claude|...)` has no
  # word boundary before the dot, so the whole pattern silently stops firing.
  set +e
  SLUG_RE=$(find "$CLIENTS_DIR" -mindepth 1 -maxdepth 1 -type d -exec basename {} \; \
    | grep -E '^[a-z0-9][a-z0-9_-]*$' | sort | paste -sd'|' -)
  slug_status=$?
  set -e
  if [ "$slug_status" -ne 0 ]; then
    SLUG_RE=""
  fi
fi
if [ -n "$SLUG_RE" ]; then
  n_slugs=$(printf '%s\n' "$SLUG_RE" | tr '|' '\n' | wc -l | tr -d ' ')
  first_slug=$(printf '%s' "$SLUG_RE" | cut -d'|' -f1)
  ctl="$CONTROL_DIR/control-client-slugs.txt"
  printf 'met with %s last week\n' "$first_slug" > "$ctl"
  hits=$(count_matches "\b($SLUG_RE)\b" "$ctl")
  if [ "$hits" != "ERROR" ] && [ "$hits" -ge 1 ]; then
    printf '  FIRES   %-28s %s hit(s) on control, %s slug(s) loaded\n' \
      "client-slugs" "$hits" "$n_slugs"
  else
    printf '  BLIND   %-28s 0 hits on its OWN control (or grep failed)\n' "client-slugs"
    blind=1
  fi
else
  printf '  SKIPPED %-28s SCRUB_CLIENTS_DIR unset or empty\n' "client-slugs"
  skipped=1
fi

if [ "$blind" -ne 0 ]; then
  echo
  echo "REFUSING to scan: at least one pattern is blind. Fix the pattern first."
  exit 2
fi

n_files=$(find "$REPO" -type f -not -path '*/.git/*' -not -path '*/__pycache__/*' \
  -not -path '*/.pytest_cache/*' | wc -l | tr -d ' ')
echo
echo "== PHASE 2: scan (each must return ZERO over $n_files files) =="

report() {
  local name="$1" found="$2"
  if [ "$found" = "GREP_ERROR" ]; then
    printf '  BROKEN  %-28s grep failed during the scan, result is NOT a zero\n' "$name"
    rc=2
  elif [ -z "$found" ]; then
    printf '  ZERO    %s\n' "$name"
  else
    printf '  FOUND   %s\n%s\n' "$name" "$found"
    rc=1
  fi
}

for i in "${!NAMES[@]}"; do
  report "${NAMES[$i]}" "$(list_matching_files "${REGEXES[$i]}")"
done

report "bak-files" "$(find "$REPO" -name '*.bak' -type f -not -path '*/.git/*')"

if [ -n "$SLUG_RE" ]; then
  report "client-slugs" "$(list_matching_files "\b($SLUG_RE)\b")"
fi

echo
if [ "$rc" -eq 2 ]; then
  echo "SCRUB INCONCLUSIVE: a grep failed. This is not a pass."
  exit 2
fi
if [ "$rc" -ne 0 ]; then
  echo "SCRUB FAILED: see FOUND above."
  exit 1
fi
if [ "$skipped" -ne 0 ]; then
  if [ "$ALLOW_SKIP" = "1" ]; then
    echo "SCRUB CLEAN on the built in patterns only (SCRUB_ALLOW_SKIP=1). Private checks did not run."
    exit 0
  fi
  echo "SCRUB CLEAN on every pattern that RAN, but at least one check was skipped."
  echo "That is not a pass. Set SCRUB_PRIVATE_PATTERNS and SCRUB_CLIENTS_DIR, or SCRUB_ALLOW_SKIP=1."
  exit 3
fi
echo "SCRUB CLEAN: every pattern fired on its control and returned zero on the repo."
