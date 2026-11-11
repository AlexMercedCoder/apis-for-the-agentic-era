"""Write proposed-openapi.json: a pull request's version of Harbor's contract.

It makes two changes a reviewer should catch: it renames Book's
price_cents to price (a break for every client that reads it), and it adds
POST /books/{book_id}/reviews without Harbor's error format or an
Idempotency-Key.
"""

import copy
import json
from pathlib import Path

here = Path(__file__).parent
doc = json.loads((here.parent.parent / "ch03-rest-apis" /
                  "openapi.json").read_text())
book = doc["components"]["schemas"]["Book"]
book["properties"]["price"] = book["properties"].pop("price_cents")
book["required"] = ["price" if r == "price_cents" else r
                    for r in book["required"]]
reviews = doc["paths"]["/books/{book_id}/reviews"]
reviews["post"] = {
    "summary": "Post a review", "operationId": "post_review",
    "tags": ["catalog"],
    "description": "Add a customer review of one book.",
    "parameters": copy.deepcopy(reviews["get"]["parameters"]),
    "requestBody": {"required": True, "content": {"application/json": {
        "schema": {"type": "object", "properties": {
            "rating": {"type": "integer"}, "text": {"type": "string"}}}}}},
    "responses": {"201": {"description": "Created"},
                  "400": {"description": "Bad request", "content": {
                      "application/json": {"schema": {"type": "object"}}}}}}
(here / "proposed-openapi.json").write_text(json.dumps(doc, indent=2) + "\n")
print("wrote proposed-openapi.json")
