# Demo: checked integer addition

Implement `add.py` with `add(a, b) -> int` and tests in `tests/test_add.py`.
Both inputs must satisfy `type(value) is int`; reject booleans and other types
with `TypeError`. Return the exact sum, including zero, negative and arbitrarily
large integers. Use only the Python standard library and no I/O or global state.

1. Inspect this contract and the existing workspace without changing frozen files.
2. Implement the function and independent `unittest` cases for valid and invalid inputs.
3. Run `python3 -m unittest discover -s tests -v` with bytecode writing disabled.
4. Write the delivery summary and completion JSON at the paths assigned by Codidator.

Acceptance: exact sums, strict type rejection, no unintended side effects, meaningful
boundary tests, required check passes, and changes stay inside the manifest allowlist.
Do not edit this contract, commit, stage, push, merge, or claim acceptance yourself.
