# A Guide to APIs for the Agentic Era: companion code

Runnable examples for *A Guide to APIs for the Agentic Era: SOAP, REST, RPC, GraphQL, and the Protocols AI Agents Run On* by Alex Merced.

Every request, response, and error printed in the book comes from running the code in this repository. The examples follow one running example, Harbor Books, a hypothetical online bookstore whose same few operations (search the catalog, place an order, check its status, cancel and refund, announce a shipment) are built in each API style the book covers, so you can compare the styles on equal terms.

## Status

The book is being written. Chapter folders are added here as each chapter is drafted, so this repository fills in over time. Available now: `harbor-data/`, `ch01-what-is-an-api/`, `ch02-soap-apis/`, `ch03-rest-apis/`, `ch04-rpc-apis/`, `ch05-graphql-apis/`, `ch06-streaming-and-event-apis/`, `ch07-data-standards/`, and `ch08-identity-standards/`.

To check everything that runs offline:

```bash
./verify.sh
```

It needs bash, `curl`, and [uv](https://docs.astral.sh/uv/); Chapters 4 and 6 also need Node 24, Chapter 4 needs [grpcurl](https://github.com/fullstorydev/grpcurl) 1.9.4, and Chapters 7 and 8 need Docker with Compose (they pull `apache/polaris:1.8.0`, `rustfs/rustfs:1.0.5`, and `quay.io/keycloak/keycloak:26.8.2` on first run). Apart from that pull, it touches nothing beyond `127.0.0.1`.

## Layout

```text
README.md            this file; how to run everything, versions pinned
verify.sh            runs every example that needs no network or keys
docker-compose.yml   identity provider, gateway, catalog, object store
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

Each chapter folder has its own README with setup steps, the commands the chapter runs, and a `captures/` folder holding the exact output the book prints.

## Languages

Python is the primary language. TypeScript appears where a style calls for it, such as tRPC and browser clients. Everything runs locally without paid accounts; where a chapter uses a provider's free test mode, a local stand-in is included.

## License

The code in this repository is released under the MIT License. See `LICENSE`. The book's text is not part of this repository and is not covered by this license.
