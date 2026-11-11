import { initTRPC, TRPCError } from "@trpc/server";
import { createHTTPServer } from "@trpc/server/adapters/standalone";
import { setTimeout as sleep } from "node:timers/promises";
import { z } from "zod";
import { getOrder, OrderError, PAYMENT_MS, placeOrder }
  from "./orders.ts";

// isDev: false keeps stack traces out of error responses.
const t = initTRPC.create({ isDev: false });

const CODES = { not_found: "NOT_FOUND", out_of_stock: "CONFLICT",
                request_id_reused: "CONFLICT" } as const;

function rethrow(err: unknown): never {
  if (err instanceof OrderError) {
    throw new TRPCError({ code: CODES[err.kind],
      message: err.hint ? `${err.message} ${err.hint}` : err.message });
  }
  throw err;
}

export const appRouter = t.router({
  getOrderStatus: t.procedure
    .input(z.object({ order_id: z.string().regex(/^ord-[0-9]{4}$/) }))
    .query(({ input }) => {
      try {
        const { order_id, status, placed_at, total_cents, currency } =
          getOrder(input.order_id);
        return { order_id, status, placed_at, total_cents, currency };
      } catch (err) { rethrow(err); }
    }),

  placeOrder: t.procedure
    .input(z.object({
      customer_id: z.string().regex(/^cus-[0-9]{3}$/),
      lines: z.array(z.object({
        book_id: z.string().regex(/^bk-[0-9]{4}$/),
        quantity: z.number().int().min(1).max(10),
      })).min(1).max(20),
      request_id: z.uuid(),
    }))
    .mutation(async ({ input }) => {
      await sleep(PAYMENT_MS);
      try {
        const [order, isNew] = placeOrder(
          input.customer_id, input.lines, input.request_id);
        console.error(`placeOrder ${isNew ? "new" : "replay"} ` +
                      order.order_id);
        return order;
      } catch (err) { rethrow(err); }
    }),
});

export type AppRouter = typeof appRouter;

const port = Number(process.env.PORT ?? 8043);
createHTTPServer({ router: appRouter }).listen(port, "127.0.0.1");
console.error(`harbor tRPC on http://127.0.0.1:${port}`);
