#!/usr/bin/env python3
"""Role-gating tests for the agent least-privilege split.

The `agent` service identity (Pi Agent Runner + scripts + scheduler) is a
tier-2 role DISTINCT from operator: it reaches exactly the write endpoints it
needs (device credentials, unifi sync/port writes) via require_any_role, but
is NOT admin, and does NOT widen operator/readonly permissions.

    docker compose exec api python3 -m unittest test_auth -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="auth-role-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from fastapi import HTTPException  # noqa: E402
from auth import require_role, require_any_role  # noqa: E402


def _checker(factory, role):
    """Call the dependency's inner checker with a user of the given role."""
    fn = factory()
    return fn(user=SimpleNamespace(role=role))


class RequireAnyRoleTest(unittest.TestCase):
    def test_agent_allowed(self):
        self.assertEqual(_checker(lambda: require_any_role("admin", "agent"), "agent").role, "agent")

    def test_admin_allowed(self):
        self.assertEqual(_checker(lambda: require_any_role("admin", "agent"), "admin").role, "admin")

    def test_operator_denied(self):
        # operator must NOT gain the agent's write endpoints
        with self.assertRaises(HTTPException):
            _checker(lambda: require_any_role("admin", "agent"), "operator")

    def test_readonly_denied(self):
        with self.assertRaises(HTTPException):
            _checker(lambda: require_any_role("admin", "agent"), "readonly")

    def test_agent_denied_admin_only(self):
        # agent is NOT admin: cannot reach admin-gated routes (users, settings...)
        with self.assertRaises(HTTPException):
            _checker(lambda: require_role("admin"), "agent")

    def test_agent_denied_operator_hierarchy(self):
        # agent is NOT operator either (roles map has no "agent" entry)
        with self.assertRaises(HTTPException):
            _checker(lambda: require_role("operator"), "agent")

    def test_admin_still_passes_hierarchy(self):
        self.assertEqual(_checker(lambda: require_role("admin"), "admin").role, "admin")
        self.assertEqual(_checker(lambda: require_role("operator"), "admin").role, "admin")


if __name__ == "__main__":
    unittest.main()
