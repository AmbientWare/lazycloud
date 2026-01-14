import { Database } from "bun:sqlite";
import { mkdirSync, existsSync } from "fs";
import { dirname } from "path";

const DB_PATH = process.env.DB_PATH || "/app/data/images.db";

// Ensure directory exists
const dir = dirname(DB_PATH);
if (!existsSync(dir)) {
  mkdirSync(dir, { recursive: true });
}

const db = new Database(DB_PATH);

// Initialize schema
db.run(`
  CREATE TABLE IF NOT EXISTS transformations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_url TEXT NOT NULL,
    transformed_url TEXT,
    style TEXT NOT NULL,
    prompt TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
  )
`);

export interface Transformation {
  id: number;
  original_url: string;
  transformed_url: string | null;
  style: string;
  prompt: string | null;
  status: string;
  created_at: string;
}

export function createTransformation(
  originalUrl: string,
  style: string,
  prompt?: string
): number {
  const result = db
    .prepare(
      "INSERT INTO transformations (original_url, style, prompt) VALUES (?, ?, ?)"
    )
    .run(originalUrl, style, prompt || null);
  return Number(result.lastInsertRowid);
}

export function updateTransformation(
  id: number,
  transformedUrl: string,
  status: string
) {
  db.prepare(
    "UPDATE transformations SET transformed_url = ?, status = ? WHERE id = ?"
  ).run(transformedUrl, status, id);
}

export function getTransformations(limit = 20): Transformation[] {
  return db
    .prepare("SELECT * FROM transformations ORDER BY created_at DESC LIMIT ?")
    .all(limit) as Transformation[];
}

export function getTransformation(id: number): Transformation | undefined {
  return db
    .prepare("SELECT * FROM transformations WHERE id = ?")
    .get(id) as Transformation | undefined;
}
