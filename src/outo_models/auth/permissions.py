"""Permission scope model for outo-models.

A `Scope` is a string token that names a permission. `ROLE_SCOPES` bundles
scopes for the two built-in roles (regular users and admins); `has_scope`
answers the authz question, with `Scope.ADMIN` acting as a wildcard so that
granting admin is functionally equivalent to granting every other scope.

Keep this module pure. It must not import FastAPI, the DB, or the auth
engine itself — any caller that has a collection of scopes and wants to
gate an action should be able to do so.
"""

from __future__ import annotations

from collections.abc import Collection
from enum import StrEnum


class Scope(StrEnum):
    """Every permission the application understands.

    Coarse scopes (`read`, `write`, `admin`) gate general API access;
    resource-prefixed scopes (`repos:read`, `repos:write`) gate the
    git smart-HTTP endpoints; admin-prefixed scopes (`admin:users`,
    `admin:quota`, `admin:gpu`) gate operator actions that must not be
    granted to ordinary users even with `admin`.
    """

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"
    REPOS_READ = "repos:read"
    REPOS_WRITE = "repos:write"
    ADMIN_USERS = "admin:users"
    ADMIN_QUOTA = "admin:quota"
    ADMIN_GPU = "admin:gpu"


#: Bundle of scopes each built-in role receives on account creation.
ROLE_SCOPES: dict[str, frozenset[Scope]] = {
    # Regular user: read & write API, can clone & push their own repos.
    "user": frozenset({Scope.READ, Scope.WRITE, Scope.REPOS_READ, Scope.REPOS_WRITE}),
    # Admin: every known scope, by virtue of the ADMIN wildcard.
    "admin": frozenset({Scope.ADMIN}),
}


def has_scope(granted: Collection[Scope], required: Scope) -> bool:
    """Return True iff `required` is satisfied by `granted`.

    `Scope.ADMIN` is a wildcard: holding it satisfies any *known* `Scope`
    value, including the admin-prefixed ones. A `required` value that is
    not a known scope cannot be satisfied — `False`, not a type error,
    because authz checks should never raise.
    """
    if required not in Scope:
        return False
    if Scope.ADMIN in granted:
        return True
    return required in granted
