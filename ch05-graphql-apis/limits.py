"""A query cost limit for Harbor's GraphQL server, as a validation rule.

Every field costs 1. A list field multiplies the cost of what is selected
under it by the number of items it can return: its `first` argument when the
query gives one as a literal, the argument's default otherwise, and
ASSUMED_LIST_SIZE for lists with no `first` argument. A `first` passed in
a variable counts as MAX_FIRST, the most any resolver returns. Introspection fields
(names starting with `__`) cost nothing, so tools can read the schema.
"""

from graphql import (
    FieldNode, FragmentSpreadNode, GraphQLError, GraphQLList, GraphQLObjectType,
    InlineFragmentNode, IntValueNode, OperationDefinitionNode, ValidationRule,
    get_named_type, get_nullable_type,
)

MAX_COST = 1000
ASSUMED_LIST_SIZE = 10
MAX_FIRST = 50


def make_cost_rule(max_cost: int = MAX_COST) -> type[ValidationRule]:
    class CostLimit(ValidationRule):
        def enter_operation_definition(self, node: OperationDefinitionNode,
                                       *_args):
            schema = self.context.schema
            root = schema.get_root_type(node.operation)
            cost = self.selection_cost(node.selection_set, root)
            if cost > max_cost:
                self.report_error(GraphQLError(
                    f"Query cost {cost} exceeds the limit of {max_cost}.",
                    node,
                    extensions={"code": "COST_LIMIT", "cost": cost,
                                "limit": max_cost},
                ))

        def selection_cost(self, selection_set, parent) -> int:
            if selection_set is None or not isinstance(parent,
                                                       GraphQLObjectType):
                return 0
            total = 0
            for sel in selection_set.selections:
                if isinstance(sel, FieldNode):
                    total += self.field_cost(sel, parent)
                elif isinstance(sel, InlineFragmentNode):
                    total += self.selection_cost(sel.selection_set, parent)
                elif isinstance(sel, FragmentSpreadNode):
                    frag = self.context.get_fragment(sel.name.value)
                    if frag:
                        total += self.selection_cost(frag.selection_set,
                                                     parent)
            return total

        def field_cost(self, node: FieldNode, parent) -> int:
            name = node.name.value
            if name.startswith("__"):
                return 0
            field = parent.fields.get(name)
            if field is None:
                return 0
            child = self.selection_cost(node.selection_set,
                                        get_named_type(field.type))
            if not isinstance(get_nullable_type(field.type), GraphQLList):
                return 1 + child
            size = ASSUMED_LIST_SIZE
            first_arg = field.args.get("first")
            if first_arg is not None:
                size = first_arg.default_value or size
                for arg in node.arguments or ():
                    if arg.name.value != "first":
                        continue
                    if isinstance(arg.value, IntValueNode):
                        size = int(arg.value.value)
                    else:
                        size = MAX_FIRST
            return 1 + size * child

    return CostLimit


def main() -> None:
    """Print the cost of each operation file named on the command line."""
    import sys
    from pathlib import Path

    from graphql import build_schema, parse, validate

    schema = build_schema(
        (Path(__file__).resolve().parent / "schema.graphql").read_text())
    for name in sys.argv[1:]:
        errors = validate(schema, parse(Path(name).read_text()),
                          [make_cost_rule(0)])
        print(f"{Path(name).stem:16} cost {errors[0].extensions['cost']}")


if __name__ == "__main__":
    main()
