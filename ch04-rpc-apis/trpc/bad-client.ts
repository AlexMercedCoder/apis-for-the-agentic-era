import { createTRPCClient, httpLink } from "@trpc/client";
import type { AppRouter } from "./server.ts";

const harbor = createTRPCClient<AppRouter>({
  links: [httpLink({ url: "http://127.0.0.1:8043" })],
});

await harbor.placeOrder.mutate({
  customer_id: "cus-001",
  lines: [{ book_id: "bk-1002", quantity: "1" }],
  request_id: "6f1c2a9e-3b4d-4e5f-8a7b-9c0d1e2f3a4b",
});

await harbor.getOrderStatus.query({ orderId: "ord-5002" });
