import { createTRPCClient, httpLink } from "@trpc/client";
import type { AppRouter } from "./server.ts";

const harbor = createTRPCClient<AppRouter>({
  links: [httpLink({ url: "http://127.0.0.1:8043" })],
});

const order = await harbor.placeOrder.mutate({
  customer_id: "cus-001",
  lines: [
    { book_id: "bk-1002", quantity: 1 },
    { book_id: "bk-1008", quantity: 2 },
  ],
  request_id: "6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b",
});
console.log(order.order_id, order.status, order.total_cents);

const status = await harbor.getOrderStatus.query({
  order_id: "ord-5002",
});
console.log(status.order_id, status.status);
