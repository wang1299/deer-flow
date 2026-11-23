"""Explicit owner decisions over one uncertain attempt, never implicit dispatch."""

import asyncio
import copy
import json
import re

from deerflow_extension_api.plugins import ToolContext

OUTCOMES = {"completed", "no_effect", "compensated", "unknown"}


def decision_payload(payload):
    from .service import text

    required = {"run_id", "step_id", "attempt", "revision", "decision_id", "outcome", "evidence_ref"}
    if not isinstance(payload, dict) or not required <= payload.keys() or payload.keys() - required - {"child_run_id"}:
        raise ValueError("Reconciliation requires an exact attempt, revision, decision and evidence reference")
    result = {k: text(payload[k], limit) for k, limit in (("run_id", 64), ("step_id", 80), ("decision_id", 80), ("outcome", 16), ("evidence_ref", 512))}
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", result["decision_id"]):
        raise ValueError("Invalid decision_id")
    for key, minimum in (("attempt", 1), ("revision", 0)):
        if type(payload[key]) is not int or not minimum <= payload[key] <= 2**53 - 1:
            raise ValueError("Invalid attempt or revision")
        result[key] = payload[key]
    if result["outcome"] not in OUTCOMES:
        raise ValueError("Invalid reconciliation outcome")
    if "child_run_id" in payload:
        result["child_run_id"] = text(payload["child_run_id"], 256)
    return result


def prior_decision(record, decision):
    for item in record.get("resolutions", []):
        if item["decision_id"] == decision["decision_id"]:
            if item["request"] != decision:
                raise ValueError("decision_id already used with different reconciliation")
            return copy.deepcopy(item["receipt"])
    return None


def blocked_step(record, decision):
    if record.get("revision", 0) != decision["revision"]:
        raise ValueError("Stale workflow revision; reload before reconciling")
    if record["status"] not in ("needs_reconciliation", "recoverable", "queued", "running", "cancelling", "paused", "waiting_approval"):
        raise ValueError("Workflow does not require reconciliation")
    row = next((s for s in record["steps"] if s["step_id"] == decision["step_id"]), None)
    if row is None or row["attempt"] != decision["attempt"] or row["status"] in ("completed", "queued"):
        raise ValueError("Attempt is no longer blocked; reload before reconciling")
    return row


def apply_decision(record, decision, observed, *, actor, now, max_calls):
    """Pure transformation, called only inside the store's serialized transaction."""
    from .service import identity

    row = blocked_step(record, decision)
    resolutions = record.setdefault("resolutions", [])
    if len(resolutions) >= 128:
        raise ValueError("Reconciliation audit limit (128)")
    outcome = decision["outcome"]
    cancelled = record.get("cancel_requested") is True
    if cancelled and outcome in ("no_effect", "compensated"):
        raise ValueError("Cancellation cannot authorize another attempt; reconcile completed results or keep unknown")
    before = row["status"]
    if outcome != "unknown":
        if not observed:
            raise ValueError("An observed child outcome is required")
        # Rewind before this request. Replayed completed siblings still use their
        # immutable output, while stale error responses are never fed to the JS.
        index = next((i for i, item in enumerate(record["journal"]) if any(m.get("type") == "agent" and m.get("payload", {}).get("stepId") == row["step_id"] for m in item["frame"]["messages"])), None)
        requests = record.setdefault("reconciliation_replay", {})
        if index is None and not any(m.get("type") == "agent" and m.get("payload", {}).get("stepId") == row["step_id"] for m in requests.values()):
            raise ValueError("Missing committed child request; cannot safely replay")
        if index is None:
            index = len(record["journal"])
        for item in record["journal"][index:]:
            for message in item["frame"]["messages"]:
                if message.get("type") != "log":
                    requests[str(message["id"])] = copy.deepcopy(message)
        if outcome == "completed":
            if observed.get("status") != "completed" or "output" not in observed:
                raise ValueError("Completed reconciliation requires an attributable result")
            row.update(copy.deepcopy(observed))
            row.pop("error", None)
        else:
            if sum(s["attempt"] for s in record["steps"]) >= max_calls:
                raise ValueError("Total Agent call limit; cannot authorize another attempt")
            record.setdefault("attempt_history", []).append(copy.deepcopy(row))
            attempt = row["attempt"] + 1
            message_id = "workflow-" + identity([record["id"], row["step_id"], attempt])[:32]
            retry = {k: copy.deepcopy(row[k]) for k in ("step_id", "agent_ref", "input", "input_artifacts")}
            if "bridge_id" in row:
                retry["bridge_id"] = row["bridge_id"]
            retry["input"]["messages"][0]["id"] = message_id
            retry.update(attempt=attempt, thread_id="workflow-" + identity([record["id"], row["step_id"], attempt])[:32], run_id=None, status="queued", admission_started=False, idempotency_key=f"{record['id']}:{message_id}:{attempt}")
            row.clear()
            row.update(retry)
        record["journal"] = record["journal"][:index]
        record["journal_bytes"] = len(json.dumps(record["journal"]).encode())
    # Operator time does not create a new execution budget. Preserve the last
    # durable allowance; it can only shrink during subsequent execution.
    record.pop("budget_deadline", None)
    record.update(status="needs_reconciliation", error="Uncertain child outcomes remain blocked")
    safe = ("completed", "queued", "cancelled") if cancelled else ("completed", "queued")
    if outcome != "unknown" and all(s["status"] in safe or (not cancelled and s["status"] == "running" and s.get("run_id")) for s in record["steps"]):
        record.update(status="cancelled" if cancelled else "recoverable", error=None if cancelled else "Reconciled; sign in and recover the compatible workflow")
    receipt = {"run_id": record["id"], "decision_id": decision["decision_id"], "status": record["status"], "revision": record.get("revision", 0) + 1}
    resolutions.append(
        {
            "decision_id": decision["decision_id"],
            "request": copy.deepcopy(decision),
            "step_id": decision["step_id"],
            "attempt": decision["attempt"],
            "outcome": outcome,
            "actor": actor,
            "decided_at": now,
            "evidence_ref": decision["evidence_ref"],
            "previous_status": before,
            "next_status": row["status"],
            "observed_child_run_id": observed.get("run_id") if observed else None,
            "observed_child_status": observed.get("child_status") if observed else None,
            "receipt": receipt,
        }
    )
    return receipt


class Reconciliation:
    async def reconcile(self, payload, context):
        if isinstance(context, ToolContext) or context.interaction != "session" or context.principal.is_internal or context.agent_runs is None:
            raise ValueError("Reconciliation requires an authenticated browser session")
        decision = decision_payload(payload)
        owner = self.owner(context)
        # Own and drain the whole operation, including a database commit after
        # client disconnect. A retried decision gets its original receipt.
        task = asyncio.create_task(self.reconcile_body(owner, decision, context.agent_runs))
        self.admissions.add(task)
        task.add_done_callback(self.admission_finished)
        return await asyncio.shield(task)

    async def reconcile_body(self, owner, decision, runs):
        async with self.launch_lock:
            record = await asyncio.to_thread(self.store.get, owner, decision["run_id"])
            await self.check_configuration(self.new_entry(owner, record, runs))
            previous = prior_decision(record, decision)
            if previous is not None:
                return previous
            if decision["run_id"] in self.entries:
                raise ValueError("Workflow is still executing or cleaning up")
            row = blocked_step(record, decision)
            observed = None
            if decision["outcome"] != "unknown":
                observed = copy.deepcopy(row)
                child = decision.get("child_run_id", row.get("run_id"))
                if row.get("run_id") and child != row["run_id"]:
                    raise ValueError("Child run identity does not match the blocked attempt")
                if child:
                    async with asyncio.timeout(5):
                        run = await runs.get(thread_id=row["thread_id"], run_id=child)
                    if run.thread_id != row["thread_id"] or run.run_id != child or run.status not in ("success", "error", "interrupted", "timeout"):
                        raise ValueError("Child must be observed terminal before resolving its reservation")
                    observed.update(run_id=child, child_status=run.status, stop_reason=run.stop_reason)
                    if decision["outcome"] == "completed":
                        await self.collect_result(runs, record, observed, run)
                    else:
                        async with asyncio.timeout(5):
                            state = await runs.get_state(thread_id=row["thread_id"])
                        self.result_marker(observed, state, child)
                elif decision["outcome"] == "completed" or not (row.get("admission_rejected") is True or row.get("admission_started") is False or row["status"] == "creating_thread"):
                    # Even an operator assertion cannot release an unidentified,
                    # possibly running child. Admission rejection is durable proof.
                    raise ValueError("Identify and observe the child, or retain an unknown outcome")
            # Permissions can be withdrawn during observation. Never commit a
            # decision based only on an earlier capability check.
            await self.check_configuration(self.new_entry(owner, record, runs))
            async with self.write_lock:
                return await asyncio.to_thread(self.store.reconcile, owner, decision, observed, self.config["max_calls"])

    @staticmethod
    def result_marker(row, state, run_id):
        from .service import pending_input

        messages = state.get("values", {}).get("messages", [])
        if state.get("next") or state.get("interrupts") or pending_input(messages):
            row["status"] = "waiting_input"
            raise ValueError("Child requires input; workflow resume cannot answer child interrupts")
        message_id = row["input"]["messages"][0]["id"]
        marker = next(
            (
                i
                for i, m in enumerate(messages)
                if m.get("type", m.get("role")) in ("human", "user")
                and not m.get("additional_kwargs", {}).get("hide_from_ui")
                and (m.get("id") in (message_id, f"{message_id}__user") or m.get("additional_kwargs", {}).get("run_id") == run_id)
            ),
            None,
        )
        if marker is None or any(m.get("type", m.get("role")) in ("human", "user") for m in messages[marker + 1 :]):
            row["status"] = "result_conflict"
            raise ValueError("Thread advanced before result collection")
        return messages[marker + 1 :]

    async def collect_result(self, runs, record, row, run):
        """Shared live/reconciliation attribution and bounded snapshot validation."""
        from .service import AGENT_OUTPUT_BYTES, encoded, identity

        async with asyncio.timeout(5):
            state = await runs.get_state(thread_id=row["thread_id"])
        messages = self.result_marker(row, state, run.run_id)
        if run.status != "success":
            row["status"] = "failed"
            raise ValueError("Agent run did not succeed")
        answers = [m for m in messages if m.get("type", m.get("role")) in ("ai", "assistant") and not m.get("tool_calls")]
        if not answers:
            raise ValueError("Agent completed without an answer")
        output = answers[-1].get("content")
        if isinstance(output, str):
            try:
                output = json.loads(output)
            except ValueError:
                pass
        encoded(output, AGENT_OUTPUT_BYTES)
        async with asyncio.timeout(5):
            proof = await runs.configuration_evidence(thread_id=row["thread_id"], run_id=run.run_id)
        if proof.get("run_id") != run.run_id or proof.get("thread_id") != row["thread_id"] or proof.get("source") != "host_assembly":
            raise ValueError("Host configuration evidence does not match child run")
        row.update(configuration_evidence=proof, configuration_version=proof["version"], status="completed", output=copy.deepcopy(output), artifact_version=identity([record["id"], row["step_id"], row["attempt"], output]))
