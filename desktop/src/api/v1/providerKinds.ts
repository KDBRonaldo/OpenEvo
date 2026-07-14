import { z } from "zod";

export const providerKindSchema = z.enum([
  "desktop_sidecar",
  "contract_simulator",
  "scaffold",
  "dry_run",
]);
