"""Seed an already-published v1 task without using the new publication API."""
from pathlib import Path

from codinator import integration
from codinator.files import digest, preserve, snapshot, write_json


def submit_legacy(engine, manifest):
    assert manifest['version'] == 1
    root = Path(manifest['workspace'])
    directory = engine.store.root / 'tasks' / manifest['id']
    integration.publication_preflight(manifest)
    before = snapshot(root, manifest['excludes'])
    directory.mkdir(parents=True)
    write_json(directory / 'manifest.json', manifest)
    write_json(directory / 'intake.json', before)
    (directory / 'handoff.md').write_bytes((root / manifest['handoff']).read_bytes())
    preserve(root, before, engine.store.root / 'blobs')
    engine.store.add(manifest, digest(before))
