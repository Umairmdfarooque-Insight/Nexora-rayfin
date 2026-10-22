"""Who is calling, in each Fabric host (T-105, FR-018 attributable actions).

See fabric-hosting.md's platform-limits table ("What a UDF's invocation context
actually exposes", "Whether a UDF can obtain a user-delegated Fabric token"):
a UDF's `UserDataFunctionContext.executing_user` has no UPN, only `Oid`
(immutable, the only field safe for authorization) and `PreferredUsername`
(mutable, display only). A job's identity comes from `app.auth.current_identity`
(T-002), which is `notebookutils.runtime.context` and has the same `upn`-is-
really-a-username caveat.
"""

from typing import Any

from app.auth import current_identity


def principal_from_udf_context(ctx: Any) -> dict | None:
    """`{oid, upn, name}` from a `fabric.functions.UserDataFunctionContext`, or
    `None` when there is no signed-in caller (anonymous, or a context shape this
    hasn't seen — never raises, since an unrecognized caller must be refused,
    not treated as the run's owner or as "system")."""
    if ctx is None:
        return None
    try:
        user = ctx.executing_user
    except AttributeError:
        return None
    # `executing_user` is documented as a dict-like object with PascalCase keys;
    # support both attribute and mapping access defensively (not yet confirmed
    # against a live tenant).
    oid = _field(user, "Oid")
    preferred_username = _field(user, "PreferredUsername")
    if not oid:
        return None
    return {"oid": oid, "upn": preferred_username or oid, "name": preferred_username or oid}


def _field(obj: Any, name: str) -> str | None:
    if obj is None:
        return None
    if isinstance(obj, dict):
        value = obj.get(name)
    else:
        value = getattr(obj, name, None)
    return str(value) if value else None


def principal_from_job() -> dict | None:
    """`{oid, upn, name}` of the user a job runs as, or `None` outside a job or
    when the runtime context doesn't carry it."""
    return current_identity()
