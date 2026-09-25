import json
from pathlib import Path
import tempfile
import unittest

from codidator.agents import PiProtocol, validate_verdict
from codidator.files import Problem


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.events = []
        self.protocol = PiProtocol('implement', Path(self.temp.name) / 'runtime.json')
        self.protocol.start(self.events.append)

    def start(self, **overrides):
        state = dict(model={'provider': 'bonsai', 'id': 'bonsai2-27b'}, thinkingLevel='xhigh',
                     isStreaming=False, pendingMessageCount=0, sessionId='abc') | overrides
        self.protocol.event({'type': 'response', 'id': 'state', 'success': True, 'data': state}, self.events.append)
        self.protocol.event({'type': 'response', 'id': 'prompt', 'success': True}, self.events.append)

    def message(self, reason):
        self.protocol.event({'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': reason}}, self.events.append)

    def test_ack_and_agent_end_are_not_completion(self):
        self.start()
        self.message('stop')
        self.assertFalse(self.protocol.event({'type': 'agent_end'}, self.events.append))
        with self.assertRaises(Problem):
            self.protocol.finish()

    def test_retry_then_settled_success(self):
        self.start()
        self.message('error')
        self.protocol.event({'type': 'agent_end', 'willRetry': True}, self.events.append)
        self.message('stop')
        self.assertTrue(self.protocol.event({'type': 'agent_settled'}, self.events.append))
        self.protocol.finish()
        self.assertEqual([e['type'] for e in self.events], ['get_state', 'prompt'])

    def test_failure_or_truncation_never_accepted(self):
        for reason in ('length', 'error', 'aborted', 'toolUse', None):
            with self.subTest(reason=reason):
                self.start_if_needed()
                self.message(reason)
                self.protocol.event({'type': 'agent_settled'}, self.events.append)
                with self.assertRaises(Problem):
                    self.protocol.finish()

    def start_if_needed(self):
        if not self.protocol.prompt_sent:
            self.start()

    def test_model_mismatch_no_prompt(self):
        with self.assertRaises(Problem):
            self.start(model={'provider': 'ollama', 'id': 'qwen'})
        self.assertEqual(len(self.events), 1)

    def test_duplicate_state_does_not_resend(self):
        self.start()
        with self.assertRaises(Problem):
            self.start()
        self.assertEqual(sum(e['type'] == 'prompt' for e in self.events), 1)

    def test_interactive_confirm_is_not_auto_approved(self):
        with self.assertRaises(Problem):
            self.protocol.event({'type': 'extension_ui_request', 'method': 'confirm'}, self.events.append)

    def test_malformed_nested_objects_fail_explicitly(self):
        for data in (None, [], {'model': []}, {'model': None}):
            with self.subTest(data=data), self.assertRaises(Problem):
                self.protocol.event({'type': 'response', 'id': 'state', 'success': True, 'data': data}, self.events.append)
        for message in (None, [], 'text'):
            with self.subTest(message=message), self.assertRaises(Problem):
                self.protocol.event({'type': 'message_end', 'message': message}, self.events.append)

    def test_verdict_strict_binding(self):
        value = {'task_id': 'T', 'submission_digest': 'abc', 'verdict': 'accepted', 'summary': 'Reviewed', 'issues': []}
        self.assertEqual(validate_verdict(value, 'T', 'abc'), value)
        for patch in ({'submission_digest': 'old'}, {'verdict': 'done'}, {'issues': {}}, {'extra': 1}, {'summary': ''}):
            with self.subTest(patch=patch), self.assertRaises(Problem):
                validate_verdict(value | patch, 'T', 'abc')
        with self.assertRaises(Problem):
            validate_verdict(value | {'verdict': 'needs_changes'}, 'T', 'abc')
