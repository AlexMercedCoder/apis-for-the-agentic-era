// Harbor's order rules for the tRPC server, the TypeScript twin of
// ../harbor_orders.py: same seed data, same IDs, same request_id replay.
import { readFileSync } from "node:fs";

const DATA = new URL("../../harbor-data/", import.meta.url);
const load = (name: string) =>
  JSON.parse(readFileSync(new URL(name, DATA), "utf8"));

type Book = { id: string; title: string; price_cents: number;
              in_stock: number };
export type OrderLine = { book_id: string; quantity: number;
                          unit_price_cents: number };
export type Order = {
  order_id: string; customer_id: string; status: string;
  placed_at: string; lines: OrderLine[]; total_cents: number;
  currency: string;
};
type SeedOrder = Omit<Order, "order_id"> & { id: string };

export class OrderError extends Error {
  readonly kind: "not_found" | "out_of_stock" | "request_id_reused";
  readonly hint?: string;
  constructor(kind: OrderError["kind"], message: string, hint?: string) {
    super(message);
    this.kind = kind;
    this.hint = hint;
  }
}

const books = new Map<string, Book>(
  load("books.json").map((b: Book) => [b.id, b]));
const customers = new Set<string>(
  load("customers.json").map((c: { id: string }) => c.id));
const orders = new Map<string, Order>(
  load("orders.json").map(({ id, ...o }: SeedOrder) =>
    [id, { order_id: id, ...o }]));
const requests = new Map<string, [string, string]>();
let nextId = 1 + Math.max(
  ...[...orders.keys()].map((id) => Number(id.split("-")[1])));

export const PAYMENT_MS = 150;  // the payment stand-in, as in Python

export function getOrder(orderId: string): Order {
  const order = orders.get(orderId);
  if (!order) {
    const ids = [...orders.keys()].sort();
    throw new OrderError("not_found", `No order with id ${orderId}.`,
      `Order IDs run from ${ids[0]} to ${ids.at(-1)}.`);
  }
  return structuredClone(order);
}

export function placeOrder(customerId: string,
                           lines: { book_id: string; quantity: number }[],
                           requestId: string): [Order, boolean] {
  if (!customers.has(customerId)) {
    throw new OrderError("not_found", `No customer ${customerId}.`);
  }
  const fingerprint = JSON.stringify([customerId, lines]);
  const seen = requests.get(requestId);
  if (seen) {
    if (seen[0] !== fingerprint) {
      throw new OrderError("request_id_reused",
        "This request_id was already used for a different order.",
        "Use a new request_id for a new order.");
    }
    return [getOrder(seen[1]), false];
  }
  const priced = lines.map((line) => {
    const book = books.get(line.book_id);
    if (!book) {
      throw new OrderError("not_found", `No book ${line.book_id}.`,
        "This catalog has IDs bk-1001 to bk-1008.");
    }
    if (book.in_stock < line.quantity) {
      throw new OrderError("out_of_stock",
        `${book.title} has ${book.in_stock} in stock.`,
        "Order fewer copies or another book.");
    }
    return { ...line, unit_price_cents: book.price_cents };
  });
  for (const line of priced) books.get(line.book_id)!.in_stock -=
    line.quantity;
  const order: Order = {
    order_id: `ord-${nextId++}`, customer_id: customerId,
    status: "placed",
    placed_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    lines: priced,
    total_cents: priced.reduce(
      (sum, l) => sum + l.quantity * l.unit_price_cents, 0),
    currency: "USD",
  };
  orders.set(order.order_id, order);
  requests.set(requestId, [fingerprint, order.order_id]);
  return [structuredClone(order), true];
}
