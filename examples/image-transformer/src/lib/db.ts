import { Database } from "bun:sqlite";
import { mkdirSync, existsSync } from "fs";
import path from "path";

const DATA_DIR = process.env.DATA_DIR || "/app/data";
const DB_PATH = path.join(DATA_DIR, "transformations.db");

// Ensure data directory exists
if (!existsSync(DATA_DIR)) {
  mkdirSync(DATA_DIR, { recursive: true });
}

const db = new Database(DB_PATH, { create: true });

// Initialize schema
db.exec(`
  CREATE TABLE IF NOT EXISTS transformations (
    id TEXT PRIMARY KEY,
    original_filename TEXT NOT NULL,
    original_path TEXT NOT NULL,
    transformed_path TEXT,
    style TEXT NOT NULL,
    custom_prompt TEXT,
    status TEXT DEFAULT 'pending',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
  )
`);

export interface Transformation {
  id: string;
  original_filename: string;
  original_path: string;
  transformed_path: string | null;
  style: string;
  custom_prompt: string | null;
  status: "pending" | "processing" | "completed" | "failed";
  created_at: string;
  completed_at: string | null;
}

export function createTransformation(data: {
  id: string;
  original_filename: string;
  original_path: string;
  style: string;
  custom_prompt?: string;
}): Transformation {
  const stmt = db.prepare(`
    INSERT INTO transformations (id, original_filename, original_path, style, custom_prompt, status)
    VALUES (?, ?, ?, ?, ?, 'processing')
    RETURNING *
  `);
  return stmt.get(
    data.id,
    data.original_filename,
    data.original_path,
    data.style,
    data.custom_prompt || null
  ) as Transformation;
}

export function completeTransformation(
  id: string,
  transformed_path: string
): Transformation | null {
  const stmt = db.prepare(`
    UPDATE transformations
    SET transformed_path = ?, status = 'completed', completed_at = CURRENT_TIMESTAMP
    WHERE id = ?
    RETURNING *
  `);
  return stmt.get(transformed_path, id) as Transformation | null;
}

export function failTransformation(id: string): void {
  const stmt = db.prepare(`UPDATE transformations SET status = 'failed' WHERE id = ?`);
  stmt.run(id);
}

export function getTransformation(id: string): Transformation | null {
  const stmt = db.prepare(`SELECT * FROM transformations WHERE id = ?`);
  return stmt.get(id) as Transformation | null;
}

export function getRecentTransformations(limit = 20): Transformation[] {
  const stmt = db.prepare(`
    SELECT * FROM transformations
    WHERE status = 'completed'
    ORDER BY created_at DESC
    LIMIT ?
  `);
  return stmt.all(limit) as Transformation[];
}

export function deleteTransformation(id: string): boolean {
  const stmt = db.prepare(`DELETE FROM transformations WHERE id = ?`);
  const result = stmt.run(id);
  return result.changes > 0;
}
