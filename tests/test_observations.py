"""Passive read-only observation for checkpoint_mode='observe'.

No model, network, credential or bwrap access is required: tests drive a fake
PiProtocol lifecycle (and, in one case, a real local subprocess) and assert that
the observer produces bounded records on schedule, sends no steer, stops for
nothing, and leaves the process free to finish. The observer is metadata only and
never spawns subprocesses, runs Git or tests.
"""
import json
import os
import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

import unittest
from unittest.mock import patch

from codinator.agents import PiProtocol
from codinator.config import load_manifest
from codinator.observations import Observer, status as observation_status
from codinator.process import Interrupted, run_process
from codinator.files import Problem
import codinator.agents as agents_mod

STATE = {"model": {"provider": "bonsai", "id": "bonsai2-27b"},
         "thinkingLevel": "xhigh", "isStreaming": False, "pendingMessageCount": 0,
         "sessionId": "fake"}

HANDOFF_DOC = ("\n## Goal\nObserve demo.\n"
               "\n## Scope and constraints\nRead-only observer.\n"
               "\n## Required inputs\nFixture files.\n"
               "\n## Deliverables\nRecords and status.\n"
               "\n## Acceptance criteria\nRecords on schedule, no steer, no stop.\n"
               "\n## Handoff rules\nContinue the same authorized task.\n")


def _valid_manifest(ws, **overrides):
    value = {
        "version": 2,
        "handoff_protocol": 1,
        "id": "OBDEMO",
        "workspace": str(ws),
        "git": {"branch": "obdemo", "base_commit": "0" * 64},
        "handoff": "docs/handoff.md",
        "allowed_paths": ["obs.py", "tests/test_obs.py"],
        "checks": [{"name": "unit", "argv": ["/bin/true"], "timeout_seconds": 30}],
        "checkpoint_seconds": 600,
    }
    value.update(overrides)
    return value


class ManifestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="obs-mnf-")
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        (self.ws / "docs").mkdir()
        (self.ws / "tests").mkdir()
        (self.ws / "obs.py").write_text("x = 1\n")
        (self.ws / "tests" / "test_obs.py").write_text("pass\n")
        (self.ws / "docs" / "handoff.md").write_text(HANDOFF_DOC)
        (self.ws / "outdir").mkdir()
        (self.ws / "output.txt").write_text("declared output\n")

    def load(self, manifest):
        path = Path(self.tmp.name) / "manifest.json"
        path.write_text(json.dumps(manifest))
        return load_manifest(path)

    def base(self, **overrides):
        return _valid_manifest(self.ws, **overrides)


class ObservationConfigTests(ManifestBase):
    def test_observe_valid(self):
        m = self.load(self.base(checkpoint_mode="observe"))
        self.assertEqual(m["checkpoint_mode"], "observe")
        self.assertNotIn("checkpoint_outputs", m)

    def test_observe_with_outputs(self):
        m = self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=["output.txt"]))
        self.assertEqual(m["checkpoint_outputs"], ["output.txt"])

    def test_observe_empty_outputs(self):
        m = self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=[]))
        self.assertEqual(m["checkpoint_outputs"], [])

    def test_observe_rejects_unknown_mode(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="watch"))

    def test_observe_rejects_non_string_mode(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode=1))

    def test_observe_requires_version_2(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", version=1))

    def test_observe_requires_handoff_protocol(self):
        value = self.base(checkpoint_mode="observe")
        del value["handoff_protocol"]
        with self.assertRaises(Exception):
            self.load(value)

    def test_observe_requires_checkpoint_seconds(self):
        value = self.base(checkpoint_mode="observe")
        del value["checkpoint_seconds"]
        with self.assertRaises(Exception):
            self.load(value)

    def test_observe_mutually_excludes_compact(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", checkpoint_format="compact",
                                first_checkpoint="goal", counterexamples=["e"]))

    def test_observe_mutually_excludes_first_checkpoint(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", first_checkpoint="goal"))

    def test_observe_mutually_excludes_counterexamples(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", counterexamples=["e"]))

    def test_observe_rejects_external_implementation(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", implementation="external"))

    def test_outputs_without_observe_rejected(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_outputs=["output.txt"]))

    def test_outputs_reject_absolute(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=["/abs/path.txt"]))

    def test_outputs_reject_dot(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=["./a.txt"]))

    def test_outputs_reject_double_dot(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=["sub/../a.txt"]))

    def test_outputs_reject_wildcard(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=["*"]))

    def test_outputs_reject_protected_dir(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=[".git/x.txt"]))

    def test_outputs_reject_duplicate(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=["a.txt", "a.txt"]))

    def test_outputs_reject_too_many(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe",
                               checkpoint_outputs=[f"f{i}.txt" for i in range(17)]))

    def test_outputs_reject_directory(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=["outdir"]))

    def test_outputs_reject_empty_entry(self):
        with self.assertRaises(Exception):
            self.load(self.base(checkpoint_mode="observe", checkpoint_outputs=[""]))

    def test_legacy_checkpoint_seconds_no_mode_preserved(self):
        m = self.load(self.base())
        self.assertNotIn("checkpoint_mode", m)
        self.assertEqual(m["checkpoint_seconds"], 600)

    def test_v1_has_no_observe_mode(self):
        value = dict(self.base())
        value["version"] = 1
        del value["git"]
        del value["handoff_protocol"]
        m = self.load(value)
        self.assertEqual(m["version"], 1)
        self.assertNotIn("checkpoint_mode", m)


class ObserveLifecycleTests(unittest.TestCase):
    """Fake PiProtocol lifecycle; no real process. Observer-driven timing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="obs-life-")
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Path(self.tmp.name) / "attempt-0001"
        self.ctx.mkdir(parents=True)
        (self.ctx / "pi").mkdir()
        (self.ctx / "pi" / "stdout.jsonl").write_text('{"type":"agent_start"}\n')
        (self.ctx / "pi" / "stderr.txt").write_text("")
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        (self.ws / "out.txt").write_text("output data\n")
        self.task = {"id": "OB", "round": 1, "attempt": 1}
        self.events = []
        self.observer = Observer(self.task, self.ctx, 1, outputs=("out.txt",),
                                 workspace=self.ws)

    def start(self):
        self.observer.start(100.0)

    def observe_step(self, t):
        # The controller loop must treat observation as an interruptible no-op:
        # it never raises, regardless of agent state.
        self.observer.observe(t)

    def records(self):
        return sorted(self.observer.directory.glob("record-*.json"))

    def test_active_tool_spans_cycles_without_stop_or_steering(self):
        self.start()
        self.observer.record_tool({"type": "tool_execution_start", "toolCallId": "t1"})
        for t in (101.0, 102.0, 103.0):
            self.observe_step(t)
        self.assertEqual(len(self.records()), 4)  # baseline + 3 observations
        self.assertEqual(self.observer.tool_starts_total, 1)
        self.assertIn("t1", self.observer.active_tools())

    def test_process_lifecycle_completes_in_observed_mode(self):
        class Clock:
            def __init__(self):
                self.v = 100.0

            def __call__(self):
                return self.v

        clock = Clock()
        with patch("time.monotonic", clock):
            clock.v = 100.0
            self.protocol = PiProtocol("prompt", self.ctx / "pi-runtime.json",
                                       observer=self.observer)
            self.protocol.start(self.events.append)
            # Activate: state response, prompt acknowledgement, agent start.
            self.protocol.event(
                {"type": "response", "id": "state", "success": True, "data": STATE},
                self.events.append)
            self.protocol.event({"type": "response", "id": "prompt", "success": True},
                                self.events.append)
            self.protocol.event({"type": "agent_start"}, self.events.append)
            # An active tool spans several soft-check moments; no report is sent.
            self.protocol.event({"type": "tool_execution_start", "toolCallId": "t1"},
                                self.events.append)
            for t in (101.0, 102.0, 103.0):
                clock.v = t
                self.protocol.observe(t)
            self.assertEqual(sum(e.get("type") == "steer" for e in self.events), 0)
            self.assertEqual(len([p for p in self.observer.directory.glob("record-*.json")]),
                             1 + 3)  # baseline + 3 observations
            # The process completes normally; observation must not stop or extend it.
            clock.v = 105.0
            self.protocol.event({"type": "tool_execution_end", "toolCallId": "t1"},
                                self.events.append)
            self.protocol.event({"type": "message_end", "message": {"role": "assistant",
                                                                   "stopReason": "stop"}},
                                self.events.append)
            self.protocol.event({"type": "agent_end"}, self.events.append)
            self.protocol.event({"type": "agent_settled"}, self.events.append)
            self.protocol.finish()
        self.assertEqual(len(self.records()), 5)  # baseline, 3 observations, final

    def test_observer_stays_active_through_agent_lifecycle(self):
        self.start()
        for t in (101.0, 102.0, 103.0, 104.0):
            self.observe_step(t)
        self.assertGreaterEqual(len(self.records()), 5)


class ObserveBoundaryTests(unittest.TestCase):
    """Read-boundary and fault injection: missing, partial JSON, oversized,
    truncation/replacement, symlinks, FIFO, directory, non-text."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="obs-bound-")
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Path(self.tmp.name) / "attempt-0001"
        self.ctx.mkdir(parents=True)
        (self.ctx / "pi").mkdir()
        (self.ctx / "pi" / "stdout.jsonl").write_text("{}\n")
        (self.ctx / "pi" / "stderr.txt").write_text("")
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        self.task = {"id": "OB", "round": 1, "attempt": 1}

    def make(self, *outputs):
        self.observer = Observer(self.task, self.ctx, 1, outputs=outputs, workspace=self.ws)
        self.observer.start(100.0)

    def sources(self, now):
        self.observer.observe(now)
        latest = observation_status(self.ctx, "OB", 1)
        return {s["path"]: s for s in latest["latest"]["sources"]}

    def test_missing_file_recorded(self):
        self.make("missing.txt")
        self.assertEqual(self.sources(101.0)["missing.txt"]["status"], "missing")

    def test_symlink_file_rejected(self):
        (self.ws / "real.txt").write_text("secret\n")
        os.symlink("real.txt", self.ws / "link.txt")
        self.make("link.txt")
        s = self.sources(101.0)["link.txt"]
        self.assertEqual(s["status"], "unreadable")
        self.assertEqual(s["error"], "symlink")

    def test_symlink_escape_parent_rejected(self):
        outside = Path(self.tmp.name) / "outside"
        os.makedirs(outside, exist_ok=True)
        (outside / "out.txt").write_text("outside content\n")
        os.symlink(str(outside), self.ws / "linked")
        self.make("linked/out.txt")
        s = self.sources(101.0)["linked/out.txt"]
        self.assertIn(s["status"], ("unreadable", "missing"))

    def test_fifo_special_file_rejected(self):
        os.mkfifo(self.ws / "pipe")
        self.make("pipe")
        s = self.sources(101.0)["pipe"]
        self.assertEqual(s["status"], "unreadable")
        self.assertEqual(s["error"], "special_file")

    def test_directory_rejected(self):
        (self.ws / "outdir").mkdir(parents=True)
        self.make("outdir")
        s = self.sources(101.0)["outdir"]
        self.assertEqual(s["status"], "unreadable")
        self.assertEqual(s["error"], "directory")

    def test_non_utf8_recorded(self):
        self.make("bad.bin")
        (self.ws / "bad.bin").write_bytes(b"\xff\xfe\x00\x01")
        s = self.sources(101.0)["bad.bin"]
        self.assertEqual(s["status"], "read")
        self.assertFalse(s["utf8"])

    def test_partial_json_flagged(self):
        (self.ctx / "pi" / "stdout.jsonl").write_text('{"type":"a"}\n{"broken":\n')
        self.make()
        s = self.sources(101.0)["pi/stdout.jsonl"]
        self.assertTrue(s["jsonl"]["incomplete"])

    def test_truncation_detected(self):
        self.make("out.txt")
        (self.ws / "out.txt").write_text("aaaaaaa\n")
        self.sources(101.0)
        (self.ws / "out.txt").write_text("ab\n")
        self.assertTrue(self.sources(102.0)["out.txt"]["truncated"])

    def test_replacement_detected(self):
        self.make("out.txt")
        (self.ws / "out.txt").write_text("aaaaaaa\n")
        self.sources(101.0)
        (self.ws / "out.txt").write_text("zzzzzzz\n")
        self.assertTrue(self.sources(102.0)["out.txt"]["content_changed"])

    def test_bounded_read_does_not_copy_full_file(self):
        self.make("out.txt")
        (self.ws / "out.txt").write_text("x" * (1_000_000))
        s = self.sources(101.0)["out.txt"]
        self.assertEqual(s["status"], "read")
        self.assertLessEqual(s["bytes_read"], 32 * 1024)
        self.assertGreater(s["bytes_read"], 0)

    def test_record_size_below_limit(self):
        self.make("out.txt")
        (self.ws / "out.txt").write_text("output\n")
        for rec in self.observer.directory.glob("record-*.json"):
            self.assertLessEqual(rec.stat().st_size, 1_000_000)

    def test_observer_spawns_no_subprocess(self):
        self.make("out.txt")
        (self.ws / "out.txt").write_text("x\n")
        calls = []

        def boom(*a, **k):
            calls.append(a)
            raise RuntimeError("subprocess forbidden")

        with patch.object(subprocess, "Popen", boom), patch.object(subprocess, "run", boom):
            self.observer.observe(101.0)
            self.observer.finish()
        self.assertEqual(calls, [])


class ObserveStatusTests(unittest.TestCase):
    """status() displays the new mode, count, latest observation and evidence paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="obs-status-")
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Path(self.tmp.name) / "attempt-0001"
        self.ctx.mkdir(parents=True)
        (self.ctx / "pi").mkdir()
        (self.ctx / "pi" / "stdout.jsonl").write_text("{}\n")
        (self.ctx / "pi" / "stderr.txt").write_text("")
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        (self.ws / "out.txt").write_text("data\n")
        self.task = {"id": "OB", "round": 1, "attempt": 1}

    def test_status_shows_observe_mode(self):
        self.observer = Observer(self.task, self.ctx, 1, outputs=("out.txt",), workspace=self.ws)
        self.observer.start(100.0)
        self.observer.observe(101.0)
        st = observation_status(self.ctx, "OB", 1)
        self.assertIsNotNone(st)
        self.assertEqual(st["mode"], "observe")
        self.assertEqual(st["latest_checkpoint"], 1)
        self.assertGreaterEqual(st["count"], 2)
        self.assertTrue(st["latest"]["note"].strip())

    def test_status_none_when_no_observe(self):
        self.assertIsNone(observation_status(self.ctx, "OB", 1))

    def test_status_wrong_attempt_returns_none(self):
        self.observer = Observer(self.task, self.ctx, 1)
        self.observer.start(100.0)
        self.assertIsNone(observation_status(self.ctx, "OB", 2))


class ObserveRealProcessTests(unittest.TestCase):
    """One real local subprocess: an active tool spans soft-check moments and the
    process still finishes without being interrupted by the observer."""

    FAKE_AGENT = r"""
import json, sys, time

def emit(e):
    sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n")
    sys.stdout.flush()

STATE = {"model": {"provider": "bonsai", "id": "bonsai2-27b"},
         "thinkingLevel": "xhigh", "isStreaming": False, "pendingMessageCount": 0,
         "sessionId": "fake"}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    kind = req.get("type")
    if kind == "get_state":
        emit({"type": "response", "id": "state", "success": True, "data": STATE})
    elif kind == "prompt":
        emit({"type": "response", "id": "prompt", "success": True})
        emit({"type": "agent_start"})
        emit({"type": "tool_execution_start", "toolCallId": "sleep-tool", "name": "sleep"})
        time.sleep(__SLEEP__)
        emit({"type": "tool_execution_end", "toolCallId": "sleep-tool"})
        emit({"type": "message_end", "message": {"role": "assistant",
                                                 "stopReason": "stop", "text": "done"}})
        emit({"type": "agent_end"})
        emit({"type": "agent_settled"})
        break
    else:
        emit({"type": "response", "id": req.get("id"), "success": True})
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="obs-proc-")
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        (self.ws / "obs.py").write_text("x = 1\n")
        self.ctx = Path(self.tmp.name) / "attempt-0001"
        self.ctx.mkdir(parents=True)
        (self.ws / "out.txt").write_text("output\n")
        self.task = {"id": "OB", "round": 1, "attempt": 1}
        self.agent = Path(self.tmp.name) / "_fake_agent.py"
        self.agent.write_text(self.FAKE_AGENT.replace("__SLEEP__", "2.0"))
        self.observer = Observer(self.task, self.ctx, 1, outputs=("out.txt",),
                                 workspace=self.ws)
        self.protocol = PiProtocol("prompt", self.ctx / "pi-runtime.json",
                                   observer=self.observer)

    def _run_process(self):
        env = dict(os.environ)
        for key in list(env):
            if key.lower().endswith("_proxy"):
                del env[key]
        argv = [sys.executable, str(self.agent)]
        code = run_process(argv, cwd=self.ws, env=env, out=self.ctx / "pi",
                           timeout=30, protocol=self.protocol)
        return code

    def test_process_completes_with_active_tool(self):
        code = self._run_process()
        self.assertEqual(code, 0)
        result = json.loads((self.ctx / "pi" / "result.json").read_text())
        self.assertEqual(result["protocol_completed"], True)
        self.assertGreaterEqual(self.observer.tool_starts_total, 1)
        records = sorted(self.observer.directory.glob("record-*.json"))
        self.assertGreaterEqual(len(records), 3)
        any_active = any(
            json.loads(p.read_text()).get("tool_events", {}).get("active_count", 0) >= 1
            for p in records)
        self.assertTrue(any_active, "no observation captured the active tool")

class ObserveBaselineTimingTests(unittest.TestCase):
    """Baseline next_due is start + interval; the first observation may only
    fire after a full interval has elapsed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="obs-tim-")
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Path(self.tmp.name) / "attempt-0001"
        self.ctx.mkdir(parents=True)
        (self.ctx / "pi").mkdir()
        (self.ctx / "pi" / "stdout.jsonl").write_text("{}\n")
        (self.ctx / "pi" / "stderr.txt").write_text("")
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        (self.ws / "out.txt").write_text("output\n")
        self.task = {"id": "OB", "round": 1, "attempt": 1}
        self.observer = Observer(self.task, self.ctx, 60, outputs=("out.txt",),
                                 workspace=self.ws)

    def start(self):
        self.observer.start(100.0)

    def records(self):
        return sorted(self.observer.directory.glob("record-*.json"))

    def test_baseline_next_due_is_start_plus_interval(self):
        self.start()
        self.assertEqual(self.observer.next_due, 160.0)
        # No sampling before one full interval has elapsed.
        self.observer.observe(159.9)
        self.assertEqual(len(self.records()), 1)

    def test_first_observation_at_interval(self):
        self.start()
        self.observer.observe(160.0)
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.observer.next_due, 220.0)
        self.observer.observe(179.9)
        self.assertEqual(len(self.records()), 2)

    def test_records_carry_contract_identity(self):
        self.start()
        self.observer.observe(160.0)
        for p in self.records():
            v = json.loads(p.read_text())
            self.assertEqual(v["task_id"], "OB")
            self.assertEqual(v["round"], 1)
            self.assertEqual(v["attempt"], 1)
            self.assertEqual(v["mode"], "observe")
            self.assertEqual(v["interval_seconds"], 60)
            self.assertEqual(v["version"], 1)

    def test_status_contract_bound_to_records(self):
        self.start()
        self.observer.observe(160.0)
        st = observation_status(self.ctx, "OB", 1)
        self.assertIsNotNone(st)
        self.assertEqual(st["contract"]["mode"], "observe")
        self.assertEqual(st["contract"]["task_id"], "OB")
        self.assertEqual(st["contract"]["round"], 1)
        self.assertEqual(st["contract"]["attempt"], 1)
        self.assertEqual(st["latest"]["task_id"], "OB")
        self.assertEqual(st["latest"]["round"], 1)
        self.assertEqual(st["latest"]["attempt"], 1)
        self.assertIsNot(st["contract"], st["latest"])
        # A record with a conflicting identity is not counted.
        bad = dict(st["latest"])
        bad["attempt"] = 2
        (self.observer.directory / "record-0099.json").write_text(json.dumps(bad) + "\n")
        st2 = observation_status(self.ctx, "OB", 1)
        self.assertEqual(st2["count"], 2)
        self.assertEqual(st2["latest_checkpoint"], 1)
        self.assertEqual(st2["contract"]["attempt"], 1)
        # A record missing an identity field is also not counted.
        bad2 = dict(st["latest"])
        del bad2["attempt"]
        (self.observer.directory / "record-0098.json").write_text(json.dumps(bad2) + "\n")
        st3 = observation_status(self.ctx, "OB", 1)
        self.assertEqual(st3["count"], 2)
        self.assertIsNone(observation_status(self.ctx, "OB", 2))


class ObserveSafeReadBoundsTests(unittest.TestCase):
    """inode/dev/range, write-in-progress hints, and versioned non-overwritten
    fragments."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="obs-bound2-")
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Path(self.tmp.name) / "attempt-0001"
        self.ctx.mkdir(parents=True)
        (self.ctx / "pi").mkdir()
        (self.ctx / "pi" / "stdout.jsonl").write_text("{}\n")
        (self.ctx / "pi" / "stderr.txt").write_text("")
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        (self.ws / "out.txt").write_text("abc\n")
        self.task = {"id": "OB", "round": 1, "attempt": 1}

    def make(self, interval=1):
        self.observer = Observer(self.task, self.ctx, interval,
                                 outputs=("out.txt",), workspace=self.ws)
        self.observer.start(100.0)
        return self.observer

    def load(self, number):
        return json.loads((self.observer.directory /
                           ("record-%04d.json" % number)).read_text())

    def source(self, record, rel):
        return [s for s in record["sources"] if s["path"] == rel][0]

    def test_inode_dev_range_recorded(self):
        obs = self.make()
        base = self.load(0)
        s = self.source(base, "out.txt")
        st = os.lstat(self.ws / "out.txt")
        self.assertEqual(s["inode"], st.st_ino)
        self.assertEqual(s["dev"], st.st_dev)
        self.assertEqual(s["range"], [0, 4])
        self.assertEqual(s["bytes_read"], 4)
        self.assertEqual(s["sha256_sample"], hashlib.sha256(b"abc\n").hexdigest())
        self.assertFalse(s["truncated"])
        self.assertFalse(s["content_changed"])
        self.assertFalse(s["previously_read"])
        (self.ws / "out.txt").write_text("0" * 5 + "tail\n")
        self.observer.observe(101.0)
        s2 = self.source(self.load(1), "out.txt")
        self.assertTrue(s2["content_changed"])
        self.assertFalse(s2["truncated"])
        self.assertEqual(s2["range"], [0, 10])
        self.assertEqual(s2["bytes_read"], 10)

    def test_write_in_progress_hint(self):
        obs = self.make()
        (self.ws / "out.txt").write_text("no trailing newline")
        self.observer.observe(101.0)
        s = self.source(self.load(1), "out.txt")
        self.assertTrue(s["write_in_progress"])
        (self.ws / "out.txt").write_text("complete\n")
        self.observer.observe(102.0)
        s2 = self.source(self.load(2), "out.txt")
        self.assertFalse(s2["write_in_progress"])

    def test_fragments_versioned_and_not_overwritten(self):
        obs = self.make()
        (self.ws / "out.txt").write_text("first\n")
        self.observer.observe(101.0)
        rec1 = self.load(1)
        (self.ws / "out.txt").write_text("second\n")
        self.observer.observe(102.0)
        rec2 = self.load(2)
        s1, s2 = self.source(rec1, "out.txt"), self.source(rec2, "out.txt")
        self.assertIsNotNone(s1["fragment_path"])
        self.assertIsNotNone(s2["fragment_path"])
        self.assertNotEqual(s1["fragment_path"], s2["fragment_path"])
        d = self.observer.directory
        b1 = (d / s1["fragment_path"]).read_bytes()
        b2 = (d / s2["fragment_path"]).read_bytes()
        self.assertEqual(b1, b"first\n")
        self.assertEqual(b2, b"second\n")
        self.assertEqual(s1["fragment_bytes"], len(b1))
        self.assertEqual(s2["fragment_bytes"], len(b2))
        self.assertEqual(s1["fragment_range"], [0, 6])
        self.assertEqual(s2["fragment_range"], [0, 7])
        names = {p.name for p in (d / "outputs") if p.is_file()}
        self.assertEqual(len(names), 3)


class ObserveRealCancelTimeoutTests(unittest.TestCase):
    """Real local subprocess: cancel, hard timeout, and final delivery absence."""

    FAKE_AGENT = r"""
import json, sys, time

def emit(e):
    sys.stdout.write(json.dumps(e, ensure_ascii=False) + "\n")
    sys.stdout.flush()

STATE = {"model": {"provider": "bonsai", "id": "bonsai2-27b"},
         "thinkingLevel": "xhigh", "isStreaming": False, "pendingMessageCount": 0,
         "sessionId": "fake"}

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    req = json.loads(line)
    kind = req.get("type")
    if kind == "get_state":
        emit({"type": "response", "id": "state", "success": True, "data": STATE})
    elif kind == "prompt":
        emit({"type": "response", "id": "prompt", "success": True})
        emit({"type": "agent_start"})
        emit({"type": "tool_execution_start", "toolCallId": "sleep-tool", "name": "sleep"})
        time.sleep(__SLEEP__)
        emit({"type": "tool_execution_end", "toolCallId": "sleep-tool"})
        emit({"type": "message_end", "message": {"role": "assistant",
                                                 "stopReason": "stop", "text": "done"}})
        emit({"type": "agent_end"})
        emit({"type": "agent_settled"})
        break
    else:
        emit({"type": "response", "id": req.get("id"), "success": True})
"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="obs-lif-")
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        self.ctx = Path(self.tmp.name) / "attempt-0001"
        self.ctx.mkdir(parents=True)
        (self.ws / "out.txt").write_text("output\n")
        self.task = {"id": "OB", "round": 1, "attempt": 1}
        self.observer = Observer(self.task, self.ctx, 1,
                                 outputs=("out.txt",), workspace=self.ws)
        self.protocol = PiProtocol("prompt", self.ctx / "pi-runtime.json",
                                   observer=self.observer)
        self.agent = Path(self.tmp.name) / "_fake_agent.py"

    def write_agent(self, sleep_seconds):
        self.agent.write_text(self.FAKE_AGENT.replace("__SLEEP__", sleep_seconds))

    def _run(self, timeout=30.0, cancel=None, **kw):
        cancel = cancel if cancel is not None else (lambda: False)
        env = dict(os.environ)
        for key in list(env):
            if key.lower().endswith("_proxy"):
                del env[key]
        return run_process([sys.executable, str(self.agent)], cwd=self.ws, env=env,
                           out=self.ctx / "pi", timeout=timeout,
                           protocol=self.protocol, cancel=cancel, **kw)

    def test_cancel_stops_process_and_records_active_tool(self):
        self.write_agent("2.0")
        def cancel():
            # Cancel once the first full-interval observation exists, while the
            # tool is still the active one.
            return (self.observer.directory / "record-0001.json").exists()
        with self.assertRaises(Interrupted):
            self._run(cancel=cancel)
        result = json.loads((self.ctx / "pi" / "result.json").read_text())
        self.assertFalse(result["protocol_completed"])
        self.assertTrue(result["failure"])
        self.assertNotEqual(result.get("process_exit_code"), 0)
        records = sorted(self.observer.directory.glob("record-*.json"))
        self.assertGreaterEqual(len(records), 2)
        any_active = any(
            json.loads(p.read_text()).get("tool_events", {}).get("active_count", 0) >= 1
            for p in records)
        self.assertTrue(any_active, "no observation captured the active tool")
        self.assertFalse((self.ctx / "delivery" / "completion.json").exists())

    def test_hard_timeout_stops_process(self):
        self.write_agent("2.0")
        with self.assertRaises(Problem):
            self._run(timeout=0.5)
        result = json.loads((self.ctx / "pi" / "result.json").read_text())
        self.assertFalse(result["protocol_completed"])
        self.assertTrue("budget" in str(result["failure"]))
        self.assertFalse((self.ctx / "delivery" / "completion.json").exists())

    def test_final_delivery_absent_stays_unverified(self):
        self.write_agent("2.0")
        code = self._run()
        self.assertEqual(code, 0)
        result = json.loads((self.ctx / "pi" / "result.json").read_text())
        self.assertTrue(result["protocol_completed"])
        # The fake agent never invoked the bound delivery tool: no submission
        # exists, and no observation record claims acceptance.
        self.assertFalse((self.ctx / "delivery" / "completion.json").exists())
        for p in sorted(self.observer.directory.glob("record-*.json")):
            v = json.loads(p.read_text())
            self.assertIn("not progress, test success or acceptance", v["note"])
            self.assertIn(v.get("kind"), ("baseline", "observation", "final"))


class ObserveWorkerPromptTests(unittest.TestCase):
    """Worker prompt in observe mode: no timed-report obligations; final
    delivery and summary duties remain."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp", prefix="obs-prom-")
        self.addCleanup(self.tmp.cleanup)
        self.ws = Path(self.tmp.name) / "ws"
        self.ws.mkdir()
        (self.ws / "obs.py").write_text("x = 1\n")
        (self.ws / "docs").mkdir()
        (self.ws / "tests").mkdir()
        (self.ws / "tests" / "test_obs.py").write_text("pass\n")
        (self.ws / "docs" / "handoff.md").write_text(HANDOFF_DOC)
        manifest_path = Path(self.tmp.name) / "manifest.json"
        manifest_path.write_text(json.dumps({
            "version": 2,
            "handoff_protocol": 1,
            "id": "WOBS",
            "workspace": str(self.ws),
            "git": {"branch": "wo-obs", "base_commit": "0" * 64},
            "handoff": "docs/handoff.md",
            "allowed_paths": ["obs.py", "tests/test_obs.py"],
            "checks": [{"name": "unit", "argv": ["/bin/true"], "timeout_seconds": 30}],
            "checkpoint_mode": "observe",
            "checkpoint_seconds": 60,
            "attempt_seconds": 7200,
            "max_seconds": 7200,
            "max_rounds": 4,
        }))
        self.task = {"id": "WOBS", "round": 1, "attempt": 1,
                     "manifest": load_manifest(manifest_path),
                     "expected_digest": "0" * 64,
                     "feedback": "Initial implementation."}
        self.attempt_dir = Path(self.tmp.name) / "attempt-0001"
        self.attempt_dir.mkdir()
        self.private_dir = Path(self.tmp.name) / "private"
        self.private_dir.mkdir()

    def test_observe_prompt_has_no_report_obligation(self):
        with patch.object(agents_mod, "_pi_worker") as mock_worker, \
             patch.object(agents_mod, "_document_instructions", return_value=""), \
             patch.object(agents_mod, "Closeout", return_value=object()):
            agents_mod.worker(self.task, self.attempt_dir, self.private_dir,
                              object(), {"timeout": 30.0})
        prompt = mock_worker.call_args[0][0]
        self.assertIn("Passive output observation every 60 seconds", prompt)
        self.assertNotIn("within five minutes", prompt)
        self.assertNotIn("valid progress report", prompt)
        self.assertNotIn("Bound progress command", prompt)
        self.assertNotIn("Soft checkpoints every", prompt)
        # Final delivery and summary duties remain.
        self.assertIn("Bound submission command", prompt)
        self.assertIn("final delivery summary/evidence duties remain", prompt)

if __name__ == "__main__":
    unittest.main()
