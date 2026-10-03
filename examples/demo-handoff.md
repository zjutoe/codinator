# Demo: checked integer addition

Implement `add.py` with `add(a, b) -> int` and tests in `tests/test_add.py`.
Both inputs must satisfy `type(value) is int`; reject booleans and other types
with `TypeError`. Return the exact sum, including zero, negative and arbitrarily
large integers. Use only the Python standard library and no I/O or global state.

1. Inspect this contract and the existing workspace without changing frozen files.
2. Implement the function and independent `unittest` cases for valid and invalid inputs.
3. Commit the allowed implementation on the declared task branch, then run
   `python3 -B -m unittest discover -s tests -v` against that exact SHA.
4. Submit the summary, status and attempt-bound Git/check evidence through the assigned delivery tool.

Acceptance: exact sums, strict type rejection, no unintended side effects, meaningful
boundary tests, required check passes, and changes stay inside the manifest allowlist.
Only task-local commits are authorized. Do not edit this contract, switch branches,
change Git configuration/ignore rules, push, merge, or claim acceptance yourself.
