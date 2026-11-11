"""Two ways to give a model Harbor's GraphQL API as tools.

    uv run python graphql_tools.py               list both designs
    uv run python graphql_tools.py order_screen  print one curated tool
    uv run python graphql_tools.py run_graphql   print the generic tool
    uv run python graphql_tools.py --check DOC   run the host's read-only check
    uv run python graphql_tools.py --sizes       bytes a model reads per design

Curated tools come from the trusted documents in operations/: one tool per
operation, named after it, described by its leading comment, with an input
schema built from its variables and the pinned schema.graphql. The host
sends the document ID, never text the model wrote. The generic tool takes
any query the model writes; the host refuses any document that holds an
operation other than a query, and the server's depth and cost limits bound
the rest. Tools use the Messages API format of Chapters 1 to 4.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

from graphql import (
    GraphQLEnumType, GraphQLInputObjectType, GraphQLList, GraphQLNonNull,
    GraphQLSyntaxError,
    OperationDefinitionNode, build_schema, parse, type_from_ast,
)

HERE = Path(__file__).resolve().parent
SCHEMA = build_schema((HERE / "schema.graphql").read_text())
SCALARS = {"ID": "string", "String": "string", "Int": "integer",
           "Float": "number", "Boolean": "boolean"}


def json_schema(gql_type) -> dict:
    if isinstance(gql_type, GraphQLNonNull):
        return json_schema(gql_type.of_type)
    if isinstance(gql_type, GraphQLList):
        return {"type": "array", "items": json_schema(gql_type.of_type)}
    if isinstance(gql_type, GraphQLEnumType):
        return {"type": "string", "enum": list(gql_type.values)}
    if isinstance(gql_type, GraphQLInputObjectType):
        props, required = {}, []
        for name, field in gql_type.fields.items():
            props[name] = json_schema(field.type)
            if field.description:
                props[name]["description"] = field.description
            if isinstance(field.type, GraphQLNonNull):
                required.append(name)
        return {"type": "object", "properties": props, "required": required}
    return {"type": SCALARS[gql_type.name]}


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def curated_tools() -> list[dict]:
    tools = []
    for path in sorted((HERE / "operations").glob("*.graphql")):
        text = path.read_text()
        op = next(d for d in parse(text).definitions
                  if isinstance(d, OperationDefinitionNode))
        props, required = {}, []
        for var in op.variable_definitions:
            name = var.variable.name.value
            props[name] = json_schema(type_from_ast(SCHEMA, var.type))
            if isinstance(type_from_ast(SCHEMA, var.type), GraphQLNonNull):
                required.append(name)
        comment = text.splitlines()[0].lstrip("# ").strip()
        tools.append({
            "name": snake(op.name.value),
            "description": comment,
            "input_schema": {"type": "object", "properties": props,
                             "required": required},
            "document_id": "sha256:" + hashlib.sha256(
                text.encode()).hexdigest(),
            "needs_approval": op.operation.value != "query",
        })
    return tools


GENERIC = {
    "name": "run_graphql",
    "description": "Run one read-only GraphQL query against Harbor's "
                   "storefront API. Read the schema first with an "
                   "introspection query. Mutations are refused. "
                   "Queries deeper than 6 levels or costlier than 1000 "
                   "are refused.",
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "A GraphQL query document"},
            "variables": {"type": "object"},
        },
        "required": ["query"],
    },
}


def check_read_only(document: str) -> str | None:
    """The host's guard for run_graphql: None if every operation is a
    query, otherwise the reason it refuses to send the document."""
    try:
        ops = [d for d in parse(document).definitions
               if isinstance(d, OperationDefinitionNode)]
    except GraphQLSyntaxError as err:
        return f"not sent: {err.message}"
    for op in ops:
        if op.operation.value != "query":
            return (f"not sent: run_graphql runs queries only, and this "
                    f"document holds a {op.operation.value}")
    return None


def main() -> None:
    if sys.argv[1:2] == ["--check"]:
        print(check_read_only(sys.argv[2]) or "sent: queries only")
        return
    tools = curated_tools()
    if sys.argv[1:2] == ["--sizes"]:
        shown = [{k: v for k, v in t.items()
                  if k in ("name", "description", "input_schema")}
                 for t in tools]
        sdl = len((HERE / "schema.graphql").read_bytes())
        print(f"schema.graphql (SDL)        {sdl:>6,} bytes")
        print(f"three curated tools (JSON)  {len(json.dumps(shown)):>6,} bytes")
        print(f"run_graphql tool (JSON)     {len(json.dumps(GENERIC)):>6,} bytes")
        return
    if len(sys.argv) > 1:
        wanted = sys.argv[1]
        tool = GENERIC if wanted == "run_graphql" else next(
            t for t in tools if t["name"] == wanted)
        shown = {k: v for k, v in tool.items()
                 if k in ("name", "description", "input_schema")}
        print(json.dumps(shown, indent=2))
        return
    print("curated: one tool per trusted document")
    for t in tools:
        flag = "ask first" if t["needs_approval"] else ""
        print(f"  {t['name']:16} {t['document_id'][:19]}...  {flag}".rstrip())
    print("generic: one tool for any query")
    print(f"  {GENERIC['name']:16} queries only, depth <= 6, cost <= 1000")


if __name__ == "__main__":
    main()
