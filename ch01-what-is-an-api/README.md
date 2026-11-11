# Chapter 1: What Is an API?

One Harbor Books request, `GET /books/bk-1001`, made by three kinds of
caller: `curl`, a Python client, and a model through a tool call.

## Requirements

- `curl` (the captures were made with 7.81.0)
- [uv](https://docs.astral.sh/uv/) (0.6.14 used); it installs Python 3.13
  and the one dependency, `httpx==0.28.1`, pinned in `pyproject.toml` and
  locked in `uv.lock`

No account and no API key. Nothing leaves `127.0.0.1`.

## Files

| File | What it is |
|---|---|
| `harbor_rest.py` | Harbor's Chapter 1 REST API: `GET /books/{id}` on `127.0.0.1:8000`, standard library only, data from `../harbor-data/books.json` |
| `client.py` | the hand-written Python client (httpx) |
| `agent.py` | the host for the model caller: the `get_book` tool definition, the request in the Claude Messages API format, the tool call executed against Harbor, and the `tool_result` block for the model (with `--live`, sent back in a second request, and the model's answer printed) |
| `run.sh` | starts the server, runs every listing, writes `captures/`, stops the server |
| `captures/` | the exact output the chapter prints |

## Run it

```bash
./run.sh
```

Or by hand, with the server in one terminal:

```bash
uv run python harbor_rest.py
```

and the callers in another:

```bash
curl -v http://127.0.0.1:8000/books/bk-1001
curl -s http://127.0.0.1:8000/books/bk-9999    # 404; hint lists valid IDs
uv run python client.py bk-1001
uv run python agent.py
grep 'bk-1001' captures/server-log.txt             # after ./run.sh
```

Expected output of `client.py`:

```text
200 application/json
Moby-Dick by Herman Melville: $14.99, 7 in stock
```

## About the model caller

By default `agent.py` uses `LocalStandIn`, a deterministic class that is
**not a model**. It finds a `bk-NNNN` ID in the prompt with a regular
expression and returns a response in the documented Messages API shape: a
content block `{"type": "tool_use", "id", "name", "input"}` with
`"stop_reason": "tool_use"`. Its output is labeled `"model": "local-standin"`
and its IDs carry `standin`, so it can't be mistaken for model output. The
request body `agent.py` prints is the real request format, and the tool call
it executes is a real HTTP request to the Harbor server, made only after
`book_id` passes the schema's `pattern` (otherwise the host returns an
`invalid_argument` tool error without calling Harbor). The stand-in stops
after printing the `tool_result` block; it never prints an answer, because
any answer it printed would be invented.

To send the same request to a real model:

```bash
export ANTHROPIC_API_KEY=...        # your own key; never commit it
uv run python agent.py --live
```

`--live` posts to `https://api.anthropic.com/v1/messages` with
`anthropic-version: 2026-10-21` and model `claude-opus-5-5` (override with
`HARBOR_MODEL`), as documented on November 10, 2026. It sends the
`tool_result` back in a second request and prints the text of the model's reply under
`--- model's answer`. It costs tokens. The `--live` path was not run when the
captures were made (no key was available). The
format follows https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview.

## Captures

| File | Command |
|---|---|
| `curl-get-book.err`, `curl-get-book.out` | `curl -sv http://127.0.0.1:8000/books/bk-1001` |
| `curl-get-missing-book.out` | `curl -s -w '%{http_code}\n' .../books/bk-9999` |
| `client.out` | `uv run python client.py bk-1001` |
| `agent.out` | `uv run python agent.py` |
| `server-log.txt` | the server's request log for the run |

Recorded November 10, 2026. `../verify.sh` reruns everything into a
temporary folder and checks the lines the chapter prints.
