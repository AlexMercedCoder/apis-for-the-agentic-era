"""A stand-in for Rosa's phone: it shows each approval request and answers.

CIBA sends the request to the person's own device, outside the agent's
channel, so the host can't answer it for her. This program plays the
device: it polls the test issuer for Rosa's pending requests, prints the
binding message she would read, and approves or denies according to
state/phone-mode (approve unless the file says deny). A real phone shows
the message and waits for her.

    uv run python phone.py [person]
"""

import json
import sys
import time

import httpx

import common
import issuer

person = sys.argv[1] if len(sys.argv) > 1 else "rosa"
mode_file = common.STATE / "phone-mode"
while True:
    try:
        waiting = httpx.get(f"{issuer.ISSUER}/phone/pending",
                            params={"person": person}).json()
    except httpx.HTTPError:
        time.sleep(0.5)
        continue
    for req in waiting:
        mode = mode_file.read_text().strip() if mode_file.exists() \
            else "approve"
        decision = "deny" if mode == "deny" else "approve"
        print(f"{person}'s phone shows: {req['binding_message']}  "
              f"[{decision}] {json.dumps(req['details'])}", flush=True)
        time.sleep(0.5)
        httpx.post(f"{issuer.ISSUER}/phone/decide",
                   data={"auth_req_id": req["auth_req_id"],
                         "decision": decision})
    time.sleep(0.3)
