"""Real local Git fault injection. No agents, models or credentials."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codinator import git_source as source
from codinator.files import Problem, ScopeViolation


class GitSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='git-source-test-', dir='/tmp')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'repo'
        self.root.mkdir()
        source.git(self.root, 'init', '-q', '-b', 'task')
        (self.root / 'src').mkdir()
        (self.root / 'src/a.py').write_text('BASE\n')
        (self.root / 'src/delete.py').write_text('delete\n')
        (self.root / 'handoff.md').write_text('Frozen contract\n')
        (self.root / '.gitignore').write_text('cache/\nsrc/cache/\n')
        source.git(self.root, 'add', '.')
        source.git(self.root, 'commit', '-qm', 'base')
        self.manifest = {'version': 2, 'workspace': str(self.root), 'allowed_paths': ['src/'],
                         'git': {'branch': 'task', 'base_commit': self.head()}}
        self.before = source.preflight(self.manifest)
        self.attempt = self.base / 'attempt'
        self.attempt.mkdir()

    def head(self):
        return source.text(self.root, 'rev-parse', 'HEAD')

    def checkpoint(self, **kwargs):
        return source.checkpoint(self.manifest, self.before, self.attempt, 'T', 1, **kwargs)

    def change(self):
        (self.root / 'src/a.py').write_text('NEXT\n')

    def test_no_change_reuses_commit_and_writes_only_git_records(self):
        self.assertEqual(self.checkpoint(), self.before)
        self.assertEqual(self.checkpoint(), self.before)
        self.assertEqual(set(p.name for p in self.attempt.iterdir()),
                         {'git-submission-intent.json', 'git-submission-result.json'})
        intent = json.loads((self.attempt / 'git-submission-intent.json').read_text())
        self.assertEqual(intent['before'], self.before)
        self.assertEqual(intent['candidate'], self.before)
        self.assertEqual(json.loads((self.attempt / 'git-submission-result.json').read_text()), self.before)
        self.assertFalse((self.base / 'blobs').exists())
        self.assertEqual(list((self.root / '.git').glob('codinator-index-*.json')), [])

    def test_checkpoint_rename_delete_executable_and_transition_after_head_moved(self):
        (self.root / 'src/a.py').rename(self.root / 'src/renamed.py')
        (self.root / 'src/renamed.py').chmod(0o755)
        (self.root / 'src/delete.py').unlink()
        after = self.checkpoint()
        self.assertEqual(source.changed_paths(self.manifest, self.before, after),
                         ['src/a.py', 'src/delete.py', 'src/renamed.py'])
        self.assertEqual(source.validate_transition(self.manifest, self.before, after),
                         ['src/a.py', 'src/delete.py', 'src/renamed.py'])
        self.assertIn(b'100755', source.git(self.root, 'ls-tree', after['commit'], 'src/renamed.py'))
        self.assertEqual(source.verify(self.manifest, after), after)
        self.assertNotEqual(after['commit'], self.before['commit'])
        self.assertEqual(source.text(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_verify_does_not_change_real_index_or_ref(self):
        index = self.root / '.git/index'
        original = index.read_bytes()
        self.assertEqual(source.verify(self.manifest), self.before)
        self.assertEqual(index.read_bytes(), original)
        self.assertEqual(self.head(), self.before['commit'])

    def test_outside_scope_leaves_worktree_and_head_in_place(self):
        self.change()
        (self.root / 'outside.txt').write_text('untracked evidence')
        with self.assertRaises(ScopeViolation):
            self.checkpoint()
        self.assertEqual((self.root / 'outside.txt').read_text(), 'untracked evidence')
        self.assertEqual(self.head(), self.before['commit'])
        self.assertFalse((self.attempt / 'git-submission-intent.json').exists())

    def test_real_staging_is_not_overwritten(self):
        self.change()
        source.git(self.root, 'add', 'src/a.py')
        index = (self.root / '.git/index').read_bytes()
        with self.assertRaisesRegex(ScopeViolation, 'staged'):
            self.checkpoint()
        self.assertEqual((self.root / '.git/index').read_bytes(), index)

    def test_assume_unchanged_and_skip_worktree_are_rejected(self):
        for flag, undo in (('--assume-unchanged', '--no-assume-unchanged'),
                           ('--skip-worktree', '--no-skip-worktree')):
            with self.subTest(flag=flag):
                source.git(self.root, 'update-index', flag, 'src/a.py')
                with self.assertRaisesRegex(ScopeViolation, 'flags'):
                    self.checkpoint()
                source.git(self.root, 'update-index', undo, 'src/a.py')

    def test_intent_to_add_is_rejected(self):
        (self.root / 'src/new.py').write_text('new')
        source.git(self.root, 'add', '-N', 'src/new.py')
        with self.assertRaises(ScopeViolation):
            self.checkpoint()

    def test_filter_and_textconv_commands_are_rejected_without_execution(self):
        marker = self.base / 'executed'
        for key in ('filter.bad.clean', 'diff.bad.textconv'):
            with self.subTest(key=key):
                source.git(self.root, 'config', key, f'touch {marker}')
                with self.assertRaisesRegex(Problem, 'filters'):
                    source.verify(self.manifest, self.before)
                source.git(self.root, 'config', '--unset', key)
        self.assertFalse(marker.exists())

    def test_content_conversions_are_rejected(self):
        for attribute in ('filter=unknown', 'text', 'eol=crlf', 'ident', 'working-tree-encoding=UTF-16'):
            with self.subTest(attribute=attribute):
                (self.root / 'src/.gitattributes').write_text('*.py ' + attribute + '\n')
                with self.assertRaisesRegex(Problem, 'conversion'):
                    self.checkpoint()

    def test_global_ignore_is_disabled_and_local_excludes_are_rejected(self):
        global_config = self.base / 'global-config'
        ignore = self.base / 'global-ignore'
        ignore.write_text('outside.txt\n')
        global_config.write_text('[core]\n excludesfile = ' + str(ignore) + '\n')
        (self.root / 'outside.txt').write_text('visible')
        with patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': str(global_config)}):
            with self.assertRaises(ScopeViolation):
                self.checkpoint()
        (self.root / 'outside.txt').unlink()
        (self.root / '.git/info/exclude').write_text('outside.txt\n')
        with self.assertRaisesRegex(Problem, 'fixed .gitignore'):
            self.checkpoint()

    def test_ignored_cache_and_internal_gitignore_are_preserved(self):
        for name in ('cache', 'src/cache'):
            cache = self.root / name
            cache.mkdir()
            (cache / '.gitignore').write_text('*\n')
            (cache / 'data').write_bytes(b'private ignored cache\x00')
        before = {str(p): p.read_bytes() for p in self.root.rglob('*') if p.is_file() and 'cache' in p.parts}
        self.change()
        self.checkpoint()
        self.assertEqual({str(p): p.read_bytes() for p in self.root.rglob('*')
                          if p.is_file() and 'cache' in p.parts}, before)
        self.assertNotIn(b'cache', source.git(self.root, 'ls-files', '-z'))

    def test_new_gitignore_cannot_hide_itself_or_outside_files(self):
        directory = self.root / 'outside'
        directory.mkdir()
        (directory / '.gitignore').write_text('*\n')
        (directory / 'hidden').write_text('still forbidden')
        self.assertNotIn(b'outside', source.git(self.root, 'ls-files', '--others', '--exclude-standard', '-z'))
        with self.assertRaisesRegex(ScopeViolation, 'New effective .gitignore'):
            self.checkpoint()

    def test_ignored_descendant_does_not_hide_effective_parent_gitignore(self):
        cache = self.root / 'src/cache'
        cache.mkdir()
        (cache / 'data').write_text('ignored')
        (self.root / 'src/.gitignore').write_text('*\n')
        with self.assertRaisesRegex(ScopeViolation, 'New effective .gitignore'):
            self.checkpoint()

    def test_fixed_gitignore_modification_or_deletion_is_rejected(self):
        ignore = self.root / '.gitignore'
        ignore.write_text('*\n')
        with self.assertRaisesRegex(ScopeViolation, 'Fixed .gitignore'):
            self.checkpoint()
        ignore.unlink()
        with self.assertRaisesRegex(ScopeViolation, 'Fixed .gitignore'):
            self.checkpoint()

    def test_nested_repository_is_rejected(self):
        nested = self.root / 'src/nested'
        nested.mkdir()
        source.git(nested, 'init', '-q')
        with self.assertRaisesRegex(ScopeViolation, 'Nested Git'):
            self.checkpoint()

    def test_changed_head_with_same_tree_is_not_adopted(self):
        other = source.text(self.root, 'commit-tree', self.before['tree'], '-p', self.before['commit'], data=b'other\n')
        source.git(self.root, 'update-ref', 'refs/heads/task', other)
        with self.assertRaisesRegex(ScopeViolation, 'HEAD changed'):
            self.checkpoint()
        with self.assertRaises(Problem):
            source.validate_transition(self.manifest, self.before, source.record(self.root, 'task', other))

    def test_fresh_index_detects_forged_matching_stat_cache(self):
        target = self.root / 'src/a.py'
        original = target.stat()
        target.write_text('EVIL\n')
        os.utime(target, ns=(original.st_atime_ns, original.st_mtime_ns))
        current = target.stat()
        index = self.root / '.git/index'
        data = bytearray(index.read_bytes())
        self.assertEqual(struct.unpack('!I', data[4:8])[0], 2)
        offset = 12
        for _ in range(struct.unpack('!I', data[8:12])[0]):
            end = data.index(0, offset + 62)
            if data[offset + 62:end] == b'src/a.py':
                values = (int(current.st_ctime), current.st_ctime_ns % 1_000_000_000,
                          int(current.st_mtime), current.st_mtime_ns % 1_000_000_000,
                          current.st_dev, current.st_ino, current.st_mode,
                          current.st_uid, current.st_gid, current.st_size)
                data[offset:offset + 40] = struct.pack('!10I', *(v & 0xffffffff for v in values))
            offset += ((end - offset + 1 + 7) // 8) * 8
        data[-20:] = hashlib.sha1(data[:-20]).digest()
        index.write_bytes(data)
        os.utime(index, (current.st_mtime + 60, current.st_mtime + 60))
        self.assertEqual(source.git(self.root, 'diff', '--name-only'), b'')
        with self.assertRaisesRegex(ScopeViolation, 'not clean'):
            source.verify(self.manifest, self.before)
        after = self.checkpoint()
        self.assertEqual(source.git(self.root, 'show', after['commit'] + ':src/a.py'), b'EVIL\n')

    def interrupt_git(self, command, *, after=False):
        original = source.git
        def interrupted(root, *args, **kwargs):
            if args and args[0] == command:
                if after:
                    original(root, *args, **kwargs)
                raise Problem('injected interruption at ' + command)
            return original(root, *args, **kwargs)
        return patch.object(source, 'git', side_effect=interrupted)

    def test_commit_object_interruption_does_not_move_ref(self):
        self.change()
        with self.interrupt_git('commit-tree', after=True), self.assertRaisesRegex(Problem, 'interruption'):
            self.checkpoint()
        self.assertEqual(self.head(), self.before['commit'])
        self.assertFalse((self.attempt / 'git-submission-intent.json').exists())
        self.checkpoint()
        self.assertEqual(source.text(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_intent_before_ref_and_ref_before_result_interruptions_resume_exact_commit(self):
        self.change()
        with self.interrupt_git('update-ref'), self.assertRaisesRegex(Problem, 'interruption'):
            self.checkpoint()
        intent = json.loads((self.attempt / 'git-submission-intent.json').read_text())
        candidate = intent['candidate']
        self.assertEqual(self.head(), self.before['commit'])
        with self.interrupt_git('update-ref', after=True), self.assertRaisesRegex(Problem, 'interruption'):
            self.checkpoint()
        self.assertEqual(self.head(), candidate['commit'])
        self.assertFalse((self.attempt / 'git-submission-result.json').exists())
        with self.interrupt_git('commit-tree'):
            self.assertEqual(self.checkpoint(), candidate)
            self.assertEqual(self.checkpoint(), candidate)
        self.assertEqual(source.text(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_after_index_before_result_interruption_and_conflicting_staging(self):
        self.change()
        original = source._write_once
        def fail_result(path, value):
            if Path(path).name == 'git-submission-result.json':
                raise Problem('interrupted result')
            return original(path, value)
        with patch.object(source, '_write_once', side_effect=fail_result), self.assertRaises(Problem):
            self.checkpoint()
        candidate = json.loads((self.attempt / 'git-submission-intent.json').read_text())['candidate']
        source.verify(self.manifest, candidate)
        (self.root / 'src/staged.py').write_text('unexpected')
        source.git(self.root, 'add', 'src/staged.py')
        index = (self.root / '.git/index').read_bytes()
        with self.assertRaisesRegex(ScopeViolation, 'staged'):
            self.checkpoint()
        self.assertEqual((self.root / '.git/index').read_bytes(), index)

    def test_recovery_rejects_unrelated_head_and_changed_worktree(self):
        self.change()
        with self.interrupt_git('update-ref'), self.assertRaises(Problem):
            self.checkpoint()
        (self.root / 'src/a.py').write_text('third change')
        with self.assertRaisesRegex(ScopeViolation, 'after checkpoint intent'):
            self.checkpoint()
        intent = json.loads((self.attempt / 'git-submission-intent.json').read_text())
        other = source.text(self.root, 'commit-tree', intent['candidate']['tree'],
                            '-p', self.before['commit'], data=b'unrelated candidate\n')
        source.git(self.root, 'update-ref', 'refs/heads/task', other)
        with self.assertRaisesRegex(ScopeViolation, 'neither'):
            self.checkpoint()

    def test_journal_identity_and_result_tampering_are_rejected(self):
        self.change()
        result = self.checkpoint()
        with self.assertRaisesRegex(Problem, 'identity'):
            source.checkpoint(self.manifest, self.before, self.attempt, 'OTHER', 1)
        (self.attempt / 'git-submission-result.json').write_text(json.dumps(result | {'tree': self.before['tree']}))
        with self.assertRaisesRegex(Problem, 'differs from intent'):
            self.checkpoint()

    def test_foreign_index_lock_is_never_removed(self):
        self.change()
        lock = self.root / '.git/index.lock'
        lock.write_bytes(b'foreign operation')
        with self.assertRaisesRegex(Problem, 'another operation'):
            self.checkpoint()
        self.assertEqual(lock.read_bytes(), b'foreign operation')

    def crash_process(self, boundary):
        script = '''
import json, os, sys
from pathlib import Path
from codinator import git_source as source
manifest, before, attempt, boundary = json.loads(sys.argv[1])
if boundary == 'ref':
    original = source.git
    def crash(root, *args, **kwargs):
        value = original(root, *args, **kwargs)
        if args and args[0] == 'update-ref': os._exit(71)
        return value
    source.git = crash
else:
    original = source._write_once
    def crash(path, value):
        original(path, value)
        if Path(path).name == 'git-submission-result.json': os._exit(71)
    source._write_once = crash
source.checkpoint(manifest, before, Path(attempt), 'T', 1)
'''
        result = subprocess.run([sys.executable, '-B', '-c', script,
            json.dumps([self.manifest, self.before, str(self.attempt), boundary])],
            capture_output=True, env=os.environ.copy(), timeout=20)
        self.assertEqual(result.returncode, 71, result.stderr.decode())
        self.assertTrue((self.root / '.git/index.lock').exists())
        candidate = json.loads((self.attempt / 'git-submission-intent.json').read_text())['candidate']
        self.assertEqual(self.checkpoint(), candidate)
        self.assertFalse((self.root / '.git/index.lock').exists())
        self.assertEqual(list((self.root / '.git').glob('codinator-index-*.json')), [])
        self.assertEqual(source.text(self.root, 'rev-list', '--count', 'HEAD'), '2')

    def test_real_crash_after_ref_recovers_owned_index_lock_without_new_commit(self):
        self.change()
        self.crash_process('ref')

    def test_real_crash_after_result_recovers_owned_index_lock(self):
        self.change()
        self.crash_process('result')


if __name__ == '__main__':
    unittest.main()
