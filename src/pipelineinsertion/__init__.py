"""Shared support code for the LPP GSEP pipeline insertion evaluator.

Importing this package has no side effects: it opens no network connections,
creates no folders and reads no data. Configuration lives in
`pipelineinsertion.config`; the classification rules that decide the result are
pure functions in `pipelineinsertion.gsep` and `pipelineinsertion.pressure`.
"""

__all__ = ["config", "gsep", "pressure", "systems", "nearest", "schema"]
