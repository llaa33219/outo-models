"""Tests for `outo_models.auth.permissions`.

`Scope` enumerates every permission a principal can hold. `ROLE_SCOPES` maps
role names to the bundles they receive on signup, and `has_scope` answers
the authz question "is this principal allowed to do X?" with the rule that
`Scope.ADMIN` is a wildcard.
"""

from __future__ import annotations

from collections.abc import Collection

import pytest

from outo_models.auth.permissions import ROLE_SCOPES, Scope, has_scope


class TestScopeEnum:
    """`Scope` is a `StrEnum` with the documented members."""

    @pytest.mark.parametrize(
        "name, value",
        [
            ("READ", "read"),
            ("WRITE", "write"),
            ("ADMIN", "admin"),
            ("REPOS_READ", "repos:read"),
            ("REPOS_WRITE", "repos:write"),
            ("ADMIN_USERS", "admin:users"),
            ("ADMIN_QUOTA", "admin:quota"),
            ("ADMIN_GPU", "admin:gpu"),
        ],
    )
    def test_member_name_and_value(self, name: str, value: str) -> None:
        member = Scope[name]
        assert member.value == value
        # StrEnum: comparison with the raw string must work.
        assert member == value

    def test_member_count_is_exactly_eight(self) -> None:
        # Adding a scope is a breaking change for every role bundle — pin it.
        assert len(Scope) == 8


class TestRoleScopes:
    """`ROLE_SCOPES` defines the scope bundles for `user` and `admin`."""

    def test_user_role_exists(self) -> None:
        assert "user" in ROLE_SCOPES

    def test_admin_role_exists(self) -> None:
        assert "admin" in ROLE_SCOPES

    def test_user_role_has_no_admin_wildcard(self) -> None:
        assert Scope.ADMIN not in ROLE_SCOPES["user"]

    def test_admin_role_has_admin_wildcard(self) -> None:
        assert Scope.ADMIN in ROLE_SCOPES["admin"]

    def test_user_role_bundles_are_frozensets(self) -> None:
        # Bundles must be immutable so they can be safely shared.
        for bundle in ROLE_SCOPES.values():
            assert isinstance(bundle, frozenset)

    def test_role_bundles_contain_only_known_scopes(self) -> None:
        for bundle in ROLE_SCOPES.values():
            for scope in bundle:
                assert isinstance(scope, Scope)


class TestHasScope:
    """`has_scope` answers "is `required` in `granted`, modulo the admin wildcard?"."""

    def test_exact_grant_returns_true(self) -> None:
        assert has_scope({Scope.READ}, Scope.READ) is True

    def test_missing_grant_returns_false(self) -> None:
        assert has_scope({Scope.READ}, Scope.WRITE) is False

    def test_admin_grant_implies_read(self) -> None:
        assert has_scope({Scope.ADMIN}, Scope.READ) is True

    def test_admin_grant_implies_write(self) -> None:
        assert has_scope({Scope.ADMIN}, Scope.WRITE) is True

    def test_admin_grant_implies_resource_scopes(self) -> None:
        # `admin` must be a real wildcard, not just "admin and read".
        for required in (
            Scope.REPOS_READ,
            Scope.REPOS_WRITE,
            Scope.ADMIN_USERS,
            Scope.ADMIN_QUOTA,
            Scope.ADMIN_GPU,
        ):
            assert has_scope({Scope.ADMIN}, required) is True

    def test_empty_collection_returns_false(self) -> None:
        assert has_scope(set(), Scope.READ) is False

    def test_unknown_scope_against_admin_returns_false(self) -> None:
        # `admin` only implies *known* scopes; an arbitrary string is not a Scope.
        assert has_scope({Scope.ADMIN}, "not-a-scope") is False  # type: ignore[arg-type]

    def test_collection_of_arbitrary_size(self) -> None:
        granted: Collection[Scope] = frozenset({Scope.READ, Scope.WRITE})
        assert has_scope(granted, Scope.READ)
        assert has_scope(granted, Scope.WRITE)
        assert not has_scope(granted, Scope.ADMIN)


class TestRoleScopesBehaviour:
    """End-to-end: every scope in a role bundle is granted by `has_scope`."""

    @pytest.mark.parametrize("role", sorted(ROLE_SCOPES))
    def test_every_scope_in_role_bundle_is_usable(self, role: str) -> None:
        bundle = ROLE_SCOPES[role]
        for required in bundle:
            assert has_scope(bundle, required) is True

    def test_admin_role_implies_every_documented_scope(self) -> None:
        # Pin the contract: an admin user can perform *every* known action.
        admin_bundle = ROLE_SCOPES["admin"]
        for required in Scope:
            assert has_scope(admin_bundle, required) is True
