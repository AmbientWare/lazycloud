import type { NextConfig } from "next";

// A static export: `next build` writes plain files to out/, which FastAPI serves.
const config: NextConfig = {
  output: "export",
};

export default config;
