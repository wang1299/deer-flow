"""Lightweight outer AgentRuns boundary shared by workflow worker/crash tests."""

import asyncio
import json

from deerflow_extension_api import AgentRun, AgentRunError, ExtensionPrincipal
from deerflow_extension_api.plugins import ActionContext


class Runs:
    def __init__(self):
        self.threads = {}
        self.runs = {}
        self.starts = []
        self.identities = {}
        self.active = 0
        self.peak = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.admission = asyncio.Event()
        self.return_admission = asyncio.Event()
        self.hold_admission = False
        self.jobs = []
        self.denied = False

    revision = "initial"

    def capture_authority(self):
        if self.denied:
            return None
        return {"token_version": 0, "source": "session", "permissions": ["runs:create", "threads:write"]}

    async def configuration_evidence(self, *, thread_id, run_id):
        return {"run_id": run_id, "thread_id": thread_id, "source": "host_assembly", "version": "synthetic-assembly", "model": "test-model"}

    async def validate_run_identity(self, *, thread_id, run_id, idempotency_key):
        if self.denied or self.identities.get((thread_id, run_id)) != idempotency_key:
            raise AgentRunError(404, "Keyed run identity unavailable")

    async def read_artifact(self, *, thread_id, path):
        return self.file_content

    file_content = b"original file"

    async def configuration_revision(self, *, assistant_ids, durable=False):
        if self.denied:
            raise AgentRunError(403, "revoked")
        return self.revision

    async def create_thread(self, *, assistant_id, thread_id, metadata):
        if self.denied:
            raise AgentRunError(403, "revoked")
        self.threads.setdefault(
            thread_id,
            {
                "values": {"messages": []},
                "next": [],
                "interrupts": [],
                "assistant": assistant_id,
            },
        )
        return thread_id

    async def start(self, *, thread_id, input, idempotency_key):
        if self.denied:
            raise AgentRunError(403, "revoked")
        if idempotency_key in self.runs:
            return self.runs[idempotency_key]
        run = AgentRun(thread_id, idempotency_key, "running", self.threads[thread_id]["assistant"])
        self.runs[run.run_id] = run
        self.starts.append((thread_id, input, idempotency_key))
        self.identities[(thread_id, run.run_id)] = idempotency_key
        self.threads[thread_id]["values"]["messages"].extend(input["messages"])
        self.jobs.append(asyncio.create_task(self.answer(run, input)))
        self.admission.set()
        if self.hold_admission:
            await self.return_admission.wait()
        return run

    async def answer(self, run, input):
        task = json.loads(input["messages"][0]["content"])
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.entered.set()
        try:
            if task == "blocked":
                await self.release.wait()
            else:
                await asyncio.sleep(0.02)
            if self.runs[run.run_id].status != "running":
                return
            state = self.threads[run.thread_id]
            if task == "interrupt":
                state["next"] = ["tools"]
                state["interrupts"] = [{"value": "Approve?"}]
            elif task == "clarification":
                state["values"]["messages"].append(
                    {
                        "type": "tool",
                        "artifact": {
                            "human_input": {
                                "kind": "human_input_request",
                                "version": 2,
                                "request_id": "req",
                                "source": "ask_clarification",
                                "question": "Confirm?",
                            }
                        },
                    }
                )
            else:
                state["values"]["messages"].append({"type": "ai", "content": json.dumps({"echo": task})})
            self.runs[run.run_id] = AgentRun(run.thread_id, run.run_id, "error" if task == "fail" else "success")
        finally:
            self.active -= 1

    async def get(self, *, thread_id, run_id):
        if self.denied:
            raise AgentRunError(403, "revoked")
        return self.runs[run_id]

    async def wait(self, *, thread_id, run_id, timeout=60):
        async with asyncio.timeout(timeout):
            while (await self.get(thread_id=thread_id, run_id=run_id)).status == "running":
                await asyncio.sleep(0.002)
        return await self.get(thread_id=thread_id, run_id=run_id)

    async def get_state(self, *, thread_id):
        return self.threads[thread_id]

    async def cancel(self, *, thread_id, run_id):
        self.runs[run_id] = AgentRun(thread_id, run_id, "interrupted")
        self.release.set()

    async def close(self):
        self.release.set()
        self.return_admission.set()
        await asyncio.gather(*self.jobs, return_exceptions=True)


def context(runs, owner="alice"):
    return ActionContext(ExtensionPrincipal(owner), {"enabled": True}, agent_runs=runs)
