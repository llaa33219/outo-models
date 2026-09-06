"""`outo-models-cli` — the user-facing command-line client for self-hosted outo-models servers.

Mirrors the surface area of `huggingface-cli` so users familiar with the Hub
can drive a self-hosted server without learning a new vocabulary:

    * `omc auth login --server ...` stores a PAT per server.
    * `omc repo create / delete / list` manage repositories.
    * `omc ls <owner>/<name>` lists a directory at a revision.
    * `omc download <owner>/<name>` streams a repository (resumable).
    * `omc upload <owner>/<name> <path>` ships one or many files.

The package is intentionally tiny: it owns the user-visible surface, nothing
else. The server itself ships separately as `outo-models`.
"""

from __future__ import annotations

__version__ = "0.1.0"
__all__ = ["__version__"]
