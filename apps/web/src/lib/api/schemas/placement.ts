import { z } from "zod";

export const productRegionSchema = z.enum([
  "us-east",
  "us-west",
  "eu-central",
  "eu-north",
  "ap-southeast",
]);
export type ProductRegion = z.infer<typeof productRegionSchema>;
