import { z } from "zod";

// The packaged renderer accepts only the native release provider. Vite aliases
// the development provider module to this parser for the product build.
export const providerKindSchema = z.literal("desktop_sidecar");
