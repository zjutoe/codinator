"""Bounded local fake-RPC tests; no model, network or credential access."""
import contextlib
import io
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from codinator.agents import PiProtocol
from codinator.checkpoints import Checkpoints, main, status
from codinator.config import load_manifest
from codinator.delivery import validate_delivery, DeliveryError
from codinator.files import Problem
from codinator.process import Interrupted, run_process
from codinator.status import report


STATE = {'model': {'provider': 'bonsai', 'id': 'bonsai2-27b'}, 'thinkingLevel': 'xhigh',
         'isStreaming': False, 'pendingMessageCount': 0, 'sessionId': 'fake'}
PROGRESS = {'completed': ['Inspected the contract'], 'checks': [], 'blockers': [],
            'next_step': 'Implement the bounded change', 'needs_guidance': False}


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='checkpoint-test-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = self.root / 'tasks/T/attempt-0001'
        self.context.mkdir(parents=True)
        self.task = {'id': 'T', 'round': 1, 'attempt': 1}
        self.checkpoints = Checkpoints(self.task, self.context, 1)
        self.events = []
        self.protocol = PiProtocol('one implementation prompt', self.context / 'pi-runtime.json',
                                   checkpoints=self.checkpoints)
        with patch('codinator.agents.time.monotonic', return_value=100):
            self.protocol.start(self.events.append)

    def activate(self, *, ack=True):
        self.protocol.event({'type': 'response', 'id': 'state', 'success': True, 'data': STATE}, self.events.append)
        if ack:
            self.protocol.event({'type': 'response', 'id': 'prompt', 'success': True}, self.events.append)
        self.protocol.event({'type': 'agent_start'}, self.events.append)

    def checkpoint_status(self):
        return status(self.context, 'T', 1)

    def submit(self, progress=PROGRESS, request=1):
        source = self.root / 'progress.json'
        source.write_text(json.dumps(progress))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(['--request', str(request), '--report', str(source)],
                        contract_path=self.context / 'checkpoint-contract.json')
        return code, stdout.getvalue(), stderr.getvalue()

    def test_only_acknowledged_active_session_gets_periodic_steer(self):
        self.protocol.tick(101, self.events.append)
        self.activate(ack=False)
        self.protocol.tick(102, self.events.append)
        self.assertFalse(any(e['type'] == 'steer' for e in self.events))
        self.protocol.event({'type': 'response', 'id': 'prompt', 'success': True}, self.events.append)
        self.protocol.tick(102, self.events.append)
        self.protocol.tick(102.9, self.events.append)
        self.protocol.tick(103, self.events.append)
        self.assertEqual([e['id'] for e in self.events if e['type'] == 'steer'], ['checkpoint-0001', 'checkpoint-0002'])
        self.assertEqual(sum(e['type'] == 'prompt' for e in self.events), 1)
        requests = self.checkpoint_status()
        self.assertEqual(requests['unanswered_requests'], [1, 2])
        self.assertIsNone(requests['latest_report'])

    def test_agent_end_final_stop_and_settled_suppress_steer(self):
        self.activate()
        self.protocol.event({'type': 'agent_end'}, self.events.append)
        self.protocol.tick(101, self.events.append)
        self.protocol.event({'type': 'agent_start'}, self.events.append)
        self.protocol.event({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop'}}, self.events.append)
        self.protocol.tick(102, self.events.append)
        self.protocol.event({'type': 'agent_settled'}, self.events.append)
        self.protocol.tick(103, self.events.append)
        self.protocol.finish()
        self.assertFalse(any(e['type'] == 'steer' for e in self.events))

    def test_acknowledgement_is_not_a_report_and_rejection_is_evidence(self):
        self.activate()
        self.protocol.tick(101, self.events.append)
        event = {'type': 'response', 'id': 'checkpoint-0001', 'success': True}
        self.protocol.event(event, self.events.append)
        value = self.checkpoint_status()
        self.assertTrue(value['latest_request']['rpc_accepted'])
        self.assertFalse(value['latest_request']['report_received'])
        self.assertIsNone(value['latest_report'])
        self.assertEqual(value['unanswered_requests'], [1])
        with self.assertRaises(Problem):
            self.protocol.event(event, self.events.append)
        self.protocol.tick(102, self.events.append)
        with self.assertRaisesRegex(Problem, 'rejected checkpoint'):
            self.protocol.event({'type': 'response', 'id': 'checkpoint-0002', 'success': False,
                                 'error': 'fake rejection'}, self.events.append)
        self.assertFalse(self.checkpoint_status()['latest_request']['rpc_accepted'])

    def test_bound_progress_is_not_completion_and_preserves_prior_reports(self):
        self.activate()
        self.protocol.tick(101, self.events.append)
        code, output, _ = self.submit()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['status'], 'progress_recorded')
        target = self.checkpoints.directory / 'reports/0001.json'
        before = (target.read_bytes(), target.stat().st_ino)
        self.assertEqual(self.submit()[0], 0)
        self.assertEqual(self.submit(PROGRESS | {'needs_guidance': True})[0], 2)
        self.assertEqual((target.read_bytes(), target.stat().st_ino), before)
        value = self.checkpoint_status()
        self.assertEqual(value['unanswered_requests'], [])
        self.assertEqual(value['latest_report']['progress'], PROGRESS)
        with self.assertRaises(DeliveryError):
            validate_delivery(self.context / 'delivery', 'T', 1, 1)
        self.assertFalse((self.context / 'delivery/completion.json').exists())

    def test_report_requires_real_request_and_strict_progress_schema(self):
        self.assertEqual(self.submit()[0], 2)
        self.activate()
        self.protocol.tick(101, self.events.append)
        for change in ({'status': 'accepted'}, {'needs_guidance': 1}, {'completed': 'done'},
                       {'checks': [{'argv': ['test'], 'result': 'pass', 'evidence': 'log'}]},
                       {'checks': [{'argv': ['test'], 'exit_code': True, 'result': 'pass', 'evidence': 'log'}]},
                       {'next_step': ''}):
            with self.subTest(change=change):
                self.assertEqual(self.submit(PROGRESS | change)[0], 2)
        source = self.root / 'progress.json'
        source.write_text(json.dumps(PROGRESS))
        argv = shlex.split(self.checkpoints.command)
        result = subprocess.run(argv + ['--request', '1', '--report', str(source), '--task-id', 'other'],
                                capture_output=True, env={'PATH': os.defpath}, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.checkpoints.directory / 'reports/0001.json').exists())

    def test_completed_check_preserves_actual_nonzero_exit_code(self):
        self.activate()
        self.protocol.tick(101, self.events.append)
        progress = PROGRESS | {'checks': [{'argv': ['python', '-m', 'unittest'], 'exit_code': 1,
                                           'result': 'One failed assertion', 'evidence': '/tmp/check.log'}]}
        self.assertEqual(self.submit(progress)[0], 0)
        self.assertEqual(self.checkpoint_status()['latest_report']['progress']['checks'][0]['exit_code'], 1)

    def test_symlink_duplicate_keys_and_wrong_attempt_are_rejected(self):
        self.activate()
        self.protocol.tick(101, self.events.append)
        self.assertEqual(self.submit()[0], 0)
        target = self.checkpoints.directory / 'reports/0001.json'
        original = json.loads(target.read_text())
        target.write_text(json.dumps(original | {'attempt': 2}))
        with self.assertRaises(Problem):
            self.checkpoint_status()
        target.write_text(json.dumps(original)[:-1] + ', "attempt": 1}')
        with self.assertRaises(Problem):
            self.checkpoint_status()
        target.unlink()
        target.symlink_to(self.root / 'progress.json')
        with self.assertRaises(Problem):
            self.checkpoint_status()
        with self.assertRaises(Problem):
            status(self.context, 'T', 2)

    def test_status_keeps_report_and_unanswered_later_request_distinct(self):
        self.activate()
        self.protocol.tick(101, self.events.append)
        self.assertEqual(self.submit()[0], 0)
        self.protocol.tick(102, self.events.append)
        task = self.task | {'state': 'implementing', 'phase': 'pi', 'reason': '', 'control': None,
            'deadline': 1000, 'attempt_seconds_override': None, 'pid': None, 'pid_start': None,
            'manifest': {'version': 1, 'workspace': str(self.root), 'attempt_seconds': 5400, 'checkpoint_seconds': 1800}}
        before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*'))
        value = report(SimpleNamespace(root=self.root), task)
        progress = value['latest_checkpoint']
        self.assertEqual(value['state'], 'implementing')
        self.assertEqual(progress['latest_request']['checkpoint'], 2)
        self.assertEqual(progress['latest_report']['checkpoint'], 1)
        self.assertFalse(progress['latest_report']['progress']['needs_guidance'])
        self.assertEqual(progress['unanswered_requests'], [2])
        self.assertEqual(sorted(str(p.relative_to(self.root)) for p in self.root.rglob('*')), before)


class ConfigTests(unittest.TestCase):
    def test_optional_checkpoint_interval_and_invalid_values(self):
        with tempfile.TemporaryDirectory(prefix='checkpoint-config-', dir='/tmp') as tmp:
            root = Path(tmp)
            (root / 'handoff.md').write_text('Fixed contract')
            value = {'version': 1, 'id': 'T', 'workspace': tmp, 'handoff': 'handoff.md',
                     'allowed_paths': ['product.py'], 'checks': [{'name': 'unit', 'argv': ['true']}],
                     'attempt_seconds': 5400}
            path = root / 'manifest.json'
            path.write_text(json.dumps(value))
            self.assertNotIn('checkpoint_seconds', load_manifest(path))
            path.write_text(json.dumps(value | {'checkpoint_seconds': 1800}))
            self.assertEqual(load_manifest(path)['checkpoint_seconds'], 1800)
            for interval in (0, -1, True, 1.5, '1800', None, 5400, 5401):
                with self.subTest(interval=interval), self.assertRaises(Problem):
                    path.write_text(json.dumps(value | {'checkpoint_seconds': interval}))
                    load_manifest(path)


class ProcessCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='checkpoint-process-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = self.root / 'attempt'
        self.context.mkdir()
        self.checkpoints = Checkpoints({'id': 'T', 'round': 1, 'attempt': 1}, self.context, 1)
        self.protocol = PiProtocol('single prompt', self.context / 'pi-runtime.json', checkpoints=self.checkpoints)

    def run_agent(self, behavior, **kwargs):
        code = f'''
import json, sys, time, fcntl
from pathlib import Path
if {behavior!r}=='backpressure': fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
def emit(value): print(json.dumps(value), flush=True)
for line in sys.stdin:
    cmd=json.loads(line)
    with open({str(self.root / 'commands.jsonl')!r}, 'a') as f: f.write(json.dumps(cmd)+'\\n')
    if cmd['type']=='get_state':
        emit({{'type':'response','id':'state','success':True,'data':{STATE!r}}})
    elif cmd['type']=='prompt':
        if {behavior!r}=='finish':
            batch=[{{'type':'response','id':'prompt','success':True}},{{'type':'agent_start'}},
                   {{'type':'message_end','message':{{'role':'assistant','stopReason':'stop'}}}},
                   {{'type':'agent_end'}},{{'type':'agent_settled'}}]
            sys.stdout.write('\\n'.join(json.dumps(e) for e in batch)+'\\n');sys.stdout.flush()
        else:
            emit({{'type':'response','id':'prompt','success':True}})
            emit({{'type':'agent_start'}})
            if {behavior!r}=='backpressure':
                sys.stderr.write('x'*200000);sys.stderr.flush()
                time.sleep(60)
    elif cmd['type']=='steer':
        emit({{'type':'response','id':cmd['id'],'success':True}})
        if {behavior!r} in ('respond','guidance-tool','guidance-batched-tools','guidance-partial-tool'):
            import subprocess
            progress={PROGRESS!r}
            if {behavior!r} in ('guidance-tool','guidance-batched-tools','guidance-partial-tool'):
                emit({{'type':'tool_execution_start','toolCallId':'long-tool'}})
                progress['needs_guidance']=True
            p=Path({str(self.root / 'progress.json')!r});p.write_text(json.dumps(progress))
            subprocess.run({shlex.split(self.checkpoints.command)!r}+['--request','1','--report',str(p)],check=True,capture_output=True)
            if {behavior!r} in ('guidance-batched-tools','guidance-partial-tool'):
                time.sleep(.4)
                batch=[{{'type':'tool_execution_end','toolCallId':'long-tool'}},
                       {{'type':'tool_execution_start','toolCallId':'second-tool'}}]
                records='\\n'.join(json.dumps(e) for e in batch)+'\\n'
                if {behavior!r}=='guidance-partial-tool':
                    sys.stdout.write(records[:-10]);sys.stdout.flush()
                    time.sleep(.3)
                    sys.stdout.write(records[-10:]);sys.stdout.flush()
                else:
                    sys.stdout.write(records);sys.stdout.flush()
                time.sleep(.7)
                Path({str(self.root / 'tool-finished')!r}).write_text('second finished')
                emit({{'type':'tool_execution_end','toolCallId':'second-tool'}})
            elif {behavior!r}=='guidance-tool':
                time.sleep(1)
                Path({str(self.root / 'tool-finished')!r}).write_text('finished')
                emit({{'type':'tool_execution_end','toolCallId':'long-tool'}})
            emit({{'type':'message_end','message':{{'role':'assistant','stopReason':'stop'}}}})
            emit({{'type':'agent_end'}})
            emit({{'type':'agent_settled'}})
'''
        return run_process([sys.executable, '-I', '-B', '-u', '-c', code], cwd=self.root,
            env={'PATH': os.defpath}, out=self.root / 'pi', protocol=self.protocol,
            timeout=kwargs.pop('timeout', 4), **kwargs)

    def test_real_monotonic_timer_steers_same_process_and_reads_progress(self):
        self.run_agent('respond')
        commands = [json.loads(x) for x in (self.root / 'commands.jsonl').read_text().splitlines()]
        self.assertEqual([x['type'] for x in commands], ['get_state', 'prompt', 'steer'])
        progress = status(self.context, 'T', 1)
        self.assertTrue(progress['latest_request']['report_received'])
        self.assertGreaterEqual(progress['latest_request']['elapsed_seconds'], 1)
        self.assertEqual(json.loads((self.root / 'pi/result.json').read_text())['exit_code'], 0)

    def test_unresponsive_checkpoint_does_not_reset_hard_timeout(self):
        started = time.monotonic()
        with self.assertRaisesRegex(Problem, 'wall-clock budget'):
            self.run_agent('ignore', timeout=2.3)
        self.assertLess(time.monotonic() - started, 5)
        progress = status(self.context, 'T', 1)
        self.assertGreaterEqual(progress['request_count'], 1)
        self.assertIsNone(progress['latest_report'])
        self.assertEqual(progress['unanswered_requests'], list(range(1, progress['request_count'] + 1)))

    def test_pause_precedes_next_checkpoint(self):
        with self.assertRaises(Interrupted):
            self.run_agent('ignore', cancel=lambda: (self.checkpoints.directory / 'requests/0001.json').exists())
        self.assertEqual(status(self.context, 'T', 1)['request_count'], 1)
        self.assertIn('Interrupted', json.loads((self.root / 'pi/result.json').read_text())['failure'])

    def test_completed_session_never_gets_an_overdue_steer(self):
        # Make the timer already due when startup completes, but emit completion
        # in the same read batch. The terminal events must take precedence.
        with patch.object(self.checkpoints, 'tick', side_effect=AssertionError('completed session was steered')):
            self.run_agent('finish')
        self.assertEqual(status(self.context, 'T', 1)['request_count'], 0)

    def backpressure(self, *, cancel=False):
        def watchdog(*_):
            raise TimeoutError('External watchdog: RPC backpressure blocked the runner')
        previous = signal.signal(signal.SIGALRM, watchdog)
        signal.setitimer(signal.ITIMER_REAL, 10)
        started = time.monotonic()
        try:
            expected = Interrupted if cancel else Problem
            with self.assertRaises(expected):
                self.run_agent('backpressure', timeout=8 if cancel else 4.5,
                    cancel=lambda: cancel and (self.checkpoints.directory / 'requests/0004.json').exists())
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
        self.assertLess(time.monotonic() - started, 8)
        self.assertGreaterEqual(status(self.context, 'T', 1)['request_count'], 4)
        self.assertEqual((self.root / 'pi/stderr.txt').stat().st_size, 200000)
        result = json.loads((self.root / 'pi/result.json').read_text())
        self.assertIn('Interrupted' if cancel else 'wall-clock budget', result['failure'])
        from codinator.process import process_start
        process = json.loads((self.root / 'pi/process.json').read_text())
        self.assertNotEqual(process_start(process['pid']), process['start'])

    def test_rpc_stdin_backpressure_cannot_bypass_hard_timeout(self):
        self.backpressure()

    def test_rpc_stdin_backpressure_cannot_prevent_pause(self):
        self.backpressure(cancel=True)

    def test_guidance_report_waits_for_real_fake_tool_completion_then_blocks(self):
        with self.assertRaisesRegex(Problem, 'needs_guidance'):
            self.run_agent('guidance-tool')
        self.assertTrue((self.root / 'tool-finished').exists())
        value = status(self.context, 'T', 1)
        self.assertEqual(value['violation']['reason'], 'needs_guidance')
        self.assertGreater(value['stop']['stopped_elapsed_seconds'] -
                           value['violation']['detected_elapsed_seconds'], .3)

    def test_same_rpc_batch_tool_end_then_start_preserves_next_active_tool(self):
        with self.assertRaisesRegex(Problem, 'needs_guidance'):
            self.run_agent('guidance-batched-tools')
        self.assertEqual((self.root / 'tool-finished').read_text(), 'second finished')
        value = status(self.context, 'T', 1)
        self.assertGreater(value['stop']['stopped_elapsed_seconds'] -
                           value['violation']['detected_elapsed_seconds'], .7)

    def test_partial_next_tool_record_is_not_mistaken_for_safe_boundary(self):
        with self.assertRaisesRegex(Problem, 'needs_guidance'):
            self.run_agent('guidance-partial-tool')
        self.assertEqual((self.root / 'tool-finished').read_text(), 'second finished')

    def partial_output(self, mode):
        # A complete checkpoint report is independent of a tool's unfinished
        # JSONL output. Use short injected timing; the production grace is 300s.
        progress = PROGRESS | {'needs_guidance': mode == 'guidance'}
        code = f'''
import json, sys, time, subprocess
from pathlib import Path
root=Path({str(self.root)!r});context=Path({str(self.context)!r})
def emit(value): print(json.dumps(value),flush=True)
def partial():
    emit({{'type':'tool_execution_start','toolCallId':'tool'}})
    sys.stdout.write('{{"type":"tool_execution_update","data":"');sys.stdout.flush()
def finish():
    (root/'observed.json').write_text(json.dumps({{
        'resolution':(context/'checkpoints/resolutions/0001.json').exists(),
        'violation':(context/'checkpoints/violation.json').exists(),
        'requests':len(list((context/'checkpoints/requests').glob('*.json')))}}))
    sys.stdout.write('finished"}}\\n');sys.stdout.flush()
    emit({{'type':'tool_execution_end','toolCallId':'tool'}})
    delivery=context/'delivery';delivery.mkdir()
    (delivery/'summary.md').write_text('Formal fixture delivery')
    (delivery/'completion.json').write_text(json.dumps({{'task_id':'T','round':1,'attempt':1,'status':'awaiting_review'}}))
    emit({{'type':'message_end','message':{{'role':'assistant','stopReason':'stop'}}}})
    emit({{'type':'agent_end'}});emit({{'type':'agent_settled'}})
for line in sys.stdin:
    cmd=json.loads(line)
    if cmd['type']=='get_state':
        emit({{'type':'response','id':'state','success':True,'data':{STATE!r}}})
    elif cmd['type']=='prompt':
        emit({{'type':'response','id':'prompt','success':True}});emit({{'type':'agent_start'}})
        if {mode!r}=='timer':
            partial();time.sleep(1.4);finish();break
    elif cmd['type']=='steer':
        emit({{'type':'response','id':cmd['id'],'success':True}});partial()
        if {mode!r}!='missing':
            report=root/'progress.json';report.write_text(json.dumps({progress!r}))
            subprocess.run({shlex.split(self.checkpoints.command)!r}+['--request','1','--report',str(report)],check=True,capture_output=True)
        time.sleep(1);finish();break
'''
        reason = None
        with patch('codinator.checkpoints.RESPONSE_GRACE_SECONDS', .6):
            try:
                run_process([sys.executable, '-I', '-B', '-u', '-c', code], cwd=self.root,
                    env={'PATH': os.defpath}, out=self.root / 'pi', protocol=self.protocol, timeout=5)
            except Problem as exc:
                reason = str(exc)
        return reason, json.loads((self.root / 'observed.json').read_text())

    def test_timely_false_report_is_observed_during_partial_tool_output(self):
        reason, observed = self.partial_output('timely')
        self.assertIsNone(reason)
        self.assertTrue(observed['resolution'])
        self.assertFalse(observed['violation'])

    def test_guidance_latches_during_partial_output_and_waits_for_tool_end(self):
        reason, observed = self.partial_output('guidance')
        self.assertIn('needs_guidance', reason)
        self.assertTrue(observed['resolution'])
        self.assertTrue(observed['violation'])

    def test_missing_report_latches_during_partial_output(self):
        reason, observed = self.partial_output('missing')
        self.assertIn('response_timeout', reason)
        self.assertFalse(observed['resolution'])
        self.assertTrue(observed['violation'])

    def test_checkpoint_timer_runs_during_partial_tool_output(self):
        reason, observed = self.partial_output('timer')
        self.assertIsNone(reason)
        self.assertGreaterEqual(observed['requests'], 1)


class CheckpointEnforcementTests(unittest.TestCase):
    activate = CheckpointTests.activate
    submit = CheckpointTests.submit
    checkpoint_status = CheckpointTests.checkpoint_status

    def setUp(self):
        CheckpointTests.setUp(self)
        self.checkpoints.interval = 1800
        self.activate()
        self.protocol.tick(101, self.events.append)

    def tool(self, kind, identity='tool'):
        return self.protocol.event({'type': 'tool_execution_' + kind, 'toolCallId': identity}, self.events.append)

    def delivery(self):
        directory = self.context / 'delivery'
        directory.mkdir()
        (directory / 'summary.md').write_text('Formal delivery; controller checks remain required.')
        (directory / 'completion.json').write_text(json.dumps({
            'task_id': 'T', 'round': 1, 'attempt': 1, 'status': 'awaiting_review'}))

    def finish(self, now):
        self.protocol.event({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop'}}, self.events.append)
        self.protocol.event({'type': 'agent_settled'}, self.events.append)
        with patch('codinator.agents.time.monotonic', return_value=now):
            self.protocol.finish()

    def test_missing_report_blocks_after_fixed_five_minute_grace(self):
        self.protocol.tick(401, self.events.append)
        self.assertIsNone(self.checkpoints.violation)
        with self.assertRaisesRegex(Problem, 'response_timeout'):
            self.protocol.tick(401.1, self.events.append)
        value = self.checkpoint_status()
        self.assertEqual(value['response_policy']['response_grace_seconds'], 300)
        self.assertFalse(value['latest_request']['report_received'])
        self.assertEqual(value['stop']['active_tools'], [])

    def test_timeout_latches_during_active_tools_and_late_report_cannot_clear_it(self):
        self.tool('start', 'first')
        self.tool('start', 'second')
        self.protocol.tick(402, self.events.append)
        self.assertEqual(self.checkpoints.violation['reason'], 'response_timeout')
        self.assertFalse((self.checkpoints.directory / 'stop.json').exists())
        self.assertEqual(self.submit()[0], 0)
        self.tool('end', 'first')
        self.protocol.tick(403, self.events.append)
        self.assertFalse((self.checkpoints.directory / 'stop.json').exists())
        self.tool('end', 'second')
        with self.assertRaisesRegex(Problem, 'response_timeout'):
            self.protocol.tick(404, self.events.append)
        self.assertIsNone(self.checkpoint_status()['latest_resolution'])

    def test_guidance_report_blocks_before_grace_expiry_at_safe_boundary(self):
        self.tool('start')
        self.assertEqual(self.submit(PROGRESS | {'needs_guidance': True})[0], 0)
        self.protocol.tick(102, self.events.append)
        self.assertEqual(self.checkpoints.violation['reason'], 'needs_guidance')
        self.assertFalse((self.checkpoints.directory / 'stop.json').exists())
        self.tool('end')
        with self.assertRaisesRegex(Problem, 'needs_guidance'):
            self.protocol.tick(103, self.events.append)

    def test_valid_report_allows_work_past_grace_and_next_soft_checkpoint(self):
        self.assertEqual(self.submit()[0], 0)
        self.protocol.tick(102, self.events.append)
        self.protocol.tick(500, self.events.append)
        self.assertIsNone(self.checkpoints.violation)
        self.assertEqual(self.checkpoint_status()['unresolved_requests'], [])
        self.protocol.tick(1901, self.events.append)
        value = self.checkpoint_status()
        self.assertEqual(value['request_count'], 2)
        self.assertEqual(value['unresolved_requests'], [2])

    def test_wrong_attempt_report_does_not_satisfy_deadline(self):
        self.assertEqual(self.submit()[0], 0)
        path = self.checkpoints.directory / 'reports/0001.json'
        value = json.loads(path.read_text())
        path.write_text(json.dumps(value | {'attempt': 99}))
        self.protocol.tick(102, self.events.append)
        self.assertEqual(self.checkpoints.resolutions, {})
        with self.assertRaisesRegex(Problem, 'response_timeout'):
            self.protocol.tick(402, self.events.append)

    def test_timely_formal_delivery_explicitly_resolves_only_final_checkpoint(self):
        self.delivery()
        self.finish(150)
        value = self.checkpoint_status()
        self.assertFalse(value['latest_request']['report_received'])
        self.assertEqual(value['latest_request']['resolved_by'], 'final_delivery')
        self.assertIsNone(value['latest_report'])
        self.assertEqual(value['unresolved_requests'], [])
        self.assertEqual(value['unanswered_requests'], [1])
        self.assertEqual(value['latest_resolution']['final_delivery']['status'], 'awaiting_review')

    def test_late_finish_with_formal_delivery_does_not_escape_timeout(self):
        self.delivery()
        with self.assertRaisesRegex(Problem, 'response_timeout'):
            self.finish(402)
        self.assertIsNone(self.checkpoint_status()['latest_resolution'])

    def test_finish_without_report_or_formal_delivery_is_not_protocol_success(self):
        with self.assertRaisesRegex(Problem, 'missing_final_delivery'):
            self.finish(150)

    def test_earlier_unanswered_checkpoint_cannot_be_replaced_by_final_delivery(self):
        self.checkpoints.next_due = 102
        self.protocol.tick(102, self.events.append)
        self.delivery()
        with self.assertRaisesRegex(Problem, 'unfinished_checkpoints'):
            self.finish(150)

    @unittest.skipIf(os.geteuid() == 0, 'Root bypasses the real report directory permission boundary')
    def test_unreadable_report_directory_waits_for_grace_and_active_tool(self):
        reports = self.checkpoints.directory / 'reports'
        original_mode = reports.stat().st_mode & 0o777
        self.tool('start')
        reports.chmod(0)
        try:
            with self.assertRaises(PermissionError):
                (reports / '0001.json').stat()
            self.protocol.tick(150, self.events.append)
            self.assertIsNone(self.checkpoints.violation)
            self.protocol.tick(402, self.events.append)
            self.assertEqual(self.checkpoints.violation['reason'], 'response_timeout')
            self.assertFalse((self.checkpoints.directory / 'stop.json').exists())
        finally:
            reports.chmod(original_mode)
        self.tool('end')
        with self.assertRaisesRegex(Problem, 'response_timeout'):
            self.protocol.tick(403, self.events.append)

    def test_legacy_status_has_no_retroactive_enforcement(self):
        (self.checkpoints.directory / 'policy.json').unlink()
        (self.checkpoints.directory / 'resolutions').rmdir()
        before = {p: p.read_bytes() for p in self.context.rglob('*') if p.is_file()}
        value = self.checkpoint_status()
        self.assertIsNone(value['response_policy'])
        self.assertNotIn('violation', value)
        self.assertNotIn('stop', value)
        self.assertEqual(value['unanswered_requests'], [1])
        self.assertEqual({p: p.read_bytes() for p in self.context.rglob('*') if p.is_file()}, before)


if __name__ == '__main__':
    unittest.main()
