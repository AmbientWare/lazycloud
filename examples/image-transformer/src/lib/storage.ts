import { mkdirSync, existsSync, writeFileSync, unlinkSync, readFileSync } from "fs";
import path from "path";

const DATA_DIR = process.env.DATA_DIR || "/app/data";
const IMAGES_DIR = path.join(DATA_DIR, "images");

// Ensure images directory exists
if (!existsSync(IMAGES_DIR)) {
  mkdirSync(IMAGES_DIR, { recursive: true });
}

export function getImagesDir(): string {
  return IMAGES_DIR;
}

export async function saveOriginalImage(
  id: string,
  dataUrl: string
): Promise<string> {
  const base64Data = dataUrl.replace(/^data:image\/\w+;base64,/, "");
  const buffer = Buffer.from(base64Data, "base64");

  const ext = dataUrl.match(/^data:image\/(\w+);/)?.[1] || "png";
  const filename = `${id}_original.${ext}`;
  const filepath = path.join(IMAGES_DIR, filename);

  writeFileSync(filepath, buffer);
  return filename;
}

export async function saveTransformedImage(
  id: string,
  imageUrl: string
): Promise<string> {
  const response = await fetch(imageUrl);
  if (!response.ok) {
    throw new Error(`Failed to fetch transformed image: ${response.statusText}`);
  }

  const contentType = response.headers.get("content-type") || "image/png";
  const ext = contentType.split("/")[1] || "png";
  const filename = `${id}_transformed.${ext}`;
  const filepath = path.join(IMAGES_DIR, filename);

  const buffer = Buffer.from(await response.arrayBuffer());
  writeFileSync(filepath, buffer);
  return filename;
}

export function deleteImages(id: string): void {
  const patterns = [`${id}_original`, `${id}_transformed`];
  const extensions = ["png", "jpg", "jpeg", "webp"];

  for (const pattern of patterns) {
    for (const ext of extensions) {
      const filepath = path.join(IMAGES_DIR, `${pattern}.${ext}`);
      if (existsSync(filepath)) {
        unlinkSync(filepath);
      }
    }
  }
}

export function getImagePath(filename: string): string {
  return path.join(IMAGES_DIR, filename);
}

export function getImageAsDataUrl(filename: string): string | null {
  const filepath = path.join(IMAGES_DIR, filename);
  if (!existsSync(filepath)) {
    return null;
  }

  const buffer = readFileSync(filepath);
  const ext = filename.split(".").pop()?.toLowerCase() || "png";
  const mimeType =
    {
      png: "image/png",
      jpg: "image/jpeg",
      jpeg: "image/jpeg",
      gif: "image/gif",
      webp: "image/webp",
    }[ext] || "image/png";

  return `data:${mimeType};base64,${buffer.toString("base64")}`;
}
