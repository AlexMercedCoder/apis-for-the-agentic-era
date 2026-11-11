"""Two ways to give a model Harbor's GraphQL API as tools.

    uv run python graphql_tools.py               list both designs
    uv run python graphql_tools.py order_screen  print one curated tool
    uv run python graphql_tools.py run_graphql   print the generic tool

Curated tools come from the trusted documents in operations/: one tool per
operation, named after it, described by its leading comment, with an input
schema built from its variables and the pinned schema.graphql. The host
sends the document ID, never text the model wrote. The generic tool takes
any query the model writes, which the server's depth and cost limits then
have to bound. Tools use the Messages API format of Chapters 1 to 4.
"""

import hashlib
import json
import re
import sys
from pathlib import Path

from graphql import (
    GraphQLEnumType, GraphQLInputObjectType, GraphQLList, GraphQLNonNull,
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
    "description": "Run one GraphQL query against Harbor's storefront API. "
                   "Read the schema first with an introspection query. "
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


def main() -> None:
    tools = curated_tools()
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
    print(f"  {GENERIC['name']:16} depth <= 6, cost <= 1000")


if __name__ == "__main__":
    main()
