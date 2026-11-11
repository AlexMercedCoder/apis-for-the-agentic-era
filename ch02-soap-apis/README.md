# Chapter 2: SOAP and the Contract-First Web Service

Harbor Books' order service as a 2004 integration partner would have
received it: a WSDL file written first, a SOAP 1.1 service that follows it,
a client that zeep generates from it, and the same contract turned into an
agent tool.

## Requirements

- `curl` (the captures were made with 7.81.0)
- [uv](https://docs.astral.sh/uv/) (0.6.14 used); it installs Python 3.13
  and `zeep==4.3.3`, pinned in `pyproject.toml` and locked in `uv.lock`
  (with lxml 6.1.3)

No account and no API key. Nothing leaves `127.0.0.1`. Run it from inside
the repository: the server reads `../harbor-data/`.

The partner login, `partner-demo` / `not-a-real-secret`, is a placeholder
that protects nothing. Override it with `HARBOR_PARTNER_USER` and
`HARBOR_PARTNER_PASSWORD` if you like. The UsernameToken sends the password
as plain text, which is acceptable only over TLS.

## Files

| File | What it is |
|---|---|
| `harbor.wsdl` | the contract: WSDL 1.1, SOAP 1.1 over HTTP, document/literal wrapped; `GetOrderStatus` (needs a WS-Security UsernameToken) and `GetBook` (public) |
| `harbor_soap.py` | the service, standard library only, on `127.0.0.1:8002`: `GET /harbor/soap?wsdl` and `POST /harbor/soap`; data from `../harbor-data/` |
| `requests/*.xml` | hand-written envelopes for `curl` |
| `client.py` | the client Zeep generates from the pinned `harbor.wsdl` (the WSDL's `soap:address` names the server); `--history` prints the envelopes it sent and received, **including the UsernameToken password**, so never use it with real credentials |
| `soap_tool.py` | `get_order_status` as an agent tool, built from the WSDL's documentation, pattern, and status values; the host loads the same pinned WSDL, calls SOAP, and returns JSON |
| `run.sh` | starts the server, runs every listing, writes `captures/`, stops the server |
| `captures/` | the exact output the chapter prints |

## Run it

```bash
./run.sh
```

Or by hand, with the server in one terminal:

```bash
uv run python harbor_soap.py
```

and the callers in another:

```bash
curl -v http://127.0.0.1:8002/harbor/soap \
  -H 'Content-Type: text/xml; charset=utf-8' \
  -H 'SOAPAction: "http://harbor.example/orders/GetOrderStatus"' \
  --data-binary @requests/get-order-status.xml
uv run python -m zeep harbor.wsdl
uv run python client.py ord-5002
uv run python client.py ord-9999      # soap:Client fault, NotFound
uv run python client.py 5002          # Zeep doesn't check xs:pattern;
                                      # the server's InvalidArgument
                                      # fault does
uv run python soap_tool.py
grep POST captures/server-log.txt | head -3   # after ./run.sh
```

Expected output of `client.py ord-5002`:

```text
ord-5002: shipped since 2026-11-03 09:41:52+00:00, 48.97 USD
```

## Faults the service returns

| `faultcode` | When |
|---|---|
| `soap:Client` | unknown order or book (`NotFound` detail with a hint), an ID that fails the WSDL's pattern (`InvalidArgument` detail with the field and pattern), malformed XML |
| `soap:MustUnderstand` | a header block marked `mustUnderstand="1"` other than `wsse:Security` |
| `soap:VersionMismatch` | a SOAP 1.2 envelope |
| `wsse:FailedAuthentication` | `GetOrderStatus` without the partner's UsernameToken |
| `wsse:UnsupportedSecurityToken` | a UsernameToken whose password isn't `PasswordText` |

Every fault is returned with HTTP 500, as SOAP 1.1 § 6.2 requires. Faults
about the body carry a `detail`, and header faults don't, as § 4.4 requires.

## Captures

| File | Command |
|---|---|
| `curl-get-order-status.err`, `.out` | `curl -sv` with `requests/get-order-status.xml` |
| `curl-order-not-found.err`, `.out` | `curl -sv` with `requests/get-order-status-missing.xml` |
| `curl-get-book.out` | `requests/get-book.xml` |
| `curl-must-understand.out` | `requests/must-understand.xml` |
| `curl-no-token.out` | the status request with the UsernameToken removed |
| `zeep-inspect.out` | `uv run python -m zeep harbor.wsdl` |
| `client.out`, `client-not-found.out`, `client-bad-id.out` | `client.py ord-5002 --history`, `ord-9999`, `5002` |
| `soap-tool.out` | `uv run python soap_tool.py` |
| `server-log.txt` | the server's request log for the run |

Recorded November 10, 2026. `../verify.sh` reruns everything into a
temporary folder and checks the lines the chapter prints.
