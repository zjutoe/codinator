"""Local fake RPC subprocesses exercise timers and cleanup, never model connectivity."""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest

from codinator.agents import PiProtocol
from codinator.checkpoints import Closeout
from codinator.delivery import prepare_contract, validate_delivery
from codinator.handoff import freeze_protocol, template
from codinator.files import Problem
from codinator.process import Interrupted, process_start, run_process


STATE = {'model': {'provider': 'bonsai', 'id': 'bonsai2-27b'}, 'thinkingLevel': 'xhigh',
         'isStreaming': False, 'pendingMessageCount': 0, 'sessionId': 'fake-closeout'}


class CloseoutProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='closeout-process-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.context = self.root / 'attempt'
        self.context.mkdir()
        manifest = {'version': 2, 'id': 'T', 'handoff_protocol': 1,
                    'git': {'branch': 'task/T', 'base_commit': 'a' * 40},
                    'checks': [{'name': 'unit', 'argv': ['true'], 'timeout_seconds': 1}]}
        self.task = {'id': 'T', 'round': 1, 'attempt': 1,
                     'manifest': manifest, 'expected_digest': 'a' * 40}
        frozen = self.root / 'frozen'
        frozen.mkdir()
        (frozen / 'manifest.json').write_text(json.dumps(manifest))
        (frozen / 'handoff.md').write_text(template('handoff'))
        freeze_protocol(frozen, manifest)
        self.delivery = self.context / 'delivery'
        self.delivery.mkdir()
        command = prepare_contract(self.task, self.context, self.delivery, task_dir=frozen)
        self.command = shlex.split(command)
        (self.root / 'summary-input.md').write_text(template('summary'))
        (self.root / 'evidence-input.json').write_text(json.dumps({
            'version': 1, 'task_id': 'T', 'round': 1, 'attempt': 1,
            'git': {'branch': 'task/T', 'base_commit': 'a' * 40, 'commit': 'a' * 40},
            'checks': [{'name': 'unit', 'argv': ['true'], 'commit': 'a' * 40,
                        'status': 'not_run', 'exit_code': None, 'evidence': 'Not executed in fake fixture'}]}))

    def run_fake(self, mode, *, timeout=1.2, cancel=lambda: False):
        self.closeout = Closeout(self.task, self.context, timeout)
        self.protocol = PiProtocol('One bounded fake prompt', self.context / 'pi-runtime.json',
                                   closeout=self.closeout)
        code = f'''
import json, subprocess, sys, time
from pathlib import Path
root=Path({str(self.root)!r})
def emit(value): print(json.dumps(value),flush=True)
def deliver():
    subprocess.run({self.command!r}+['--summary',str(root/'summary-input.md'),
        '--status','awaiting_review','--evidence',str(root/'evidence-input.json')],
        check=True,capture_output=True)
def terminal():
    emit({{'type':'message_end','message':{{'role':'assistant','stopReason':'stop'}}}})
    emit({{'type':'agent_end'}})
    emit({{'type':'agent_settled'}})
for line in sys.stdin:
    cmd=json.loads(line)
    with (root/'commands.jsonl').open('a') as stream:
        stream.write(json.dumps(cmd)+'\\n')
    if cmd['type']=='get_state':
        emit({{'type':'response','id':'state','success':True,'data':{STATE!r}}})
    elif cmd['type']=='prompt':
        emit({{'type':'response','id':'prompt','success':True}})
        emit({{'type':'agent_start'}})
        if {mode!r}=='early':
            deliver();terminal();break
        if {mode!r} in ('settled-tool','hung-tool'):
            emit({{'type':'tool_execution_start','toolCallId':'owned-tool'}})
    elif cmd['type']=='steer':
        emit({{'type':'response','id':cmd['id'],'success':True}})
        if {mode!r}=='complete':
            deliver();terminal();break
        if {mode!r}=='settled-tool':
            deliver();terminal()
            time.sleep(.15)
            (root/'tool-finished').write_text('tool finished after settled')
            emit({{'type':'tool_execution_end','toolCallId':'owned-tool'}})
            break
        if {mode!r}=='hung-tool':
            terminal()
        if {mode!r} in ('ignore','hung-tool'):
            time.sleep(10)
'''
        return run_process([sys.executable, '-I', '-B', '-u', '-c', code], cwd=self.root,
            env={'PATH': os.defpath}, out=self.context / 'pi', timeout=timeout,
            protocol=self.protocol, cancel=cancel)

    def result(self):
        return json.loads((self.context / 'pi/result.json').read_text())

    def commands(self):
        return [json.loads(line) for line in (self.root / 'commands.jsonl').read_text().splitlines()]

    def assert_process_stopped(self):
        record = json.loads((self.context / 'pi/process.json').read_text())
        self.assertNotEqual(process_start(record['pid']), record['start'])
        self.assertIsNotNone(self.result()['process_exit_code'])

    def test_single_near_deadline_closeout_ack_then_bound_final_summary(self):
        self.assertEqual(self.run_fake('complete'), 0)
        steers = [cmd for cmd in self.commands() if cmd['type'] == 'steer']
        self.assertEqual([cmd['id'] for cmd in steers], ['closeout-0001'])
        request = json.loads((self.closeout.directory / 'request.json').read_text())
        self.assertGreaterEqual(request['elapsed_seconds'], .6)
        self.assertLess(request['elapsed_seconds'], 1.2)
        self.assertAlmostEqual(request['response_deadline_elapsed_seconds'], 1.2)
        ack = json.loads((self.closeout.directory / 'ack.json').read_text())
        self.assertTrue(ack['success'])
        self.assertTrue(ack['within_deadline'])
        self.assertEqual(validate_delivery(self.delivery, 'T', 1, 1), 'awaiting_review')
        self.assertTrue(self.result()['protocol_completed'])
        self.assert_process_stopped()

    def test_hard_timeout_after_ack_retains_request_and_no_fabricated_summary(self):
        started = time.monotonic()
        with self.assertRaisesRegex(Problem, 'wall-clock budget'):
            self.run_fake('ignore', timeout=1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertTrue((self.closeout.directory / 'request.json').exists())
        self.assertTrue((self.closeout.directory / 'ack.json').exists())
        self.assertEqual(len([cmd for cmd in self.commands() if cmd['type'] == 'steer']), 1)
        self.assertFalse((self.delivery / 'summary.md').exists())
        self.assertFalse((self.delivery / 'completion.json').exists())
        self.assertFalse(self.result()['protocol_completed'])
        self.assert_process_stopped()

    def test_cancel_after_ack_retains_raw_records_and_missing_final_summary(self):
        with self.assertRaises(Interrupted):
            self.run_fake('ignore', cancel=lambda: (self.context / 'closeout/ack.json').exists())
        self.assertIn('Interrupted', self.result()['failure'])
        self.assertFalse(self.result()['protocol_completed'])
        self.assertTrue((self.context / 'pi/stdout.jsonl').exists())
        self.assertFalse((self.delivery / 'summary.md').exists())
        self.assert_process_stopped()

    def test_terminal_before_timer_never_sends_closeout(self):
        self.assertEqual(self.run_fake('early'), 0)
        self.assertFalse(any(cmd['type'] == 'steer' for cmd in self.commands()))
        self.assertFalse((self.closeout.directory / 'request.json').exists())
        self.assertTrue(self.result()['protocol_completed'])
        self.assert_process_stopped()

    def test_settled_with_active_tool_waits_for_real_tool_completion(self):
        self.assertEqual(self.run_fake('settled-tool'), 0)
        self.assertEqual((self.root / 'tool-finished').read_text(), 'tool finished after settled')
        self.assertTrue(self.result()['protocol_completed'])
        self.assertEqual(self.protocol.active_tools, set())
        self.assertEqual(len([cmd for cmd in self.commands() if cmd['type'] == 'steer']), 1)
        self.assert_process_stopped()

    def test_settled_without_tool_completion_expires_and_cannot_claim_completion(self):
        with self.assertRaisesRegex(Problem, 'wall-clock budget'):
            self.run_fake('hung-tool', timeout=1)
        self.assertTrue(self.protocol.settled)
        self.assertEqual(self.protocol.active_tools, {'owned-tool'})
        self.assertFalse(self.result()['protocol_completed'])
        self.assertFalse((self.delivery / 'summary.md').exists())
        self.assert_process_stopped()


if __name__ == '__main__':
    unittest.main()
