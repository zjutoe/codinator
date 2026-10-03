"""Seed historical v1 records; never run legacy task or Git operations."""
from pathlib import Path
from codinator.files import write_json

def submit_legacy(engine, manifest):
    assert manifest['version'] == 1
    directory = engine.store.root / 'tasks' / manifest['id']
    directory.mkdir(parents=True)
    write_json(directory / 'manifest.json', manifest)
    write_json(directory / 'intake.json', {'historical': True, 'files': {}})
    (directory / 'handoff.md').write_bytes((Path(manifest['workspace']) / manifest['handoff']).read_bytes())
    engine.store.add(manifest, 'historical-v1-snapshot')
