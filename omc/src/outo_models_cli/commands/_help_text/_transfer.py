"""Help text for the read/write transfer commands: `ls`, `download`, `upload`."""

from __future__ import annotations

from outo_models_cli.commands._help_text._common import (
    AUTH_NOTES,
    COMMON_EXIT_CODES,
    CREDENTIAL_RESOLUTION_ORDER,
    EXAMPLES_MARKER,
    EXIT_CODES_MARKER,
    NOTES_MARKER,
    PURPOSE_MARKER,
    SERVER_RESOLUTION_ORDER,
    USAGE_MARKER,
)

# --- ls -------------------------------------------------------------------

LS_PURPOSE: str = (
    "PURPOSE\n"
    "  List the contents of one directory of a repository at a revision.\n"
    "  Equivalent to `ls -la` on a working tree - useful for inspecting a\n"
    "  repo before downloading or uploading."
)

LS_USAGE: str = (
    "USAGE\n"
    "  omc ls <owner>/<name> [--path <subdir>] [--revision <branch|tag|sha>]\n"
    "                       [--server <url>]"
)

LS_EXAMPLES: str = (
    "EXAMPLES\n"
    "  omc ls alice/my-model\n"
    "      List the repo root.\n"
    "\n"
    "  omc ls alice/my-model --path weights\n"
    "      List the `weights/` subdirectory.\n"
    "\n"
    "  omc ls alice/my-model --revision v1.0\n"
    "      List at tag `v1.0`.\n"
    "\n"
    "  omc ls alice/my-model --revision 8d2f0c1\n"
    "      List at commit SHA `8d2f0c1` (full or short hash accepted).\n"
    "\n"
    "  omc ls alice/my-model --server https://staging.example.com\n"
    "      Browse a repo on an alternate server."
)

LS_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  Table printed (or `(empty directory)` if the path has no entries).\n"
        "  1  Repo not found (404); forbidden (403); missing credential;\n"
        "     argument not in `<owner>/<name>` form.\n"
        "  2  Typer-level usage error."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

LS_NOTES: str = (
    (
        "NOTES\n"
        "  - `--revision` defaults to `main`. Branches, tags, and short or\n"
        "    full commit SHAs are all accepted.\n"
        "  - Subdirectories are listed lazily: each `ls` call hits the\n"
        "    server once per directory. For a recursive listing use\n"
        "    `omc download` (which iterates internally).\n"
        "  - File sizes reflect the latest revision's blob size. LFS-managed\n"
        "    files show the pointer-text size (a few hundred bytes), not\n"
        "    the resolved object's size."
    )
    + "\n"
    + AUTH_NOTES
    + "\n"
    + SERVER_RESOLUTION_ORDER
)

LS_EPILOG: str = "\n\n".join([LS_EXAMPLES, LS_EXIT_CODES, LS_NOTES])

# --- download -------------------------------------------------------------

DOWNLOAD_PURPOSE: str = (
    "PURPOSE\n"
    "  Recursively download a repository into a local directory. Files\n"
    "  larger than the per-file LFS boundary (default 100 MiB) are\n"
    "  streamed through the LFS GET endpoint; smaller files come through\n"
    "  the resolve endpoint. The download is resumable - a partially\n"
    "  populated directory is filled in on the next run."
)

DOWNLOAD_USAGE: str = (
    "USAGE\n"
    "  omc download <owner>/<name> [--revision <branch|tag|sha>]\n"
    "                          [--include <glob>] [--exclude <glob>]\n"
    "                          [--local-dir <path>] [--max-workers <int>]\n"
    "                          [--server <url>]"
)

DOWNLOAD_EXAMPLES: str = (
    "EXAMPLES\n"
    "  omc download alice/my-model\n"
    "      Download the entire repo into `./my-model/` (the local dir\n"
    "      defaults to the repo's name).\n"
    "\n"
    "  omc download alice/my-model --local-dir ./checkpoints\n"
    "      Download into an explicit directory.\n"
    "\n"
    "  omc download alice/my-model --include 'weights/*.safetensors'\n"
    "      Only pull files matching the glob (repeat `--include` to OR\n"
    "      several patterns; use `--exclude` to AND-filter them out).\n"
    "\n"
    "  omc download alice/my-model --exclude '*.bin' --max-workers 16\n"
    "      Skip the old `.bin` files; parallelize with 16 workers\n"
    "      (default 8). Use a lower value on slow networks.\n"
    "\n"
    "  omc download alice/my-model --revision v2.1 --server https://staging.example.com\n"
    "      Pin a specific tag on an alternate server.\n"
    "\n"
    "  # Resume a partial download:\n"
    "  omc download alice/my-model --local-dir ./my-model\n"
    "      Files already present on disk with matching size + content\n"
    "      are skipped; the line `Resumed: N` reports how many were kept."
)

DOWNLOAD_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  All files downloaded (or skipped because already present).\n"
        "  1  Repo not found (404); forbidden (403); missing credential;\n"
        "     argument not in `<owner>/<name>` form; server unreachable;\n"
        "     LFS object missing on the server.\n"
        "  2  Typer-level usage error (e.g. `--max-workers` not an int)."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

DOWNLOAD_NOTES: str = (
    (
        "NOTES\n"
        "  - Local-dir default is the repo's `<name>` (the part after `/`).\n"
        "    Pass `--local-dir .` for the current directory.\n"
        "  - Include / exclude globs match against the in-repo path\n"
        "    (`weights/layer-00.safetensors`, not the absolute local path).\n"
        "  - Files already on disk are detected by size + partial-hash; the\n"
        "    CLI never re-downloads what is already correct.\n"
        "  - Timeouts: 30 s connect / 30 s read on each file. The Basic-auth\n"
        "    client follows the `/resolve/...` to `/info/lfs/objects/{oid}`\n"
        "    redirect transparently.\n"
        "  - `--max-workers` only affects per-file parallelism; each file is\n"
        "    still a single GET (no chunked resumable stream)."
    )
    + "\n"
    + AUTH_NOTES
    + "\n"
    + SERVER_RESOLUTION_ORDER
)

DOWNLOAD_EPILOG: str = "\n\n".join(
    [DOWNLOAD_EXAMPLES, DOWNLOAD_EXIT_CODES, DOWNLOAD_NOTES],
)

# --- upload ---------------------------------------------------------------

UPLOAD_PURPOSE: str = (
    "PURPOSE\n"
    "  Upload a local file or folder to a repository. Files at or below\n"
    "  100 MiB ride the multipart endpoint in one (or sequential) commits;\n"
    "  files above the cap are routed through Git LFS - a single batch\n"
    "  POST, one streaming PUT per object, and a pointer-text commit that\n"
    "  ships alongside the small files in one final commit. No `git lfs\n"
    "  track` setup is required on the user side."
)

UPLOAD_USAGE: str = (
    "USAGE\n"
    "  omc upload <owner>/<name> <local-path>\n"
    "                         [--path-in-repo <subdir>] [--message <text>]\n"
    "                         [--server <url>]"
)

UPLOAD_EXAMPLES: str = (
    "EXAMPLES\n"
    '  omc upload alice/my-model ./README.md --message "add README"\n'
    "      Single-file upload. The file lands at the repo root.\n"
    "\n"
    "  omc upload alice/my-model ./weights --path-in-repo weights\n"
    "      Folder upload. Every file under `./weights/` lands under\n"
    "      `weights/` in the repo, preserving the directory structure.\n"
    "\n"
    "  omc upload alice/my-model ./big.bin\n"
    "      A 4 GiB binary. Routed to LFS automatically: the CLI hashes\n"
    "      the file, sends one LFS batch, streams the PUT, then commits\n"
    "      the pointer text in a single commit alongside any small\n"
    "      files in the same call.\n"
    "\n"
    '  omc upload alice/dataset ./data --message "v2 release" --path-in-repo v2\n'
    "      Upload under a subdirectory; useful for versioning a dataset.\n"
    "\n"
    "  omc upload alice/space ./site --server https://staging.example.com\n"
    "      Upload a static site folder to a Space on an alternate server.\n"
    "\n"
    '  OMC_TOKEN=hf_xxx... omc upload alice/my-model ./weights --message "ci release"\n'
    "      CI invocation: the env var supplies the credential, so no\n"
    "      config file is read or written."
)

UPLOAD_EXIT_CODES: str = (
    (
        "EXIT CODES\n"
        "  0  All files committed (one or more commits; the final SHA is\n"
        "     printed).\n"
        "  1  Missing credential; repo not found (404); forbidden (403);\n"
        "     argument not in `<owner>/<name>` form; local path missing or\n"
        "     empty; LFS batch reported per-object errors; the server did\n"
        "     not respond in time (treat as ambiguous - verify with\n"
        "     `omc ls` before retrying, since a retry can duplicate work).\n"
        "  2  Typer-level usage error."
    )
    + "\n"
    + COMMON_EXIT_CODES
)

UPLOAD_NOTES: str = (
    (
        "NOTES\n"
        "  - Partition: files <= 100 MiB use the multipart endpoint; larger\n"
        "    files use LFS. The threshold matches the server's multipart cap.\n"
        "  - Multi-request split: when the small-file total exceeds 50 MiB,\n"
        "    the CLI falls back to one commit per file. Same behavior as the\n"
        "    pre-LFS upload command.\n"
        "  - LFS dedup: identical `(oid, size)` pairs collapse to one PUT; the\n"
        "    pointer text is reused at commit time.\n"
        "  - The Basic-auth client (HTTP Basic, not Bearer) is used for the\n"
        "    LFS surface. The username is fetched via `/api/auth/me` on first\n"
        "    use and cached in the config file.\n"
        "  - Errors with `LFS upload failed for <file>: <code> <message>` are\n"
        "    per-object; one bad object aborts the whole upload (no silent\n"
        "    half-commits).\n"
        "  - A folder containing no files is an error (`No files found under <path>.`)."
    )
    + "\n"
    + AUTH_NOTES
    + "\n"
    + SERVER_RESOLUTION_ORDER
    + "\n"
    + CREDENTIAL_RESOLUTION_ORDER
)

UPLOAD_EPILOG: str = "\n\n".join(
    [UPLOAD_EXAMPLES, UPLOAD_EXIT_CODES, UPLOAD_NOTES],
)

# --- markers --------------------------------------------------------------

TRANSFER_MARKERS: tuple[str, ...] = (
    PURPOSE_MARKER,
    USAGE_MARKER,
    EXAMPLES_MARKER,
    EXIT_CODES_MARKER,
    NOTES_MARKER,
)


__all__ = [
    "DOWNLOAD_EPILOG",
    "DOWNLOAD_PURPOSE",
    "DOWNLOAD_USAGE",
    "LS_EPILOG",
    "LS_EXAMPLES",
    "LS_PURPOSE",
    "LS_USAGE",
    "TRANSFER_MARKERS",
    "UPLOAD_EPILOG",
]
