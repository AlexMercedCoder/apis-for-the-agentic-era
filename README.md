# A Guide to APIs for the Agentic Era: companion code

Runnable examples for *A Guide to APIs for the Agentic Era: SOAP, REST, RPC, GraphQL, and the Protocols AI Agents Run On* by Alex Merced.

Every request, response, and error printed in the book comes from running the code in this repository. The examples follow one running example, Harbor Books, a hypothetical online bookstore whose same few operations (search the catalog, place an order, check its status, cancel and refund, announce a shipment) are built in each API style the book covers, so you can compare the styles on equal terms.

## Running the examples

Every chapter has a folder, from `ch01-what-is-an-api/` to `ch12-enabling-your-ai-agent/`, plus the shared seed data in `harbor-data/`. Each folder's README gives its setup steps and the `run.sh` that produces its `captures/`.

To check everything at once:

```bash
./verify.sh
```

It needs bash, `curl`, and [uv](https://docs.astral.sh/uv/). Chapters 4, 6, and 11 also need Node 24, and Chapter 4 needs [grpcurl](https://github.com/fullstorydev/grpcurl) 1.9.4. Chapters 7, 8, 10, 11, and 12 need Docker with Compose; each has its own `docker-compose.yml`, and the first run pulls `apache/polaris:1.8.0`, `rustfs/rustfs:1.0.5`, `quay.io/keycloak/keycloak:26.8.2`, and `apache/apisix:3.19.0-debian` (Chapter 11's governance checks also run `tufin/oasdiff:v1.37.0`). Apart from those pulls, nothing leaves `127.0.0.1`, and no API keys are needed. A full run takes several minutes; if a Docker chapter fails while its containers are still starting, run that chapter's `run.sh` again on its own.

## Layout

```text
README.md            this file; how to run everything, versions pinned
verify.sh            runs every example that needs no network or keys
harbor-data/         shared seed data (books, customers, orders)
ch01-what-is-an-api/
ch02-soap-apis/
ch03-rest-apis/
ch04-rpc-apis/
ch05-graphql-apis/
ch06-streaming-and-event-apis/
ch07-data-standards/
ch08-identity-standards/
ch09-payment-standards/
ch10-agent-protocols/
ch11-governing-and-securing-apis/
ch12-enabling-your-ai-agent/
```

The Docker chapters each carry their own `docker-compose.yml`.

Each chapter folder has its own README with setup steps, the commands the chapter runs, and a `captures/` folder holding the exact output the book prints.

## Languages

Python is the primary language. TypeScript appears where a style calls for it, such as tRPC and browser clients. Everything runs locally without paid accounts; where a chapter uses a provider's free test mode, a local stand-in is included.

## License

The code in this repository is released under the MIT License. See `LICENSE`. The book's text is not part of this repository and is not covered by this license.
