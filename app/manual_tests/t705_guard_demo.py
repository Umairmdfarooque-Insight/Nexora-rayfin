"""T-705 manual check: send a payload with 10 row dictionaries through the shared model client.

Run: python -m app.manual_tests.t705_guard_demo
Expected: `PayloadRejected: The model payload was rejected: payload.input.customers is a list of 10 records ...`
and no request to the model service (the guard refuses before anything is sent).
"""

import json

from app.llm.client import client
from app.observability.logs import configure_logging
from app.security.llm_guard import PayloadRejected

configure_logging()  # JSON log lines, as the API writes them
rows = [{"ID": i, "Name": f"Customer {i}"} for i in range(10)]
try:
    client.responses.create(model="unused", instructions="demo", input=json.dumps({"customers": rows}))
except PayloadRejected as error:
    print(f"PayloadRejected: {error}")
    print("No request was sent.")
else:
    raise SystemExit("FAIL: the payload was not rejected")
