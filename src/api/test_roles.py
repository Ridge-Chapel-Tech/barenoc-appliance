#!/usr/bin/env python3
"""Tests for the three-tier roles + requester-owned close-loop (P1).

Covers:
  1. Role hierarchy: user (customer) < technician < admin, with the legacy
     operator==technician / tenant==user aliases; agent stays exact-match only.
  2. Users API: signup/creation defaults to `user`; technician + user are
     valid roles; role changes are admin-only.
  3. Requester-owned closure at the API: requester closes their own; a
     non-requester customer never closes (404); technician closes within
     device-group scope only; admin closes anything; readonly never mutates.

    cd src/api && python3 -m unittest test_roles -v
"""

import os
import tempfile
import unittest
from types import SimpleNamespace

_TMP = tempfile.mkdtemp(prefix="roles-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"

from fastapi import HTTPException, Response  # noqa: E402
from database import SessionLocal, init_db  # noqa: E402
from models import User, Ticket, Device  # noqa: E402
from auth import require_role  # noqa: E402
from schemas import RegisterRequest, TicketUpdate  # noqa: E402
from routes.tickets import update_ticket  # noqa: E402
from routes.users import create_user, update_user, UserCreate, UserUpdate  # noqa: E402
from routes.auth import register  # noqa: E402


def _checker(factory, role):
    """Call the require_role dependency's inner checker with a user role."""
    fn = factory()
    return fn(user=SimpleNamespace(role=role))


def _add(db, obj):
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


class RoleHierarchyTest(unittest.TestCase):
    """admin > technician/operator > readonly > user/tenant > agent."""

    def test_admin_passes_everything(self):
        self.assertEqual(_checker(lambda: require_role("admin"), "admin").role, "admin")
        self.assertEqual(_checker(lambda: require_role("operator"), "admin").role, "admin")

    def test_technician_passes_operator_gates(self):
        self.assertEqual(_checker(lambda: require_role("operator"), "technician").role, "technician")

    def test_operator_still_passes_operator_gates(self):
        self.assertEqual(_checker(lambda: require_role("operator"), "operator").role, "operator")

    def test_technician_denied_admin_gates(self):
        with self.assertRaises(HTTPException) as ctx:
            _checker(lambda: require_role("admin"), "technician")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_user_denied_operator_and_admin(self):
        for gate in ("operator", "admin"):
            with self.assertRaises(HTTPException):
                _checker(lambda: require_role(gate), "user")

    def test_tenant_denied_operator_and_admin(self):
        for gate in ("operator", "admin"):
            with self.assertRaises(HTTPException):
                _checker(lambda: require_role(gate), "tenant")

    def test_readonly_denied_operator(self):
        with self.assertRaises(HTTPException):
            _checker(lambda: require_role("operator"), "readonly")

    def test_agent_denied_operator(self):
        with self.assertRaises(HTTPException):
            _checker(lambda: require_role("operator"), "agent")


class UsersApiTest(unittest.TestCase):
    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        db.query(Device).delete()
        db.query(User).delete()
        admin = User(username="admin", hashed_password="x", role="admin", is_active=True)
        _add(db, admin)
        db.close()

    def _admin(self):
        db = SessionLocal()
        u = db.query(User).filter(User.username == "admin").first()
        db.close()
        return u

    def test_create_user_defaults_to_user(self):
        db = SessionLocal()
        create_user(UserCreate(username="bob", password="password123"),
                    db=db, user=self._admin())
        u = db.query(User).filter(User.username == "bob").first()
        self.assertEqual(u.role, "user")
        db.close()

    def test_create_technician_is_valid(self):
        db = SessionLocal()
        create_user(UserCreate(username="tech", password="password123", role="technician"),
                    db=db, user=self._admin())
        u = db.query(User).filter(User.username == "tech").first()
        self.assertEqual(u.role, "technician")
        db.close()

    def test_create_invalid_role_400(self):
        db = SessionLocal()
        with self.assertRaises(HTTPException) as ctx:
            create_user(UserCreate(username="x", password="password123", role="super"),
                        db=db, user=self._admin())
        self.assertEqual(ctx.exception.status_code, 400)
        db.close()

    def test_update_to_technician(self):
        db = SessionLocal()
        create_user(UserCreate(username="bob", password="password123"), db=db, user=self._admin())
        bob = db.query(User).filter(User.username == "bob").first()
        update_user(bob.id, UserUpdate(role="technician"), db=db, user=self._admin())
        self.assertEqual(bob.role, "technician")
        db.close()

    def test_register_defaults_to_user(self):
        db = SessionLocal()
        register(RegisterRequest(username="newbie", password="password123"),
                 Response(), db)
        u = db.query(User).filter(User.username == "newbie").first()
        self.assertIsNotNone(u)
        self.assertEqual(u.role, "user")
        db.close()


class CloseGateTest(unittest.TestCase):
    """Requester-owned close-loop: requester / (technician in scope) / admin."""

    def setUp(self):
        init_db()
        db = SessionLocal()
        db.query(Ticket).delete()
        db.query(Device).delete()
        db.query(User).delete()
        self.requester_id = _add(db, User(username="req", hashed_password="x",
                                          role="user", is_active=True)).id
        self.other_id = _add(db, User(username="other", hashed_password="x",
                                      role="user", is_active=True)).id
        self.tech_id = _add(db, User(username="tech", hashed_password="x",
                                     role="technician", is_active=True)).id
        self.admin_id = _add(db, User(username="admin", hashed_password="x",
                                      role="admin", is_active=True)).id
        self.readonly_id = _add(db, User(username="ro", hashed_password="x",
                                         role="readonly", is_active=True)).id
        self.ungrouped_id = _add(db, Device(name="Home GW", ip_address="10.0.0.1",
                                            device_type="gateway", device_group="default")).id
        self.grouped_id = _add(db, Device(name="Core SW", ip_address="10.0.1.1",
                                          device_type="switch", device_group="device-core")).id
        db.close()

    def _ticket(self, target_id):
        db = SessionLocal()
        _add(db, Ticket(ticket_id="TKT-ROLE-0001", title="t", description="d",
                        priority="P3", status="open", source="manual",
                        submitter_id=self.requester_id,
                        target_device_id=target_id,
                        work_notes="[]"))
        db.close()
        return target_id

    def _ctx(self, username, groups=None):
        db = SessionLocal()
        u = db.query(User).filter(User.username == username).first()
        db.close()
        return {"user": u, "groups": groups or [], "auth_method": "password"}

    def _ticket_status(self):
        db = SessionLocal()
        t = db.query(Ticket).filter(Ticket.ticket_id == "TKT-ROLE-0001").first()
        s = t.status
        db.close()
        return s

    def test_requester_closes_own(self):
        self._ticket(self.ungrouped_id)
        db = SessionLocal()
        update_ticket("TKT-ROLE-0001", TicketUpdate(status="closed"), db=db,
                      ctx=self._ctx("req"))
        self.assertEqual(self._ticket_status(), "closed")
        db.close()

    def test_non_requester_customer_never_closes(self):
        self._ticket(self.ungrouped_id)
        db = SessionLocal()
        with self.assertRaises(HTTPException) as ctx:
            update_ticket("TKT-ROLE-0001", TicketUpdate(status="closed"), db=db,
                          ctx=self._ctx("other"))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(self._ticket_status(), "open")
        db.close()

    def test_technician_closes_ungrouped_in_scope(self):
        self._ticket(self.ungrouped_id)
        db = SessionLocal()
        update_ticket("TKT-ROLE-0001", TicketUpdate(status="closed"), db=db,
                      ctx=self._ctx("tech"))
        self.assertEqual(self._ticket_status(), "closed")
        db.close()

    def test_technician_denied_out_of_scope_group(self):
        self._ticket(self.grouped_id)
        db = SessionLocal()
        with self.assertRaises(HTTPException) as ctx:
            update_ticket("TKT-ROLE-0001", TicketUpdate(status="closed"), db=db,
                          ctx=self._ctx("tech"))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self._ticket_status(), "open")
        db.close()

    def test_technician_closes_grouped_when_in_group(self):
        self._ticket(self.grouped_id)
        db = SessionLocal()
        update_ticket("TKT-ROLE-0001", TicketUpdate(status="closed"), db=db,
                      ctx=self._ctx("tech", groups=["device-core"]))
        self.assertEqual(self._ticket_status(), "closed")
        db.close()

    def test_admin_closes_any(self):
        self._ticket(self.grouped_id)
        db = SessionLocal()
        update_ticket("TKT-ROLE-0001", TicketUpdate(status="closed"), db=db,
                      ctx=self._ctx("admin"))
        self.assertEqual(self._ticket_status(), "closed")
        db.close()

    def test_readonly_cannot_mutate(self):
        self._ticket(self.ungrouped_id)
        db = SessionLocal()
        with self.assertRaises(HTTPException) as ctx:
            update_ticket("TKT-ROLE-0001", TicketUpdate(status="closed"), db=db,
                          ctx=self._ctx("ro"))
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertEqual(self._ticket_status(), "open")
        db.close()

    def test_customer_can_only_close(self):
        self._ticket(self.ungrouped_id)
        db = SessionLocal()
        with self.assertRaises(HTTPException) as ctx:
            update_ticket("TKT-ROLE-0001", TicketUpdate(priority="P1"), db=db,
                          ctx=self._ctx("req"))
        self.assertEqual(ctx.exception.status_code, 403)
        db.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
