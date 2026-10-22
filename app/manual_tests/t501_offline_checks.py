"""T-501 offline checks: sandbox deployment client and `cli deploy model`.

Run: python -m app.manual_tests.t501_offline_checks
Expected: every line starts with PASS and the last line is "All checks passed."
Fabric is replaced by a fake HTTP layer, so no sign-in or network is needed.
The live steps (a real workspace) are in MANUAL_TESTING.md.
"""

import base64
import contextlib
import io
import os
import tempfile
from pathlib import Path

import requests

from app.cli import main as cli_main
from app.compiler.critical_terms import ConfigError
from app.connectors.fabric_api import FabricRateLimited, FabricTimeout
from app.deploy.fabric_items import (
    FabricItemsClient,
    FabricOperationFailed,
    InvalidDefinition,
    NameConflict,
    TargetNotAllowed,
    tmdl_parts,
)

SANDBOX = "11111111-1111-4111-8111-111111111111"
OTHER = "99999999-9999-4999-8999-999999999999"
BASE = "https://api.fabric.microsoft.com/v1"
OPERATION = f"{BASE}/operations/op-1"


def check(name: str, condition: bool) -> None:
    print(("PASS " if condition else "FAIL ") + name)
    if not condition:
        raise SystemExit(1)


def raises(exc, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc as e:
        return e
    return None


class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._body


class FakeFabric:
    """Records calls and answers like Fabric: items list, create, update, operations."""

    def __init__(self):
        self.items: list[dict] = []
        self.calls: list[tuple] = []
        self.operation_answers: list[Resp] = []
        self.create_answer: Resp | None = None
        self.update_answer: Resp | None = None
        self.throttle = 0
        self.clock = 0.0

    def __call__(self, method, url, headers=None, json=None, params=None, timeout=None):
        self.calls.append((method, url, json))
        assert headers["Authorization"] == "Bearer token"
        if self.throttle > 0 and method == "POST":
            self.throttle -= 1
            return Resp(429, {}, {"Retry-After": "3"})
        if method == "GET" and url.endswith("/items"):
            return Resp(200, {"value": list(self.items)})
        if method == "GET" and url == OPERATION:
            return self.operation_answers.pop(0) if len(self.operation_answers) > 1 else self.operation_answers[0]
        if method == "POST" and url.endswith("/semanticModels"):
            if self.create_answer is not None:
                answer = self.create_answer
                if answer.status_code < 300:
                    self.items.append({"id": "item-1", "displayName": json["displayName"], "type": "SemanticModel"})
                return answer
            item = {"id": "item-1", "displayName": json["displayName"], "type": "SemanticModel"}
            self.items.append(item)
            return Resp(201, item)
        if method == "POST" and url.endswith("/updateDefinition"):
            return self.update_answer or Resp(200, {})
        raise AssertionError(f"unexpected call {method} {url}")

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


def make_client(fake: FakeFabric, **kw):
    sleeps: list[float] = []

    def sleep(seconds):
        sleeps.append(seconds)
        fake.clock += seconds

    client = FabricItemsClient(
        token_provider=lambda: "token", http=fake, sleep=sleep, clock=lambda: fake.clock, **kw
    )
    return client, sleeps


def write_model(folder: Path, measure: str = "Product Count") -> Path:
    (folder / "definition" / "tables").mkdir(parents=True, exist_ok=True)
    (folder / "definition.pbism").write_bytes(('{"version": "4.0"}\n').encode("utf-8"))
    (folder / "definition" / "database.tmdl").write_bytes(("database Nexora\n").encode("utf-8"))
    (folder / "definition" / "model.tmdl").write_bytes(("model Model\n").encode("utf-8"))
    (folder / "definition" / "tables" / "Product.tmdl").write_text(
        f"table Product\n\tmeasure '{measure}' = COUNTROWS('Product')\n", encoding="utf-8"
    )
    return folder


def run_cli(*argv):
    # main() reconfigures its streams, so they must be real text wrappers
    out = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    err = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli_main(list(argv))
        out.flush()
        err.flush()
    return code, out.buffer.getvalue().decode("utf-8"), err.buffer.getvalue().decode("utf-8")


def main() -> None:
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX
    os.environ.pop("FABRIC_SANDBOX_WORKSPACE_ID", None)
    tmp = Path(tempfile.mkdtemp())
    model = write_model(tmp / "model")

    # Parts
    parts = tmdl_parts(model)
    paths = [p["path"] for p in parts]
    check("parts: definition.pbism first, then sorted definition files",
          paths == ["definition.pbism", "definition/database.tmdl", "definition/model.tmdl", "definition/tables/Product.tmdl"])
    check("parts: base64 inline payloads",
          all(p["payloadType"] == "InlineBase64" for p in parts)
          and base64.b64decode(parts[0]["payload"]) == b'{"version": "4.0"}\n')
    check("parts: missing definition.pbism is refused", raises(InvalidDefinition, tmdl_parts, tmp) is not None)

    # Create (synchronous 201)
    fake = FakeFabric()
    client, _ = make_client(fake)
    result = client.upsert_semantic_model(SANDBOX, "Nexora_Smoke", model)
    check("create: operation created with item id", result == {"item_id": "item-1", "operation": "created"})
    method, url, body = fake.posts()[0]
    check("create: POST /semanticModels with displayName and definition parts",
          url == f"{BASE}/workspaces/{SANDBOX}/semanticModels" and body["displayName"] == "Nexora_Smoke"
          and [p["path"] for p in body["definition"]["parts"]] == paths)

    # Update, not duplicate
    write_model(model, "Products")
    result = client.upsert_semantic_model(SANDBOX, "nexora_smoke", model)
    check("update: same name (case-insensitive) updates the existing item", result == {"item_id": "item-1", "operation": "updated"})
    check("update: POST updateDefinition, no second create",
          fake.posts()[-1][1].endswith("/semanticModels/item-1/updateDefinition") and len(fake.items) == 1
          and sum(1 for c in fake.posts() if c[1].endswith("/semanticModels")) == 1)
    again = client.upsert_semantic_model(SANDBOX, "Nexora_Smoke", model)
    check("idempotent: deploying twice leaves one item", again["operation"] == "updated" and len(fake.items) == 1)

    # Long-running create with Retry-After
    fake = FakeFabric()
    fake.create_answer = Resp(202, {}, {"Location": OPERATION})
    fake.operation_answers = [
        Resp(200, {"status": "Running"}, {"Retry-After": "7"}),
        Resp(200, {"status": "Running"}, {"Retry-After": "5"}),
        Resp(200, {"status": "Succeeded"}),
    ]
    client, sleeps = make_client(fake)
    result = client.upsert_semantic_model(SANDBOX, "Nexora_Smoke", model)
    check("LRO: polls until Succeeded, honouring Retry-After", sleeps == [7, 5])
    check("LRO: item id found by name after a 202 create", result == {"item_id": "item-1", "operation": "created"})

    # Long-running update
    fake.update_answer = Resp(202, {}, {"Location": OPERATION})
    fake.operation_answers = [Resp(200, {"status": "Succeeded"})]
    result = client.upsert_semantic_model(SANDBOX, "Nexora_Smoke", model)
    check("LRO: update polls too", result["operation"] == "updated")

    # Failed operation
    fake = FakeFabric()
    fake.create_answer = Resp(202, {}, {"Location": OPERATION})
    fake.operation_answers = [Resp(200, {"status": "Failed", "error": {"errorCode": "Bad_Tmdl", "message": "Syntax error at line 2"}}, {"RequestId": "req-9"})]
    client, _ = make_client(fake)
    err = raises(FabricOperationFailed, client.upsert_semantic_model, SANDBOX, "Nexora_Smoke", model)
    check("failed operation: code, message and request id", err is not None and "Bad_Tmdl" in str(err) and "Syntax error at line 2" in str(err) and err.request_id == "req-9")
    check("failed operation: Fabric error kept in details", err.details == {"errorCode": "Bad_Tmdl", "message": "Syntax error at line 2"})

    # Rejected definition (400) surfaced unchanged
    fake = FakeFabric()
    fake.create_answer = Resp(400, {"errorCode": "InvalidDefinition", "message": "Bad part", "moreDetails": [{"x": 1}]}, {"RequestId": "req-4"})
    client, _ = make_client(fake)
    err = raises(FabricOperationFailed, client.upsert_semantic_model, SANDBOX, "Nexora_Smoke", model)
    check("rejected definition: Fabric's body in details", err is not None and err.details["moreDetails"] == [{"x": 1}] and err.request_id == "req-4")

    # 429
    fake = FakeFabric()
    fake.throttle = 2
    client, sleeps = make_client(fake)
    result = client.upsert_semantic_model(SANDBOX, "Nexora_Smoke", model)
    check("429: waits Retry-After and retries", sleeps == [3, 3] and result["operation"] == "created")
    fake = FakeFabric()
    fake.throttle = 99
    client, sleeps = make_client(fake)
    err = raises(FabricRateLimited, client.upsert_semantic_model, SANDBOX, "Nexora_Smoke", model)
    check("429: gives up after 5 retries", err is not None and len(sleeps) == 5)

    # Name used by another type
    fake = FakeFabric()
    fake.items = [{"id": "x", "displayName": "Nexora_Smoke", "type": "Report"}]
    client, _ = make_client(fake)
    err = raises(NameConflict, client.upsert_semantic_model, SANDBOX, "Nexora_Smoke", model)
    check("name conflict: 'name used by a Report item'", err is not None and "used by a Report item" in str(err) and not fake.posts())

    # Timeout
    fake = FakeFabric()
    fake.create_answer = Resp(202, {}, {"Location": OPERATION})
    fake.operation_answers = [Resp(200, {"status": "Running"}, {"Retry-After": "30"})]
    client, _ = make_client(fake)
    err = raises(FabricTimeout, client.wait_for_operation, OPERATION, 100)
    check("timeout: UPSTREAM_TIMEOUT, state unknown", err is not None and "state is unknown" in str(err))

    # Guard
    fake = FakeFabric()
    client, _ = make_client(fake)
    check("guard: non-sandbox workspace refused", raises(TargetNotAllowed, client.upsert_semantic_model, OTHER, "X", model) is not None)
    check("guard: nothing was called", fake.calls == [])
    check("guard: get_item_by_name refused too", raises(TargetNotAllowed, client.get_item_by_name, OTHER, "X", "SemanticModel") is not None)
    promo, _ = make_client(fake, allow_any_workspace=True)
    check("guard: the promotion path may target another workspace", promo.upsert_semantic_model(OTHER, "X", model)["operation"] == "created")
    del os.environ["SANDBOX_WORKSPACE_ID"]
    check("guard: no sandbox configured is a config error", raises(ConfigError, make_client(FakeFabric())[0].upsert_semantic_model, SANDBOX, "X", model) is not None)
    os.environ["FABRIC_SANDBOX_WORKSPACE_ID"] = SANDBOX
    check("setting: falls back to FABRIC_SANDBOX_WORKSPACE_ID", make_client(FakeFabric())[0].upsert_semantic_model(SANDBOX, "X", model)["operation"] == "created")
    del os.environ["FABRIC_SANDBOX_WORKSPACE_ID"]
    os.environ["SANDBOX_WORKSPACE_ID"] = SANDBOX

    # CLI (the HTTP layer is swapped for the fake)
    import app.deploy.fabric_items as module

    fake = FakeFabric()
    original = (module.requests.request, module.get_token)
    module.requests.request = fake
    module.get_token = lambda scope: "token"
    try:
        code, out, err_text = run_cli("deploy", "model", str(model), "--name", "Nexora_Smoke")
        check("cli: created, prints item id and operation", code == 0 and "item_id: item-1" in out and "operation: created" in out)
        code, out, _ = run_cli("deploy", "model", str(model), "--name", "Nexora_Smoke")
        check("cli: second run prints updated", code == 0 and "operation: updated" in out)
        code, out, err_text = run_cli("deploy", "model", str(model), "--name", "Nexora_Smoke", "--workspace-id", OTHER)
        check("cli: non-sandbox workspace refused with TARGET_NOT_ALLOWED", code == 1 and "TARGET_NOT_ALLOWED" in err_text and len(fake.items) == 1)
        code, _, err_text = run_cli("deploy", "model", str(tmp / "missing"), "--name", "X")
        check("cli: missing folder is exit 2", code == 2 and "definition.pbism not found" in err_text)
    finally:
        module.requests.request, module.get_token = original

    print("All checks passed.")


if __name__ == "__main__":
    main()
