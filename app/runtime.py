"""Detects which runtime this process is executing in, once.

`RUNTIME` is the single source of truth: every other module that needs to behave
differently in a notebook job, a User Data Function or locally reads this value
instead of probing the environment itself.
"""

from typing import Literal

RuntimeName = Literal["local", "fabric_job", "fabric_udf"]


def _detect() -> RuntimeName:
    try:
        import fabric.functions  # noqa: F401  (only importable inside the UDF host)
    except ImportError:
        pass
    else:
        return "fabric_udf"

    try:
        import notebookutils  # noqa: F401  (only importable inside a Fabric notebook)
    except ImportError:
        pass
    else:
        return "fabric_job"

    return "local"


RUNTIME: RuntimeName = _detect()
