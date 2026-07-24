import { z } from "zod";

const timestamp = z.string().regex(
  /^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$/,
);
const cursor = z.string().min(1).max(512);

// Kept outside the product renderer graph: release UI does not receive raw
// process streams. This model remains available to dedicated maintainer tools.
export const logEntryV2Schema = z.object({
  sequence: z.number().int().safe().min(1),
  occurred_at: timestamp,
  stream: z.enum(["system", "stdout", "stderr", "transcript"]),
  message: z.string().max(16_384).refine((value) => !/[\u0000-\u001f\u007f]/.test(value)),
}).strict();

export const logPageV2Schema = z.object({
  schema_version: z.literal("2"),
  items: z.array(logEntryV2Schema).max(100),
  next_cursor: cursor.nullable().default(null),
  has_more: z.boolean(),
}).strict().superRefine((value, context) => {
  if (value.has_more !== (value.next_cursor !== null)) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ["next_cursor"],
      message: "has_more must match next_cursor presence",
    });
  }
});

export type LogEntryV2 = z.infer<typeof logEntryV2Schema>;
export type LogPageV2 = z.infer<typeof logPageV2Schema>;
