"""Contract-selected Pi identities; local fake agents never invoke a model."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_engine
from codinator.agents import PiProtocol, reviewer as real_reviewer
from codinator.config import load_manifest, pi_settings
from codinator.files import Problem
from codinator.handoff import template, verify_protocol


STRATA = {'provider': 'strata', 'model': 'qwen3.8-flash-next-iq3_s', 'thinking': 'high'}


class PiIdentityTests(unittest.TestCase):
    def test_legacy_default_does_not_mutate_manifest(self):
        manifest = {'version': 2}
        selected = pi_settings(manifest)
        self.assertEqual(selected, {'provider': 'bonsai', 'model': 'bonsai2-27b', 'thinking': 'xhigh'})
        selected['model'] = 'changed'
        self.assertEqual(manifest, {'version': 2})
        self.assertEqual(pi_settings(manifest)['model'], 'bonsai2-27b')

    def test_invalid_selection_fails_at_manifest_boundary(self):
        invalid = [None, [], {}, STRATA | {'extra': 1}, STRATA | {'provider': ''},
                   STRATA | {'model': '--help'}, STRATA | {'model': 'model:xhigh'},
                   STRATA | {'provider': 'strata\n'}, STRATA | {'model': 'model\x00'},
                   STRATA | {'model': 'model name'}, STRATA | {'thinking': 'auto'},
                   STRATA | {'thinking': []}, STRATA | {'provider': 1}]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'manifest.json'
            for spec in invalid:
                with self.subTest(spec=spec), self.assertRaises(Problem):
                    path.write_text(json.dumps({'version': 2, 'pi': spec}))
                    load_manifest(path)
            with self.assertRaisesRegex(Problem, 'native Pi'):
                path.write_text(json.dumps({'version': 2, 'pi': STRATA, 'implementation': 'external'}))
                load_manifest(path)

    def test_selected_identity_checked_before_prompt_and_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = {'model': {'provider': STRATA['provider'], 'id': STRATA['model']},
                     'thinkingLevel': STRATA['thinking'], 'isStreaming': False, 'pendingMessageCount': 0}
            for override in ({}, {'model': {'provider': 'bonsai', 'id': STRATA['model']}},
                             {'model': {'provider': 'strata', 'id': 'wrong'}},
                             {'thinkingLevel': 'xhigh'}):
                with self.subTest(override=override):
                    events = []
                    runtime = Path(temporary) / 'runtime.json'
                    runtime.unlink(missing_ok=True)
                    protocol = PiProtocol('bounded task', runtime, pi=STRATA)
                    protocol.start(events.append)
                    response = {'type': 'response', 'id': 'state', 'success': True, 'data': state | override}
                    if override:
                        with self.assertRaises(Problem):
                            protocol.event(response, events.append)
                        self.assertEqual([e['type'] for e in events], ['get_state'])
                        self.assertFalse(runtime.exists())
                    else:
                        protocol.event(response, events.append)
                        self.assertEqual([e['type'] for e in events], ['get_state', 'prompt'])
                        recorded = json.loads(runtime.read_text())
                        self.assertEqual({key: recorded[key] for key in STRATA}, STRATA)


class SelectedPiLifecycleTests(unittest.TestCase):
    def setUp(self):
        test_engine.EngineTests.setUp(self)
        (self.workspace / 'handoff.md').write_text(template('handoff').replace(
            'Describe the concrete problem and expected outcome.',
            'Implement product.py with VALUE=42; commit before tests.'))
        self.git('add', 'handoff.md')
        self.git('commit', '-qm', 'Publish selected model fixture contract')
        self.initial = self.git('rev-parse', 'HEAD')
        raw = self.raw | {'id': 'selected', 'handoff_protocol': 1, 'pi': STRATA,
                          'git': {'branch': 'task', 'base_commit': self.initial}}
        self.path.write_text(json.dumps(raw))
        self.selected = load_manifest(self.path)
        self.engine.submit(self.selected)

    def run_selected(self, mode='accept'):
        os.environ['FAKE_MODE'] = mode
        self.engine.run('selected')
        task = self.store.get('selected')
        return task, self.engine.attempt_path(task)

    def assert_pi_identity(self, context):
        argv = json.loads((context / 'pi/launch.json').read_text())['argv']
        for key, flag in (('provider', '--provider'), ('model', '--model'), ('thinking', '--thinking')):
            self.assertEqual(argv[argv.index(flag) + 1], STRATA[key])
        runtime = json.loads((context / 'pi-runtime.json').read_text())
        self.assertEqual({key: runtime[key] for key in STRATA}, STRATA)

    def test_selected_task_accepted_and_codex_identity_unchanged(self):
        task, out = self.run_selected()
        self.assertEqual(task['state'], 'accepted', task['reason'])
        self.assert_pi_identity(out)
        argv = json.loads((out / 'codex/launch.json').read_text())['argv']
        self.assertEqual(argv[argv.index('-m') + 1], 'gpt-6-astra')
        self.assertIn('model_reasoning_effort="xhigh"', argv)

    def test_frozen_selection_rejects_contract_mutation(self):
        task_dir = self.store.root / 'tasks/selected'
        verify_protocol(task_dir, self.selected)
        with self.assertRaises(Problem):
            verify_protocol(task_dir, self.selected | {'pi': STRATA | {'model': 'different'}})
        legacy = json.loads((self.store.root / 'tasks/test/manifest.json').read_text())
        self.assertNotIn('pi', legacy)

    def test_repair_and_review_retry_keep_selected_identity_original_evidence_and_budget(self):
        def unavailable(*args, **kwargs):
            with patch.dict(os.environ, {'FAKE_MODE': 'codex-unavailable'}):
                return real_reviewer(*args, **kwargs)

        with patch('codinator.engine.reviewer', side_effect=unavailable):
            task, source = self.run_selected('malformed-then-repair')
        self.assertEqual(task['state'], 'blocked', task['reason'])
        self.assert_pi_identity(source)
        self.assert_pi_identity(source / 'delivery-repair')
        before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
        # An identity mismatch in either the original implementation or its repair
        # must prevent a review-only retry, even with successful process evidence.
        for context in (source, source / 'delivery-repair'):
            identity = context / 'pi-runtime.json'
            original = identity.read_bytes()
            identity.write_text(json.dumps(pi_settings({})))
            with self.assertRaisesRegex(Problem, 'Unexpected Pi runtime identity'):
                self.engine.resume('selected', review_only=True)
            identity.write_bytes(original)
        self.engine.resume('selected', review_only=True)
        with patch('codinator.engine.worker', side_effect=AssertionError('Review retry replayed Pi')):
            final, out = self.run_selected()
        self.assertEqual((final['state'], final['round'], final['attempt']), ('accepted', 1, 2), final['reason'])
        self.assertEqual(final['deadline'], task['deadline'])
        self.assertFalse((out / 'pi').exists())
        self.assertEqual(before, {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()})
