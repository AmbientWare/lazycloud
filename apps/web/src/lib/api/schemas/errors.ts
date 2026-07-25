import { z } from "zod";

/** Standard non-2xx JSON body from shared.http.errors.ErrorResponse. */
export const errorResponseSchema = z.object({ detail: z.string() }).strict();
export type ErrorResponse = z.infer<typeof errorResponseSchema>;
