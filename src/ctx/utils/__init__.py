"""ctx.utils — low-level primitives.

  _safe_name   - path-derived name validation (Windows device-name safe,
                 drive-relative safe, reserved-char safe)
  _fs_utils    - atomic file write, atomic JSON write, sha256
  _file_lock   - cross-platform file lock for concurrent ingest/enrich

Populated in phase R1 (moves from src/_safe_name.py, src/_fs_utils.py,
src/_file_lock.py).
"""
