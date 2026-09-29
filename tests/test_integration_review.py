"""Independent reviewer regressions: real Git and fake model processes only."""
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

from codinator import integration
from codinator.files import Problem
from codinator.store import Store
import test_integration as fixtures


class IntegrationReviewTests(unittest.TestCase):
    setUp = fixtures.IntegrationTests.setUp

    def test_agent_promisor_config_never_executes_on_host(self):
        original = integration.integrator
        marker = self.base / 'host-command-executed'
        wrapped = []
        original_wrap = self.engine.sandbox.wrap

        def record_wrap(argv, root, *args, **kwargs):
            if Path(root).name == 'repository' and argv[0] == 'git':
                wrapped.append((argv, kwargs))
            return original_wrap(argv, root, *args, **kwargs)

        def malicious(task, attempt, *args, **kwargs):
            original(task, attempt, *args, **kwargs)
            clone = attempt / 'repository'
            head = integration.text(clone, 'rev-parse', 'HEAD')
            payload = clone / 'payload.sh'
            payload.write_text('#!/bin/sh\ntouch ' + str(marker) + '\nexit 1\n')
            payload.chmod(0o755)
            integration.git(clone, 'config', 'remote.origin.url', 'ext::' + str(payload))
            integration.git(clone, 'config', 'remote.origin.promisor', 'true')
            integration.git(clone, 'config', 'protocol.ext.allow', 'always')
            (clone / '.git/objects' / head[:2] / head[2:]).unlink()

        with patch.object(self.engine.sandbox, 'wrap', side_effect=record_wrap), \
             patch('codinator.integration.integrator', side_effect=malicious):
            self.engine.run('test')
        task = self.store.get('test')
        self.assertEqual(task['state'], 'blocked', task['reason'])
        self.assertFalse(marker.exists(), 'Agent-controlled transport executed outside its sandbox')
        self.assertTrue(wrapped, 'Verification bypassed sandbox.wrap')
        for argv, kwargs in wrapped:
            self.assertEqual(kwargs.get('readonly'), [Path(argv[2])])
            self.assertFalse(kwargs.get('writable'))
        fixtures.IntegrationTests.target_unchanged(self)

    def test_concurrent_cancel_survives_integration_failure_publication(self):
        ready = threading.Event()
        release = threading.Event()
        attempting = threading.Event()
        finished = threading.Event()
        errors = []

        def cancel():
            other = None
            try:
                other = Store(self.store.root)
                ready.set()
                if not release.wait(30):
                    raise AssertionError('Cancellation trigger timed out')
                attempting.set()
                other.request_control('test', 'cancel')
            except BaseException as exc:
                errors.append(exc)
            finally:
                if other is not None:
                    other.db.close()
                finished.set()

        thread = threading.Thread(target=cancel, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(5))
        original = self.store.finish
        observations = []

        def racing_finish(task_id, message, **fields):
            if fields.get('state') == 'blocked' and fields.get('phase') == 'integration':
                release.set()
                self.assertTrue(attempting.wait(5))
                observations.append(finished.wait(0.5))
            return original(task_id, message, **fields)

        try:
            with patch('codinator.integration.promote', side_effect=Problem('injected promotion failure')), \
                 patch.object(self.store, 'finish', side_effect=racing_finish):
                self.engine.run('test')
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(observations, [False], 'Control bypassed terminal-state transaction')
        self.assertEqual(self.store.get('test')['state'], 'cancelled')
        fixtures.IntegrationTests.target_unchanged(self)
