#!/usr/bin/env bash
# scripts/check-docs.sh — AGENTS.md §3 contract checker.
#
# Verifies the doc/ ↔ source contract documented in AGENTS.md §3:
#   (a) every docs/*.md file referenced from docs/index.md exists on disk,
#       and no docs/*.md file is missing from the index.
#   (b) every top-level CLI command name registered in src/outo_models/cli/main.py
#       (and the admin/setup/server sub-apps) appears in docs/cli.md.
#   (c) every OUTO_* env var that the running process reads (Settings fields
#       in src/outo_models/config.py plus the script-override knobs) appears
#       in at least one docs/*.md file.
#
# Exits 0 when every check passes; non-zero with a diagnostic on the first
# failure. Designed to be cheap enough to run from `make lint` and CI.

set -euo pipefail

# Resolve the repo root from this script's location so the script can be
# invoked from any working directory (CI, pre-commit, manual run).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DOCS_DIR="${REPO_ROOT}/docs"
CLI_MAIN="${REPO_ROOT}/src/outo_models/cli/main.py"
CLI_DIR="${REPO_ROOT}/src/outo_models/cli"
CONFIG_PY="${REPO_ROOT}/src/outo_models/config.py"

# --- preflight ----------------------------------------------------------

fail() {
    printf '[check-docs] %s\n' "$*" >&2
    exit 1
}

[[ -d "${DOCS_DIR}" ]]   || fail "docs/ directory not found: ${DOCS_DIR}"
[[ -f "${CLI_MAIN}" ]]   || fail "cli/main.py not found: ${CLI_MAIN}"
[[ -f "${CONFIG_PY}" ]]  || fail "config.py not found: ${CONFIG_PY}"
[[ -f "${DOCS_DIR}/index.md" ]] || fail "docs/index.md is required (TOC anchor)"

# --- helpers ------------------------------------------------------------

# Strip the markdown link wrapper `[text](path)` → `path`. The regex is
# anchored to `](path)` so we extract only the URL fragment and never
# carry along the surrounding prose. Two passes avoid sed's
# `s/PAT/REPL/gp` quirk where the modified *line* is printed, not just
# the matched fragments.
extract_links() {
    grep -oE '\]\(([^)]+)\)' "$1" \
        | grep -oE '\([^)]+\)' \
        | tr -d '()'
}

# Return a sorted, deduplicated list of items emitted by `extract` (stdin
# is the raw markdown). Whitespace trimmed, comments / blanks dropped.
sorted_uniq() {
    tr -d '\r' \
        | sed -E 's/[[:space:]]+//g; /^$/d; /^#/d' \
        | sort -u
}

# --- (a) docs/index.md ↔ docs/*.md --------------------------------------

mapfile -t INDEX_LINKS < <(extract_links "${DOCS_DIR}/index.md" | sorted_uniq)

# Only same-directory markdown links are relevant for the TOC contract.
# `index.md` itself is excluded — a TOC does not normally link to itself,
# and treating its own absence as a failure would be a false positive.
declare -a EXPECTED_DOCS=()
for link in "${INDEX_LINKS[@]}"; do
    case "${link}" in
        *.md)
            [[ "${link}" == "index.md" ]] && continue
            EXPECTED_DOCS+=("${link}")
            ;;
    esac
done

# Files that exist on disk under docs/. `index.md` is the TOC anchor and
# does not need to link to itself, so it is excluded from the on-disk set.
mapfile -t ON_DISK < <(
    find "${DOCS_DIR}" -maxdepth 1 -type f -name '*.md' -printf '%f\n' \
        | grep -v '^index.md$' \
        | sorted_uniq
)

declare -A on_disk_set=()
for f in "${ON_DISK[@]}"; do on_disk_set["${f}"]=1; done

declare -A index_set=()
for f in "${EXPECTED_DOCS[@]}"; do index_set["${f}"]=1; done

# Files referenced from index.md but missing on disk.
missing_files=()
for f in "${EXPECTED_DOCS[@]}"; do
    [[ -z "${on_disk_set[${f}]:-}" ]] && missing_files+=("${f}")
done

# Files that exist on disk but are not linked from index.md.
unlinked_files=()
for f in "${ON_DISK[@]}"; do
    [[ -z "${index_set[${f}]:-}" ]] && unlinked_files+=("${f}")
done

if (( ${#missing_files[@]} > 0 )); then
    fail "files referenced from docs/index.md are missing on disk: ${missing_files[*]}"
fi
if (( ${#unlinked_files[@]} > 0 )); then
    fail "docs/*.md files are not listed in the docs/index.md table of contents: ${unlinked_files[*]}"
fi

# --- (b) CLI command names → docs/cli.md -------------------------------

# Collect every CLI command name registered in the source tree.
#
# Patterns we care about:
#   - `app.add_typer(<sub>, name="<name>"...)`  (Typer sub-app under root)
#   - `@app.command("<name>")` / `app.command("<name>", ...)(<fn>)`     (root commands)
#   - same patterns inside the sub-app modules
#
# Names like `app.command("setup" ...)` collapse onto the root Typer `app`,
# so the *full* command path is just the registered string — Typer assembles
# the parent path itself.

declare -a CLI_NAMES=()

# Root Typer app in cli/main.py.
while IFS= read -r name; do
    [[ -n "${name}" ]] && CLI_NAMES+=("${name}")
done < <(
    grep -nE \
        -e 'app\.add_typer\([^,]+,\s*name="([^"]+)"' \
        -e 'app\.command\(\s*"([^"]+)"' \
        -e "@app\.command\(\s*\"([^\"]+)\"" \
        "${CLI_MAIN}" \
        | sed -E 's/.*name="([^"]+)".*/\1/; s/.*command\(\s*"([^"]+)".*/\1/' \
        | sorted_uniq
)

# Sub-app definitions inside cli/{setup,admin,server}/__init__.py — pull
# the sub-app names plus the commands they register.
for sub_module in "${CLI_DIR}/setup/__init__.py" \
                  "${CLI_DIR}/admin/__init__.py" \
                  "${CLI_DIR}/server.py"; do
    [[ -f "${sub_module}" ]] || continue
    while IFS= read -r name; do
        [[ -n "${name}" ]] && CLI_NAMES+=("${name}")
    done < <(
        grep -nE \
            -e '^[A-Za-z_]+_app\s*=\s*typer\.Typer\(\s*name="([^"]+)"' \
            -e '@[A-Za-z_]+_app\.command\(\s*"([^"]+)"' \
            -e '[A-Za-z_]+_app\.command\(\s*"([^"]+)"' \
            -e '[A-Za-z_]+_app\.add_typer\([^,]+,\s*name="([^"]+)"' \
            "${sub_module}" \
            | sed -E 's/.*name="([^"]+)".*/\1/; s/.*command\(\s*"([^"]+)".*/\1/' \
            | sorted_uniq
    )
done

mapfile -t CLI_NAMES < <(printf '%s\n' "${CLI_NAMES[@]}" | sorted_uniq)

# Sanity: we expect a non-trivial CLI surface. If the extractor missed
# everything, that is itself a bug worth surfacing.
if (( ${#CLI_NAMES[@]} == 0 )); then
    fail "CLI command extraction returned no results — verify the regex is intact."
fi

CLI_DOC="${DOCS_DIR}/cli.md"
[[ -f "${CLI_DOC}" ]] || fail "docs/cli.md is missing — cannot run check (b)."

missing_cmds=()
for name in "${CLI_NAMES[@]}"; do
    # Look for the command name as a standalone word in docs/cli.md.
    # Typer command names are kebab-case identifiers without punctuation,
    # so a simple whole-word match is safe.
    if ! grep -qE "(^|[^A-Za-z0-9_-])${name}([^A-Za-z0-9_-]|$)" "${CLI_DOC}"; then
        missing_cmds+=("${name}")
    fi
done

if (( ${#missing_cmds[@]} > 0 )); then
    fail "the following CLI commands are missing from docs/cli.md: ${missing_cmds[*]}"
fi

# --- (c) OUTO_* env vars → docs/*.md ----------------------------------

# Sources to scan (each contributes its own `OUTO_*` symbol set):
#   - src/outo_models/config.py          — Pydantic Settings fields
#   - src/outo_models/cli/__init__.py    — script-override env vars
#   - src/outo_models/cli/setup/_collect.py
#   - src/outo_models/cli/setup/_effect.py
#   - src/outo_models/cli/reset.py
#   - src/outo_models/cli/start.py
#   - src/outo_models/firewall/open_ports.py
#   - src/outo_models/tls/caddy_manager.py
#   - src/outo_models/server/app.py
#   - src/outo_models/objectstore/factory.py  — S3 backend error messages
#   - src/outo_models/spaces/runtime.py       — runtime_disabled hint
#
# We extract every `OUTO_<UPPER_SNAKE>` token and require it appear in at
# least one docs/*.md file. Comments / strings are in scope: env vars that
# the code only *talks about* must still be documented somewhere.
#
# `config.py` ALSO contributes the `OUTO_*` form of every Settings field —
# the Pydantic `env_prefix="OUTO_"` setting maps each field to its env var
# even when the code only references the field name (`settings.foo_bar`).
# The extractor walks the class body for `^\s+<name>\s*:` declarations and
# uppercases them. `model_config` is excluded because it is a Pydantic
# bookkeeping attribute, not an env knob.
declare -a ENVVAR_SOURCES=(
    "${CONFIG_PY}"
    "${CLI_DIR}/__init__.py"
    "${CLI_DIR}/setup/__init__.py"
    "${CLI_DIR}/setup/_collect.py"
    "${CLI_DIR}/setup/_effect.py"
    "${CLI_DIR}/reset.py"
    "${CLI_DIR}/start.py"
    "${REPO_ROOT}/src/outo_models/firewall/open_ports.py"
    "${REPO_ROOT}/src/outo_models/tls/caddy_manager.py"
    "${REPO_ROOT}/src/outo_models/server/app.py"
    "${REPO_ROOT}/src/outo_models/utils/paths.py"
    "${REPO_ROOT}/src/outo_models/db/migrations/env.py"
    "${REPO_ROOT}/src/outo_models/objectstore/factory.py"
    "${REPO_ROOT}/src/outo_models/spaces/runtime.py"
)

declare -a ENVVARS=()
for src in "${ENVVAR_SOURCES[@]}"; do
    [[ -f "${src}" ]] || continue
    while IFS= read -r tok; do
        [[ -n "${tok}" ]] && ENVVARS+=("${tok}")
    done < <(
        grep -hoE 'OUTO_[A-Z][A-Z0-9_]*' "${src}" | sorted_uniq
    )
done

# Settings-field → env-var expansion: read each `<field>: <annotation>` line
# from `Settings` and convert the field name to its `OUTO_<UPPER>` form.
# Methods (which have `:` only after the return-type annotation) and
# properties (which start with `def `) are filtered out by the leading
# whitespace + field-name shape below.
if [[ -f "${CONFIG_PY}" ]]; then
    while IFS= read -r field; do
        [[ -n "${field}" ]] || continue
        upper="$(printf '%s' "${field}" | tr '[:lower:]' '[:upper:]')"
        ENVVARS+=("OUTO_${upper}")
    done < <(
        grep -E '^[[:space:]]+[a-z_][a-z0-9_]*[[:space:]]*:' "${CONFIG_PY}" \
            | sed -E 's/^[[:space:]]+([a-z_][a-z0-9_]*).*/\1/' \
            | grep -v '^model_config$' \
            | sorted_uniq
    )
fi

mapfile -t ENVVARS < <(printf '%s\n' "${ENVVARS[@]}" | sorted_uniq)

if (( ${#ENVVARS[@]} == 0 )); then
    fail "OUTO_* environment variable extraction returned no results — verify the regex is intact."
fi

# Every docs/*.md is a valid target — concatenate once for efficiency.
DOCS_CONCAT="$(mktemp)"
trap 'rm -f "${DOCS_CONCAT}"' EXIT
cat "${DOCS_DIR}"/*.md > "${DOCS_CONCAT}"

missing_vars=()
for var in "${ENVVARS[@]}"; do
    if ! grep -qE "(^|[^A-Za-z0-9_])${var}([^A-Za-z0-9_]|$)" "${DOCS_CONCAT}"; then
        missing_vars+=("${var}")
    fi
done

if (( ${#missing_vars[@]} > 0 )); then
    fail "the following OUTO_* environment variables do not appear in any docs/*.md file: ${missing_vars[*]}"
fi

# --- all checks passed -------------------------------------------------

printf '[check-docs] OK — %d docs files, %d CLI commands, %d env vars verified.\n' \
    "${#ON_DISK[@]}" "${#CLI_NAMES[@]}" "${#ENVVARS[@]}"
exit 0