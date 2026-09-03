#!/usr/bin/env python3
"""In-container integration test for the worker pipeline (judge/executor split).

Runs inside the barenoc-worker container (has SQLAlchemy + shared modules).
Uses a scratch sqlite DB and mocked judge/executor — no real LLM calls, no
touching the live DB or job queue.

    docker compose exec worker python3 /app/test_integration.py
"""

import os
import sys
import json
import shutil
import datetime
import tempfile
from unittest.mock import patch

# ── scratch environment BEFORE importing app modules ───────────────────────
_TMP = tempfile.mkdtemp(prefix="barenoc-int-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["LLM_JUDGE_ENABLED"] = "true"

# Shared modules (database/models/…) live in src/api — append it so this test
# runs from src/worker on the dev box / CI, not just in the flattened container.
# APPEND (not insert-0) so `import main` still resolves to src/worker/main.py.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api"))

from database import SessionLocal, init_db
from models import Ticket
from schemas import generate_ticket_id
import main as worker
import policy as policy_mod
from policy import reset_policy_cache
from judge import Verdict
from llm_client import LLMResponse
import llm_client

# Hermetic: ignore the mounted .env for policy loading (the live file holds
# explicit values that correctly override profile presets, and env_file:
# injects them into the process env too). Policy behavior is driven by
# os.environ below; file-precedence is covered by test_judge.
_ENV_PATCHER = patch("policy.read_env_file", return_value={})
_ENV_PATCHER.start()
# _retry_config/_judge_enabled import llm_providers.read_env_file at call time —
# patch it too so the live .env (which now holds LLM_RETRY_MAX_ATTEMPTS=10 from
# the settings save) can't override test values.
_ENV2_PATCHER = patch("llm_providers.read_env_file", return_value={})
_ENV2_PATCHER.start()
# F6 cost optimization is OFF for this suite — the integration checks exercise
# the pipeline itself, not the rate-window deferral / tier routing (covered by
# test_ratewindows + test_tierrouter).
os.environ["LLM_COST_OPTIMIZATION"] = "false"
for _k in list(os.environ):
    # Strip policy/retry/pi-agent env injected from the live .env (env_file:)
    # — explicit values must not override what the tests are exercising.
    if (_k.startswith("LLM_POLICY") or _k.startswith("LLM_RETRY")
            or _k.startswith("PI_AGENT")):
        del os.environ[_k]

PASS = 0


def ok(label: str, cond: bool, extra: str = ""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {extra}")
        raise SystemExit(1)


def make_ticket(title: str, desc: str = "", priority: str = "P3") -> Ticket:
    db = SessionLocal()
    t = Ticket(ticket_id=generate_ticket_id(), title=title, description=desc,
               priority=priority, status="open", source="user")
    db.add(t)
    db.commit()
    db.refresh(t)
    db.close()
    return t


def main():
    init_db()
    print(f"scratch DB: {_TMP}/test.db")
    os.makedirs(f"{_TMP}/jobs", exist_ok=True)
    worker.JOBS_INCOMING = f"{_TMP}/jobs/incoming"
    worker.JOBS_RUNNING = f"{_TMP}/jobs/running"
    worker.JOBS_COMPLETED = f"{_TMP}/jobs/completed"
    for d in (worker.JOBS_INCOMING, worker.JOBS_RUNNING, worker.JOBS_COMPLETED):
        os.makedirs(d, exist_ok=True)

    # 0. env plumbing
    ok("LLM_JUDGE_ENABLED parsed true", worker._judge_enabled() is True)
    os.environ["LLM_JUDGE_ENABLED"] = "false"
    ok("LLM_JUDGE_ENABLED=false disables", worker._judge_enabled() is False)
    os.environ["LLM_JUDGE_ENABLED"] = "true"

    # 1. judge says UNLAWFUL -> ticket escalated to human-tech, no job file
    t = make_ticket("delete the firewall rules", "block everything")
    db = SessionLocal()
    with patch("judge.judge_request", return_value=Verdict(
            lawful="no", action_class="", risk="high", scope="policy",
            checks={"legal": False, "safe": False},
            reason="security policy change needs a human technician")):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("unlawful -> escalated", t.status == "escalated",
       f"status={t.status}")
    ok("unlawful -> human-tech", t.assigned_to == "human-tech",
       f"assigned={t.assigned_to}")
    ok("unlawful -> judge reason in resolution",
       "Judge ruled unlawful" in (t.resolution or ""), t.resolution or "")
    ok("unlawful -> no job file", not t.job_file_path, t.job_file_path or "")
    db.close()

    # 2. judge AMBIGUOUS -> escalated, no job
    t = make_ticket("something weird happened", "not sure what to do")
    db = SessionLocal()
    with patch("judge.judge_request", return_value=Verdict(
            lawful="ambiguous", action_class="", reason="cannot classify")):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("ambiguous -> escalated", t.status == "escalated", t.status)
    ok("ambiguous -> no job file", not t.job_file_path)
    db.close()

    # 3. lawful + executor high-conf read-only -> auto-executed job file
    t = make_ticket("is the gateway online?", "connectivity check")
    db = SessionLocal()
    verdict = Verdict(lawful="yes", action_class="ping_test", risk="low",
                      checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
                      reason="routine connectivity check")
    resp = LLMResponse(action="ping_test", target="192.0.2.1", params={"count": 4},
                       reason="verify connectivity", confidence=0.95,
                       raw_text="{}", model="deepseek/deepseek-chat",
                       prompt_tokens=10, response_tokens=5, cost_usd=0.0001)
    with patch("judge.judge_request", return_value=verdict), \
         patch("executor.call_executor", return_value=resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("lawful read-only -> in_progress (auto-exec)", t.status == "in_progress", t.status)
    ok("job file written", bool(t.job_file_path) and os.path.exists(t.job_file_path),
       t.job_file_path or "none")
    if t.job_file_path and os.path.exists(t.job_file_path):
        job = json.load(open(t.job_file_path))
        ok("job action = ping_test", job["action"] == "ping_test", str(job))
        ok("job target = gateway", job["target"] == "192.0.2.1", job.get("target"))
    ok("llm metadata stored", t.llm_model == "deepseek/deepseek-chat", t.llm_model or "")
    db.close()

    # 4. POLICY: autonomous profile -> write action auto-executes (no approval)
    from policy import reset_policy_cache, Policy
    os.environ["LLM_POLICY_PROFILE"] = "autonomous"
    reset_policy_cache()
    t = make_ticket("reboot the switch tonight", "maintenance window")
    db = SessionLocal()
    verdict = Verdict(lawful="yes", action_class="reboot_device", risk="medium",
                      checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
                      reason="scheduled maintenance")
    write_resp = LLMResponse(action="reboot_device", target="switch-01",
                             params={"scheduled_at": "2026-08-05T02:00:00Z"},
                             reason="scheduled reboot", confidence=0.95,
                             raw_text="{}", model="deepseek/deepseek-chat",
                             prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("judge.judge_request", return_value=verdict), \
         patch("executor.call_executor", return_value=write_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("autonomous write -> auto-executed", t.status == "in_progress", t.status)
    ok("autonomous job written", bool(t.job_file_path))
    db.close()

    # 5. POLICY: strict profile -> same write lands in the approval queue
    os.environ["LLM_POLICY_PROFILE"] = "strict"
    reset_policy_cache()
    t = make_ticket("reboot the switch tonight", "maintenance window")
    db = SessionLocal()
    with patch("judge.judge_request", return_value=verdict), \
         patch("executor.call_executor", return_value=write_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("strict write -> approval queue", t.status == "escalated", t.status)
    ok("strict -> 'Needs review' resolution", "Needs review" in (t.resolution or ""),
       t.resolution or "")
    ok("strict job written (approval holds it)", bool(t.job_file_path))
    db.close()
    os.environ.pop("LLM_POLICY_PROFILE", None)
    reset_policy_cache()

    # 6. READ-ONLY CATALOG: new unifi_clients action auto-executes as read-only
    t = make_ticket("who is online right now?", "connectivity check")
    db = SessionLocal()
    verdict = Verdict(lawful="yes", action_class="unifi_clients", risk="low",
                      checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
                      reason="routine read-only query")
    ro_resp = LLMResponse(action="unifi_clients", target="", params={},
                          reason="list clients", confidence=0.95,
                          raw_text="{}", model="deepseek/deepseek-chat",
                          prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("judge.judge_request", return_value=verdict), \
         patch("executor.call_executor", return_value=ro_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("unifi_clients auto-executed (read-only)", t.status == "in_progress", t.status)
    ok("unifi_clients job written", bool(t.job_file_path))
    db.close()

    # 7. single-phase path still works when judge disabled (regression)
    os.environ["LLM_JUDGE_ENABLED"] = "false"
    t = make_ticket("ping the switch", "P3")
    db = SessionLocal()
    resp2 = LLMResponse(action="ping_test", target="switch-01", params={"count": 4},
                        reason="t", confidence=0.95, raw_text="{}",
                        model="deepseek/deepseek-chat",
                        prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("llm_client.call_llm", return_value=resp2):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("single-phase still auto-executes", t.status == "in_progress", t.status)
    ok("single-phase job written", bool(t.job_file_path))
    db.close()

    # 8. LLM RETRY: failed LLM call schedules a retry, never an instant escalation
    os.environ["LLM_RETRY_INTERVAL_MIN"] = "1"
    os.environ["LLM_RETRY_MAX_ATTEMPTS"] = "5"
    t = make_ticket("retry me", "P3")
    db = SessionLocal()
    with patch("llm_client.call_llm", return_value=None):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("llm failure -> stays in_progress (no instant escalation)", t.status == "in_progress", t.status)
    ok("llm failure -> agent_retry note", worker._last_note_event(t) == "agent_retry",
       worker._last_note_event(t))
    ok("llm failure -> attempt counted", worker._count_retry_notes(t) == 1,
       str(worker._count_retry_notes(t)))
    ok("llm failure -> no resolution left behind", t.resolution in (None, ""), t.resolution or "")
    db.close()

    # 9. RETRY ACCUMULATES then escalates at the cap
    for _ in range(4):  # attempts 2..5
        db = SessionLocal()
        with patch("llm_client.call_llm", return_value=None):
            worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
        db.close()
    db = SessionLocal()
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("retry cap -> escalated", t.status == "escalated", t.status)
    ok("retry cap -> human-tech", t.assigned_to == "human-tech", t.assigned_to or "")
    ok("retry cap -> 'model service unreachable' resolution",
       "model service unreachable" in (t.resolution or ""), t.resolution or "")
    db.close()

    # 10. RETRY CLEARS on a successful later call
    t = make_ticket("retry then succeed", "P3")
    db = SessionLocal()
    with patch("llm_client.call_llm", return_value=None):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    db.close()
    db = SessionLocal()
    with patch("llm_client.call_llm", return_value=resp2):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("retry clears on success -> auto-executed", t.status == "in_progress", t.status)
    ok("retry clears on success -> job written", bool(t.job_file_path))
    db.close()

    # 11. unifi_port_config flows through the worker path (write action)
    os.environ["LLM_POLICY_PROFILE"] = "autonomous"
    reset_policy_cache()
    t = make_ticket("tag Storage vlan on port 7 of the core switch", "P3")
    db = SessionLocal()
    verdict = Verdict(lawful="yes", action_class="unifi_port_config", risk="medium",
                      checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
                      reason="port vlan write")
    port_resp = LLMResponse(action="unifi_port_config", target="aa:bb:cc:dd:ee:01",
                            params={"port_idx": 7, "tagged": ["Storage"], "native": "Production"},
                            reason="assign vlans", confidence=0.95, raw_text="{}",
                            model="deepseek/deepseek-chat",
                            prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("judge.judge_request", return_value=verdict), \
         patch("executor.call_executor", return_value=port_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("unifi_port_config write -> auto-executed (autonomous)", t.status == "in_progress", t.status)
    ok("unifi_port_config job written", bool(t.job_file_path))
    if t.job_file_path and os.path.exists(t.job_file_path):
        job = json.load(open(t.job_file_path))
        ok("job action = unifi_port_config", job["action"] == "unifi_port_config", job.get("action"))
        ok("job params carry port_idx/tagged/native",
           job.get("params", {}).get("port_idx") == 7
           and "Storage" in (job.get("params", {}).get("tagged") or [])
           and job.get("params", {}).get("native") == "Production",
           str(job.get("params")))
    db.close()
    os.environ.pop("LLM_POLICY_PROFILE", None)
    reset_policy_cache()

    # 12. NEW READS: unifi_firewall_rules auto-executes as read-only
    # This exercises the judge/executor split under the LEGACY (no-profile)
    # policy, so re-enable the judge for just this case (test #7 left it off
    # for the single-phase regression; #14+ override it back off via policy).
    os.environ["LLM_JUDGE_ENABLED"] = "true"
    t = make_ticket("show my firewall rules", "P4")
    db = SessionLocal()
    verdict = Verdict(lawful="yes", action_class="unifi_firewall_rules", risk="low",
                      checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
                      reason="read-only query")
    fw_resp = LLMResponse(action="unifi_firewall_rules", target="", params={},
                          reason="list rules", confidence=0.95, raw_text="{}",
                          model="deepseek/deepseek-chat",
                          prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("judge.judge_request", return_value=verdict), \
         patch("executor.call_executor", return_value=fw_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("unifi_firewall_rules auto-executed (read-only)", t.status == "in_progress", t.status)
    ok("unifi_firewall_rules job written", bool(t.job_file_path))
    db.close()
    os.environ["LLM_JUDGE_ENABLED"] = "false"

    # 13. NEW WRITE: unifi_restart goes to the approval queue in strict
    os.environ["LLM_POLICY_PROFILE"] = "strict"
    reset_policy_cache()
    t = make_ticket("restart the AP-01 access point", "P2")
    db = SessionLocal()
    verdict = Verdict(lawful="yes", action_class="unifi_restart", risk="medium",
                      checks={"legal": True, "doable": True, "safe": True, "in_scope": True},
                      reason="device restart")
    rst_resp = LLMResponse(action="unifi_restart", target="aa:bb:cc:00:00:0a", params={},
                           reason="restart ap", confidence=0.95, raw_text="{}",
                           model="deepseek/deepseek-chat",
                           prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("judge.judge_request", return_value=verdict), \
         patch("executor.call_executor", return_value=rst_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("unifi_restart write -> approval queue (strict)", t.status == "escalated", t.status)
    ok("unifi_restart -> 'Needs review'", "Needs review" in (t.resolution or ""), t.resolution or "")
    db.close()
    os.environ.pop("LLM_POLICY_PROFILE", None)
    reset_policy_cache()

    # 14. AUTONOMOUS: low-confidence WRITE -> Customer Action, never escalated,
    # and the message lists the adopted gear for review
    os.environ["LLM_POLICY_PROFILE"] = "autonomous"
    os.environ["LLM_POLICY_JUDGE_REQUIRED"] = "false"
    reset_policy_cache()
    from models import Device as _D
    _db = SessionLocal()
    _db.add(_D(name="Office AP", ip_address="10.0.0.9", device_type="ap",
               status="online", claimed=True, unifi_managed=True,
               device_group="default", mac_address="aa:bb:cc:00:00:0f"))
    _db.commit()
    _db.close()
    t = make_ticket("vlan tagging on all aps", "P3")
    db = SessionLocal()
    low = LLMResponse(action="unifi_port_config", target="aa:bb:cc:dd:ee:01",
                      params={"port_idx": 7, "tagged": ["Storage"]},
                      reason="not sure which ports", confidence=0.70, raw_text="{}",
                      model="deepseek/deepseek-chat",
                      prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("llm_client.call_llm", return_value=low), \
         patch("emailer.send_email", return_value=(True, "")):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("autonomous low-conf write -> customer_action (no escalation)", t.status == "customer_action", t.status)
    ok("autonomous low-conf write -> customer assigned", t.assigned_to == "customer", t.assigned_to or "")
    notes = t.work_notes or ""
    ok("review message lists adopted gear", "Here's what I found on your network" in notes
       and "Office AP" in notes, notes[-200:])
    db.close()
    os.environ.pop("LLM_POLICY_PROFILE", None)
    os.environ.pop("LLM_POLICY_JUDGE_REQUIRED", None)
    reset_policy_cache()

    # 15. BATCH: autonomous -> one job file carrying the sub-jobs
    os.environ["LLM_POLICY_PROFILE"] = "autonomous"
    os.environ["LLM_POLICY_JUDGE_REQUIRED"] = "false"
    reset_policy_cache()
    t = make_ticket("bounce every port on the core switch", "P3")
    db = SessionLocal()
    batch_resp = LLMResponse(action="batch", target="", params={"jobs": [
        {"action": "unifi_port_bounce", "target": "aa:bb:cc:dd:ee:01",
         "params": {"port_idx": 1}},
        {"action": "unifi_port_bounce", "target": "aa:bb:cc:dd:ee:01",
         "params": {"port_idx": 2}},
    ]}, reason="bounce all ports", confidence=0.95, raw_text="{}",
        model="deepseek/deepseek-chat", prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("llm_client.call_llm", return_value=batch_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("batch auto-executed (autonomous)", t.status == "in_progress", t.status)
    ok("batch job written", bool(t.job_file_path) and os.path.exists(t.job_file_path))
    if t.job_file_path and os.path.exists(t.job_file_path):
        job = json.load(open(t.job_file_path))
        ok("job action = batch", job["action"] == "batch", job.get("action"))
        ok("job carries 2 sub-jobs", len(job.get("params", {}).get("jobs", [])) == 2,
           str(job.get("params")))
        ok("auto-exec job NOT flagged requires_approval", not job.get("requires_approval"))
    db.close()
    # #15 disabled the judge via a policy override; drop it so the strict test
    # below actually exercises the judge/executor path again.
    os.environ.pop("LLM_POLICY_JUDGE_REQUIRED", None)
    os.environ.pop("LLM_POLICY_PROFILE", None)
    reset_policy_cache()

    # 16. BATCH under strict -> held for approval (requires_approval in file)
    os.environ["LLM_POLICY_PROFILE"] = "strict"
    reset_policy_cache()
    t = make_ticket("bounce every port on the core switch", "P2")
    db = SessionLocal()
    with patch("judge.judge_request", return_value=Verdict(
            lawful="yes", action_class="batch", risk="high",
            checks={"legal": True, "doable": True, "safe": False, "in_scope": True},
            reason="multi-port write")), \
         patch("executor.call_executor", return_value=batch_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("batch under strict -> approval queue", t.status == "escalated", t.status)
    if t.job_file_path and os.path.exists(t.job_file_path):
        job = json.load(open(t.job_file_path))
        ok("held batch job flagged requires_approval", job.get("requires_approval") is True)
    db.close()
    os.environ.pop("LLM_POLICY_PROFILE", None)
    os.environ.pop("LLM_POLICY_JUDGE_REQUIRED", None)
    reset_policy_cache()

    # 17. CUSTOMER-ACTION EMAIL: submitter with a real email gets notified
    from models import User as _U
    _db = SessionLocal()
    _u = _U(username="homeowner", email="owner@example.com", hashed_password="x",
            role="admin")
    _db.add(_u)
    _db.commit()
    _uid = _u.id
    _db.close()
    os.environ["LLM_POLICY_PROFILE"] = "autonomous"
    os.environ["LLM_POLICY_JUDGE_REQUIRED"] = "false"
    reset_policy_cache()
    t = make_ticket("need input please", "P3")
    db = SessionLocal()
    tt = db.query(Ticket).filter(Ticket.id == t.id).first()
    tt.submitter_id = _uid
    db.commit()
    low2 = LLMResponse(action="escalate_human", target="", params={},
                       reason="need the AP names", confidence=0.5, raw_text="{}",
                       model="deepseek/deepseek-chat",
                       prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("llm_client.call_llm", return_value=low2), \
         patch("emailer.send_email", return_value=(True, "")) as send:
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("autonomous escalate_human -> customer_action", t.status == "customer_action", t.status)
    import time as _t
    _t.sleep(0.3)  # email fires in a background thread
    ok("customer-action email sent to submitter", send.call_count >= 1,
       f"calls={send.call_count}")
    if send.call_count:
        args = send.call_args
        ok("email recipient = submitter", "owner@example.com" in args[0][0], str(args[0][0]))
        ok("email subject has ticket id", "needs your input" in args[0][1], args[0][1])
    db.close()
    os.environ.pop("LLM_POLICY_PROFILE", None)
    os.environ.pop("LLM_POLICY_JUDGE_REQUIRED", None)
    reset_policy_cache()

    # 18. GUARDRAIL: 'which APs are offline' -> params get device_type=ap + status=offline
    t = make_ticket("which APs are offline", "P4")
    db = SessionLocal()
    dev_resp = LLMResponse(action="unifi_devices", target="", params={},
                           reason="list ap status", confidence=0.95, raw_text="{}",
                           model="deepseek/deepseek-chat",
                           prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("llm_client.call_llm", return_value=dev_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    if t.job_file_path and os.path.exists(t.job_file_path):
        job = json.load(open(t.job_file_path))
        p = job.get("params", {})
        ok("guardrail: device_type=ap", p.get("device_type") == "ap", str(p))
        ok("guardrail: status=offline", p.get("status") == "offline", str(p))
    else:
        ok("guardrail job written", False, t.status)
    db.close()

    # 19. FAILURE-FEEDBACK: re-processing an agent_failed ticket passes the
    # error back into the LLM call (technician loop), bounded by the budget
    t = make_ticket("tag wifi 5 on uplink", "P3")
    db = SessionLocal()
    tt = db.query(Ticket).filter(Ticket.id == t.id).first()
    worker.add_note(tt, "agent_failed",
                    "unifi_port_config on Outdoor AP failed: target must be a switch MAC")
    db.commit()
    dev_resp2 = LLMResponse(action="unifi_port_config", target="aa:bb:cc:dd:ee:01",
                            params={"port_idx": 2, "tagged": ["WiFi"]},
                            reason="corrected target", confidence=0.95, raw_text="{}",
                            model="deepseek/deepseek-v4-flash",
                            prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("llm_client.call_llm", return_value=dev_resp2) as cl:
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    ctx_ok = False
    for call_args in cl.call_args_list:
        kw = call_args.kwargs or {}
        if "target must be a switch MAC" in str(kw.get("extra_context", "")):
            ctx_ok = True
    ok("failure context fed back to the AI", ctx_ok)
    ok("failed-note counter", worker._count_failed_notes(tt) == 1)
    ok("attempt budget defaults (3, 60s)", worker._attempt_config() == (3, 60),
       str(worker._attempt_config()))
    db.close()

    # 20. TICKET-STATUS SHORT-CIRCUIT: an explicit TKT-… status reference
    # answers deterministically (ticket_status) in EVERY profile — never calls
    # the judge/executor and never spawns a device-action ticket (bug #16).
    os.environ["LLM_JUDGE_ENABLED"] = "true"
    os.environ["LLM_POLICY_PROFILE"] = "strict"
    reset_policy_cache()
    t = make_ticket("Can you give me a status on TKT-20260816-5935?", "P3")
    db = SessionLocal()
    with patch("judge.judge_request") as mj, patch("executor.call_executor") as me:
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    mj.assert_not_called()
    me.assert_not_called()
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("tkt status -> auto-executed (strict)", t.status == "in_progress", t.status)
    ok("tkt status -> job written", bool(t.job_file_path) and os.path.exists(t.job_file_path))
    if t.job_file_path and os.path.exists(t.job_file_path):
        job = json.load(open(t.job_file_path))
        ok("job action = ticket_status", job["action"] == "ticket_status",
           job.get("action"))
        ok("job params carry ticket_id",
           job.get("params", {}).get("ticket_id") == "TKT-20260816-5935",
           str(job.get("params")))
    db.close()
    os.environ.pop("LLM_POLICY_PROFILE", None)
    os.environ["LLM_JUDGE_ENABLED"] = "false"
    reset_policy_cache()

    # ── provider failover: chain a→b, a fails (timeout), b answers ──
    from llm_client import _PROVIDER_FAILURES, _PROVIDER_DOWN
    _PROVIDER_FAILURES.clear(); _PROVIDER_DOWN.clear()
    def _fake_prov(*_a):
        return [
            {"name": "a", "type": "openai", "api_key": "k1", "base_url": "http://a",
             "chat_model": "m1", "reasoner_model": "m1", "judge_model": "m1",
             "thinking": "auto", "price_mode": "zero", "input_price": 0, "output_price": 0},
            {"name": "b", "type": "openai", "api_key": "k2", "base_url": "http://b",
             "chat_model": "m2", "reasoner_model": "m2", "judge_model": "m2",
             "thinking": "auto", "price_mode": "zero", "input_price": 0, "output_price": 0},
        ]
    JSON_OK = ('{"action":"ping_test","target":"192.0.2.1","params":{},"reason":"r","confidence":0.9}')
    def _adapter(provider, model, messages, temperature, max_tokens, timeout):
        if provider["name"] == "a":
            raise TimeoutError("a timed out")
        return JSON_OK, 3, 3
    with patch("llm_client.provider_chain", side_effect=_fake_prov), \
         patch("llm_client.ADAPTERS", {"openai": _adapter}):
        resp = llm_client.call_llm("test failover", "P3")
    ok("failover: second provider answered", resp is not None and resp.model.startswith("b/"),
       str(resp))
    ok("failover: failed provider counted", _PROVIDER_FAILURES.get("a") == 1,
       str(_PROVIDER_FAILURES))
    ok("failover: healthy provider not marked down", "b" not in _PROVIDER_DOWN)

    # mark-down: after N consecutive failures a provider is skipped
    import llm_client as llm_client_mod
    _PROVIDER_FAILURES["a"] = llm_client_mod._provider_down_after() - 1
    llm_client_mod._note_provider_fail("a")
    ok("failover: provider marked down after threshold", "a" in _PROVIDER_DOWN,
       str(_PROVIDER_DOWN))
    with patch("llm_client.provider_chain", side_effect=_fake_prov), \
         patch("llm_client.ADAPTERS", {"openai": _adapter}):
        resp2 = llm_client.call_llm("test skip downed", "P3")
    ok("failover: downed provider skipped, next answers", resp2 is not None and resp2.model.startswith("b/"),
       str(resp2))
    _PROVIDER_FAILURES.clear(); _PROVIDER_DOWN.clear()

    # ── LLM outage → P1 ticket (dedup) → auto-close on recovery ──
    db = SessionLocal()
    worker._LLM_OUTAGE = False
    db.query(Ticket).filter(Ticket.title == worker.OUTAGE_TITLE).delete()
    db.commit()
    with patch("main._alert_llm_outage"):
        worker._mark_llm_outage(db, "chain down: a timed out, b timed out")
    out = db.query(Ticket).filter(Ticket.title == worker.OUTAGE_TITLE).all()
    ok("outage: P1 ticket opened", len(out) == 1 and out[0].priority == "P1")
    with patch("main._alert_llm_outage"):
        worker._mark_llm_outage(db, "still down")
    ok("outage: deduplicated while open",
       db.query(Ticket).filter(Ticket.title == worker.OUTAGE_TITLE).count() == 1)
    worker._clear_llm_outage(db, "b/m2")
    out2 = db.query(Ticket).filter(Ticket.title == worker.OUTAGE_TITLE).first()
    ok("outage: closed on recovery", out2 is not None and out2.status == "closed",
       str(out2.status if out2 else None))
    ok("outage: flag cleared", worker._LLM_OUTAGE is False)
    db.query(Ticket).filter(Ticket.title == worker.OUTAGE_TITLE).delete()
    db.commit()
    db.close()

    # 21. PI RE-DISPATCH GUARD: a user reply during an active pi session must
    # NOT spawn a second session — a short note is posted and no second job
    # file is written (the 08-17 double-dispatch incident).
    t = make_ticket("can you run updates on my Laptop?", "P3")
    db = SessionLocal()
    tt = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("pi dispatch: first dispatch handled",
       worker._dispatch_pi(db, tt, "can you run updates on my Laptop?") is True)
    db.commit()
    # simulate the runner picking the job up (incoming -> running)
    src_job = os.path.join(worker.JOBS_INCOMING, f"{tt.ticket_id}.json")
    os.makedirs(worker.JOBS_RUNNING, exist_ok=True)
    shutil.move(src_job, os.path.join(worker.JOBS_RUNNING, f"{tt.ticket_id}.json"))
    ok("pi guard: active run detected", worker._pi_run_active(tt) is True)
    # user replies mid-run -> re-dispatch attempt
    ok("pi re-dispatch guard: second dispatch skipped",
       worker._dispatch_pi(db, tt, "update?") is True)
    notes = worker._notes_list(tt)
    ok("pi re-dispatch guard: 'already working' note posted",
       any("already working" in (n.get("detail") or "") for n in notes),
       str([n.get("detail") for n in notes[-3:]]))
    ok("pi re-dispatch guard: no second job file",
       not os.path.exists(os.path.join(worker.JOBS_INCOMING, f"{tt.ticket_id}.json")))
    db.close()

    # 22. WATCHDOG: a genuinely-stuck pi run still escalates — the re-dispatch
    # guard blocks a second session but never blocks the escalation path.
    t = make_ticket("reboot the AP and tell me when done", "P3")
    db = SessionLocal()
    tt = db.query(Ticket).filter(Ticket.id == t.id).first()
    worker._dispatch_pi(db, tt, "reboot the AP and tell me when done")
    db.commit()
    # simulate a stuck run: the runner finished and removed its job file, but
    # the result POST was lost — no terminal note, last update >10 min ago.
    src_job = os.path.join(worker.JOBS_INCOMING, f"{tt.ticket_id}.json")
    if os.path.exists(src_job):
        os.remove(src_job)
    tt.updated_at = datetime.datetime.utcnow() - datetime.timedelta(minutes=11)
    tt.status = "in_progress"
    db.commit()
    ok("watchdog: stuck pi run still active (notes-based guard)",
       worker._pi_run_active(tt) is True)
    n_esc = worker._escalate_stuck_jobs(db)
    tt = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("watchdog: stuck pi run escalated", tt.status == "escalated", tt.status)
    ok("watchdog: human-tech assigned", tt.assigned_to == "human-tech",
       tt.assigned_to or "")
    ok("watchdog: escalated exactly once", n_esc == 1, str(n_esc))
    ok("watchdog: escalation clears the active run (re-dispatch now allowed)",
       worker._pi_run_active(tt) is False)
    db.close()

    # 23-28. TICKET CLOSE-DIRECTIVE: a completed ticket's close/ack reply is
    # handled inline (close or ack note), never re-dispatched to a fresh pi
    # session (TKT-20260818-5615); non-close follow-ups still dispatch; owner
    # gating (requester or operator/admin may close).
    from models import User as _U
    _db = SessionLocal()
    _owner = _U(username="close-owner", display_name="Owner",
                hashed_password="x", role="tenant", is_active=True)
    _other = _U(username="close-other", display_name="Other",
                hashed_password="x", role="tenant", is_active=True)
    _op = _U(username="close-op", display_name="Operator",
             hashed_password="x", role="operator", is_active=True)
    _db.add_all([_owner, _other, _op])
    _db.commit()
    _owner_id = _owner.id
    _db.close()

    def _completed(actor, message, submitter_id=None):
        t = make_ticket("can you run updates on my Laptop?", "P3")
        db = SessionLocal()
        tt = db.query(Ticket).filter(Ticket.id == t.id).first()
        tt.submitter_id = submitter_id if submitter_id is not None else _owner_id
        worker.add_note(tt, "agent_completed", "updates applied — task done")
        worker.add_note(tt, "user_message", message, actor=actor)
        tt.status = "customer_action"
        db.commit()
        tid = tt.ticket_id
        db.close()
        return tid

    os.environ["LLM_POLICY_PROFILE"] = "autonomous"
    os.environ["PI_AGENT_ENABLED"] = "true"
    reset_policy_cache()

    # 23. completed + close (requester) -> closed inline, no pi job file
    tid = _completed("close-owner", "yes, please close")
    db = SessionLocal()
    worker.process_ticket(db, db.query(Ticket).filter(Ticket.ticket_id == tid).first())
    tt = db.query(Ticket).filter(Ticket.ticket_id == tid).first()
    ok("close-directive: completed+close -> closed", tt.status == "closed", tt.status)
    ok("close-directive: resolved_at set", tt.resolved_at is not None)
    ok("close-directive: assigned_to = who closed", tt.assigned_to == "close-owner",
       tt.assigned_to or "")
    ok("close-directive: no pi job file",
       not os.path.exists(os.path.join(worker.JOBS_INCOMING, f"{tid}.json")))
    db.close()

    # 24. completed + ack -> short ack note, no dispatch, stays customer_action
    tid = _completed("close-owner", "thanks!")
    db = SessionLocal()
    worker.process_ticket(db, db.query(Ticket).filter(Ticket.ticket_id == tid).first())
    tt = db.query(Ticket).filter(Ticket.ticket_id == tid).first()
    ok("close-directive: completed+ack -> stays customer_action",
       tt.status == "customer_action", tt.status)
    notes = worker._notes_list(tt)
    ok("close-directive: ack note posted",
       any("say 'close'" in (n.get("detail") or "") for n in notes),
       str([n.get("detail") for n in notes[-3:]]))
    ok("close-directive: ack -> no pi job file",
       not os.path.exists(os.path.join(worker.JOBS_INCOMING, f"{tid}.json")))
    db.close()

    # 25. completed + NEW request -> normal dispatch (a fresh pi session)
    tid = _completed("close-owner", "please run updates again")
    db = SessionLocal()
    worker.process_ticket(db, db.query(Ticket).filter(Ticket.ticket_id == tid).first())
    tt = db.query(Ticket).filter(Ticket.ticket_id == tid).first()
    ok("close-directive: completed+new request -> dispatches",
       tt.status == "in_progress" and tt.action == "pi_task",
       f"status={tt.status} action={tt.action}")
    jpath = os.path.join(worker.JOBS_INCOMING, f"{tid}.json")
    ok("close-directive: new request -> pi job file written", os.path.exists(jpath))
    if os.path.exists(jpath):
        os.remove(jpath)
    db.close()

    # 26. completed + close by NON-requester -> waiting on requester, no close
    tid = _completed("close-other", "close the ticket", submitter_id=_owner_id)
    db = SessionLocal()
    worker.process_ticket(db, db.query(Ticket).filter(Ticket.ticket_id == tid).first())
    tt = db.query(Ticket).filter(Ticket.ticket_id == tid).first()
    ok("close-directive: non-requester close -> not closed",
       tt.status == "customer_action", tt.status)
    notes = worker._notes_list(tt)
    ok("close-directive: non-requester -> waiting on requester note",
       any("Waiting on close-owner" in (n.get("detail") or "") for n in notes),
       str([n.get("detail") for n in notes[-3:]]))
    ok("close-directive: non-requester -> no pi job file",
       not os.path.exists(os.path.join(worker.JOBS_INCOMING, f"{tid}.json")))
    db.close()

    # 27. completed + close by operator (technician) -> closed
    tid = _completed("close-op", "close it", submitter_id=_owner_id)
    db = SessionLocal()
    worker.process_ticket(db, db.query(Ticket).filter(Ticket.ticket_id == tid).first())
    tt = db.query(Ticket).filter(Ticket.ticket_id == tid).first()
    ok("close-directive: operator close -> closed", tt.status == "closed", tt.status)
    ok("close-directive: operator close -> assigned to op", tt.assigned_to == "close-op",
       tt.assigned_to or "")
    db.close()

    # 28. mid-work + close -> polite 'still open' note, no dispatch, not closed
    t = make_ticket("reboot the AP", "P3")
    db = SessionLocal()
    tt = db.query(Ticket).filter(Ticket.id == t.id).first()
    tt.submitter_id = _owner_id
    worker.add_note(tt, "auto_execute", worker._PI_DISPATCH_DETAIL)
    worker.add_note(tt, "user_message", "close it", actor="close-owner")
    tt.status = "in_progress"
    db.commit()
    tid = tt.ticket_id
    db.close()
    db = SessionLocal()
    worker.process_ticket(db, db.query(Ticket).filter(Ticket.ticket_id == tid).first())
    tt = db.query(Ticket).filter(Ticket.ticket_id == tid).first()
    ok("close-directive: mid-work close -> not closed", tt.status == "in_progress", tt.status)
    notes = worker._notes_list(tt)
    ok("close-directive: mid-work close -> 'still open' note",
       any("still open" in (n.get("detail") or "") for n in notes),
       str([n.get("detail") for n in notes[-3:]]))
    ok("close-directive: mid-work close -> no pi job file",
       not os.path.exists(os.path.join(worker.JOBS_INCOMING, f"{tid}.json")))
    db.close()

    os.environ.pop("LLM_POLICY_PROFILE", None)
    os.environ.pop("PI_AGENT_ENABLED", None)
    reset_policy_cache()

    # 29. WHOLE-SUBNET RESILIENCE (friend's bug #2): a "ping sweep
    # 192.168.1.0/24" request must not abort because the AI pinned an
    # unresolvable device name — scan the subnet + note the name miss.
    from action_validator import MANAGED_DEVICES as _MD
    _MD.clear()
    _MD["gateway"] = {"id": 1, "ip": "192.0.2.1", "type": "router",
                      "hostname": None}
    t = make_ticket("ping sweep 192.168.1.0/24", "find live hosts on the subnet")
    db = SessionLocal()
    sweep_resp = LLMResponse(action="ping_test", target="switch-01",
                             params={"count": 4}, reason="sweep", confidence=0.95,
                             raw_text="{}", model="deepseek/deepseek-chat",
                             prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("llm_client.call_llm", return_value=sweep_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("subnet sweep -> auto-executed", t.status == "in_progress", t.status)
    ok("subnet sweep -> job written", bool(t.job_file_path) and os.path.exists(t.job_file_path))
    if t.job_file_path and os.path.exists(t.job_file_path):
        job = json.load(open(t.job_file_path))
        ok("subnet sweep -> network_discovery", job["action"] == "network_discovery",
           job.get("action"))
        ok("subnet sweep -> CIDR target", job["target"] == "192.168.1.0/24",
           job.get("target"))
    notes = t.work_notes or ""
    ok("subnet sweep -> name-miss note",
       "switch-01" in notes and "couldn't find a device" in notes, notes[-400:])
    db.close()
    _MD.clear()

    # 30. BARE NAME-ONLY REQUEST: an unknown device name with no subnet in the
    # request still fails — but with the friendly product message in chat, and
    # the technical inventory detail kept in the ticket note/log.
    _MD.clear()
    _MD["gateway"] = {"id": 1, "ip": "192.0.2.1", "type": "router",
                      "hostname": None}
    t = make_ticket("ping switch-01", "is it up")
    db = SessionLocal()
    bad_resp = LLMResponse(action="ping_test", target="switch-01",
                           params={"count": 4}, reason="t", confidence=0.95,
                           raw_text="{}", model="deepseek/deepseek-chat",
                           prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("llm_client.call_llm", return_value=bad_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("bare name -> escalated", t.status == "escalated", t.status)
    ok("bare name -> friendly chat message",
       "I couldn't find a device named 'switch-01'" in (t.resolution or ""),
       t.resolution or "")
    ok("bare name -> no inventory jargon in chat",
       "managed inventory" not in (t.resolution or ""), t.resolution or "")
    _notes = json.loads(t.work_notes or "[]")
    _esc = next((n for n in _notes if n.get("event") == "escalated"), {})
    _tech = next((n for n in _notes if n.get("event") == "target_validation_failed"), {})
    ok("bare name -> escalated note is friendly",
       "I couldn't find a device named 'switch-01'" in (_esc.get("detail") or ""),
       _esc.get("detail") or "")
    ok("bare name -> technical detail kept in hidden note",
       "managed inventory" in (_tech.get("detail") or ""), _tech.get("detail") or "")
    ok("bare name -> no job file", not t.job_file_path, t.job_file_path or "")
    db.close()
    _MD.clear()

    # 31. ENDPOINT-SCAN GUARDRAIL (forum: "odd results when looking for
    # endpoints"): "what endpoints are responding on 192.168.1.0/24" is a
    # subnet ping-sweep (network_discovery), NOT a network/VLAN summary
    # (network_info). Even when the AI reads the word "network" and returns
    # network_info, the worker must correct it to the live-host sweep the
    # customer asked for. (Judge is disabled at this point in the run — this
    # exercises the direct-LLM path, the same one the judge/executor fix
    # covers via test_judge.)
    t = make_ticket(
        "endpoints on the network",
        "Can you please tell me what endpoints are responding on the "
        "192.168.1.0/24 network?")
    db = SessionLocal()
    wrong_resp = LLMResponse(action="network_info", target="", params={},
                             reason="fetch network/VLAN/SSID config",
                             confidence=0.95, raw_text="{}",
                             model="deepseek/deepseek-chat",
                             prompt_tokens=5, response_tokens=5, cost_usd=0.0)
    with patch("llm_client.call_llm", return_value=wrong_resp):
        worker.process_ticket(db, db.query(Ticket).filter(Ticket.id == t.id).first())
    t = db.query(Ticket).filter(Ticket.id == t.id).first()
    ok("endpoint scan -> auto-executed", t.status == "in_progress", t.status)
    ok("endpoint scan -> job written",
       bool(t.job_file_path) and os.path.exists(t.job_file_path),
       t.job_file_path or "none")
    if t.job_file_path and os.path.exists(t.job_file_path):
        job = json.load(open(t.job_file_path))
        ok("endpoint scan -> network_discovery",
           job["action"] == "network_discovery", job.get("action"))
        ok("endpoint scan -> CIDR target",
           job["target"] == "192.168.1.0/24", job.get("target"))
    notes = json.loads(t.work_notes or "[]")
    ok("endpoint scan -> correction note",
       any("instead of network_info" in (n.get("detail") or "") for n in notes),
       str([n.get("detail") for n in notes[-4:]]))
    db.close()

    print(f"\nALL {PASS} INTEGRATION CHECKS PASSED (scratch DB: {_TMP})")


if __name__ == "__main__":
    main()
