"""Read Harbor's contract from the running server, the way a tool would.

Sends the standard introspection query, rebuilds the schema from the
answer, and compares its SDL with the pinned schema.graphql. With
--print, prints the rebuilt SDL instead.
"""

import sys
from pathlib import Path

import httpx
from graphql import (
    build_client_schema, build_schema, get_introspection_query,
    lexicographic_sort_schema, print_schema,
)

URL = "http://127.0.0.1:8005/graphql"
HERE = Path(__file__).resolve().parent


def main() -> int:
    reply = httpx.post(URL, json={"query": get_introspection_query(
        descriptions=True)})
    body = reply.json()
    if body.get("errors"):
        print(reply.status_code, body["errors"][0]["message"])
        return 1
    sdl = print_schema(lexicographic_sort_schema(
        build_client_schema(body["data"]))) + "\n"
    if "--print" in sys.argv:
        print(sdl, end="")
        return 0
    pinned = print_schema(lexicographic_sort_schema(build_schema(
        (HERE / "schema.graphql").read_text()))) + "\n"
    print(f"introspection: {len(reply.content)} bytes, "
          f"{len(body['data']['__schema']['types'])} types")
    if sdl == pinned:
        print("served schema matches the pinned schema.graphql")
        return 0
    print("DRIFT: served schema differs from schema.graphql")
    return 1


if __name__ == "__main__":
    sys.exit(main())
