import jwt from "jsonwebtoken";
import { env } from "@/env";
import {
  JwtTokenSchema,
  JwtTokenExpiry,
  type JwtTokenExpiresInOptions,
} from "@/interfaces/jwtTokens";

function convertExpiresInToMs(
  expiresIn: JwtTokenExpiresInOptions | number,
): number {
  if (typeof expiresIn === "number") return expiresIn;

  switch (expiresIn) {
    case JwtTokenExpiry.ONE_MINUTE:
      return 60 * 1000;
    case JwtTokenExpiry.FIVE_MINUTES:
      return 5 * 60 * 1000;
  }
}

export function generateJwtToken(
  userId: string,
  expiresIn: JwtTokenExpiresInOptions | number = JwtTokenExpiry.FIVE_MINUTES,
): string {
  const expiresInMs = convertExpiresInToMs(expiresIn);
  const payload = JwtTokenSchema.parse({
    sub: userId,
    exp: Math.floor((Date.now() + expiresInMs) / 1000),
  });
  return jwt.sign(payload, env.JWT_SECRET, { algorithm: "HS256" });
}
