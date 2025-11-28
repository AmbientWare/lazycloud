import { z } from "zod";

export enum JwtTokenExpiry {
  ONE_MINUTE = "1m",
  FIVE_MINUTES = "5m",
}

export const JwtTokenExpiresInOptionsSchema = z.nativeEnum(JwtTokenExpiry);

export const JwtTokenSchema = z.object({
  sub: z.string(), // this is actually just the user_id
  exp: z.number(), // Unix timestamp in seconds
});

export type JwtToken = z.infer<typeof JwtTokenSchema>;
export type JwtTokenExpiresInOptions = z.infer<
  typeof JwtTokenExpiresInOptionsSchema
>;
