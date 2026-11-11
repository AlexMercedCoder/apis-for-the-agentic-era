// Parse and validate ../asyncapi.yaml with the AsyncAPI parser.
// Usage: node validate.mjs ../asyncapi.yaml
import { readFileSync } from "node:fs";
import { Parser } from "@asyncapi/parser";

const file = process.argv[2];
const parser = new Parser();
const { document, diagnostics } = await parser.parse(
  readFileSync(file, "utf8"));
const errors = diagnostics.filter((d) => d.severity === 0);
for (const d of diagnostics) {
  console.log(`severity ${d.severity} ${d.code}: ${d.message}`);
}
if (!document || errors.length) {
  console.log(`${file}: ${errors.length} errors`);
  process.exit(1);
}
const ops = document.operations().all().map(
  (o) => `${o.id()} (${o.action()} on ${o.channels().all()[0].address()})`);
console.log(`${file}: valid AsyncAPI ${document.version()}, ` +
  `operations: ${ops.join(", ")}`);
