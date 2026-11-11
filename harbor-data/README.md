# Harbor Books seed data

Harbor Books is a hypothetical online bookstore, the running example of the
book. Every chapter's services load their data from this folder, so the same
book, customer, and order look the same in SOAP, REST, gRPC, GraphQL, and the
agent protocols.

| File | Holds | Key |
|---|---|---|
| `books.json` | the catalog: title, author, first publication year, format, price, stock | `id`, such as `bk-1001` |
| `customers.json` | three customers | `id`, such as `cus-001` |
| `orders.json` | four orders in different states | `id`, such as `ord-5001` |

The titles, authors, and first publication years are real public-domain books.
Everything else is made up for the example: prices, stock counts, customers,
email addresses (all on `example.com`), dates, and orders.

Conventions later chapters rely on:

- Money is an integer number of cents in `price_cents`, `unit_price_cents`,
  and `total_cents`, with a `currency` field. No floating-point prices.
- Order `status` is one of `placed`, `paid`, `shipped`, `delivered`,
  `cancelled`.
- An order's `total_cents` equals the sum of `quantity * unit_price_cents`
  over its `lines`. `verify.sh` checks this.
- `bk-1004` (*The Time Machine*) has `in_stock: 0`, so chapters have an
  out-of-stock book to show a failed order against.

Add records here rather than inside a chapter folder, and add them at the end
of each file so existing IDs keep their meaning.
