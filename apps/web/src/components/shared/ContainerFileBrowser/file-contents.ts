/**
 * What a container file's leading bytes are, for its preview. Nothing here runs
 * or parses the content beyond reading format headers and `JSON.parse`.
 */

export type FileContents =
  | { kind: "image"; label: string; mime: string }
  | { kind: "text"; label: string; text: string }
  | { kind: "binary"; label: string };

/** Image types a preview draws; each also names the MIME type its blob needs. */
const IMAGE_EXTENSIONS: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  webp: "image/webp",
  svg: "image/svg+xml",
};

/** The image MIME type a file's extension names; a dotfile or a name with no dot has none. */
export function imageMimeForName(name: string): string | undefined {
  const dot = name.lastIndexOf(".");
  if (dot <= 0) return undefined;
  const extension = name.slice(dot + 1).toLowerCase();
  return Object.hasOwn(IMAGE_EXTENSIONS, extension) ? IMAGE_EXTENSIONS[extension] : undefined;
}

type Signature = { label: string; image?: string; matches: (bytes: Uint8Array) => boolean };

const SIGNATURES: Signature[] = [
  { label: "PNG image", image: "image/png", matches: startsWith([0x89, 0x50, 0x4e, 0x47]) },
  { label: "JPEG image", image: "image/jpeg", matches: startsWith([0xff, 0xd8, 0xff]) },
  { label: "GIF image", image: "image/gif", matches: startsWith(ascii("GIF8")) },
  {
    label: "WebP image",
    image: "image/webp",
    matches: (bytes) => startsWith(ascii("RIFF"))(bytes) && startsWith(ascii("WEBP"), 8)(bytes),
  },
  { label: "PDF document", matches: startsWith(ascii("%PDF-")) },
  { label: "gzip archive", matches: startsWith([0x1f, 0x8b]) },
  { label: "Zip archive", matches: startsWith([0x50, 0x4b, 0x03, 0x04]) },
  { label: "Zip archive", matches: startsWith([0x50, 0x4b, 0x05, 0x06]) },
  {
    label: "bzip2 archive",
    matches: (bytes) => startsWith(ascii("BZh"))(bytes) && looksBinary(bytes),
  },
  { label: "xz archive", matches: startsWith([0xfd, 0x37, 0x7a, 0x58, 0x5a, 0x00]) },
  { label: "Zstandard archive", matches: startsWith([0x28, 0xb5, 0x2f, 0xfd]) },
  { label: "7-Zip archive", matches: startsWith([0x37, 0x7a, 0xbc, 0xaf, 0x27, 0x1c]) },
  { label: "tar archive", matches: startsWith(ascii("ustar"), 257) },
  { label: "Debian package", matches: startsWith(ascii("!<arch>\ndebian-binary")) },
  { label: "ar archive", matches: startsWith(ascii("!<arch>\n")) },
  { label: "SQLite database", matches: startsWith(ascii("SQLite format 3\0")) },
  { label: "WebAssembly module", matches: startsWith([0x00, 0x61, 0x73, 0x6d]) },
  {
    label: "Windows executable",
    matches: (bytes) => startsWith(ascii("MZ"))(bytes) && looksBinary(bytes),
  },
  { label: "Mach-O executable", matches: startsWith([0xcf, 0xfa, 0xed, 0xfe]) },
  { label: "Mach-O executable", matches: startsWith([0xce, 0xfa, 0xed, 0xfe]) },
];

/**
 * Classify a file from its name and leading bytes.
 *
 * `complete` says the bytes are the whole file: an image drawn from part of one
 * would render broken, and JSON cut short does not parse.
 */
export function describeFileContents(
  name: string,
  bytes: Uint8Array,
  complete: boolean,
): FileContents {
  if (startsWith([0x7f, 0x45, 0x4c, 0x46])(bytes))
    return { kind: "binary", label: elfLabel(bytes) };
  const signature = SIGNATURES.find((candidate) => candidate.matches(bytes));
  if (signature) {
    return signature.image && complete
      ? { kind: "image", label: signature.label, mime: signature.image }
      : { kind: "binary", label: signature.label };
  }
  if (looksBinary(bytes)) return { kind: "binary", label: "Binary data" };

  const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
  if (complete && imageMimeForName(name) === "image/svg+xml" && /<svg[\s>]/.test(text)) {
    return { kind: "image", label: "SVG image", mime: "image/svg+xml" };
  }
  const pretty = complete ? prettyJson(name, text) : undefined;
  if (pretty !== undefined) return { kind: "text", label: "JSON", text: pretty };
  return { kind: "text", label: "Text", text };
}

/** Offset, hex and printable columns, sixteen bytes to a row. */
export function hexDump(bytes: Uint8Array): string {
  const rows: string[] = [];
  for (let offset = 0; offset < bytes.length; offset += 16) {
    const row = bytes.subarray(offset, offset + 16);
    const hex = Array.from(
      row,
      (byte, index) => (index === 8 ? " " : "") + byte.toString(16).padStart(2, "0"),
    ).join(" ");
    const printable = Array.from(row, (byte) =>
      byte >= 0x20 && byte < 0x7f ? String.fromCharCode(byte) : ".",
    ).join("");
    rows.push(`${offset.toString(16).padStart(8, "0")}  ${hex.padEnd(48)}  |${printable}|`);
  }
  return rows.join("\n");
}

/** `ls -l` permissions from a stat mode, or undefined when the listing carried none. */
export function formatMode(mode: number): string | undefined {
  if (!mode) return undefined;
  const type: Record<number, string> = {
    0o040000: "d",
    0o120000: "l",
    0o020000: "c",
    0o060000: "b",
    0o010000: "p",
    0o140000: "s",
  };
  const special = [0o4000, 0o2000, 0o1000];
  let result = type[mode & 0o170000] ?? "-";
  for (let who = 0; who < 3; who += 1) {
    const bits = (mode >> (6 - who * 3)) & 0o7;
    const execute = (bits & 1) !== 0;
    const marker = who === 2 ? "t" : "s";
    result += bits & 4 ? "r" : "-";
    result += bits & 2 ? "w" : "-";
    result +=
      (mode & special[who]) !== 0 ? (execute ? marker : marker.toUpperCase()) : execute ? "x" : "-";
  }
  return result;
}

function elfLabel(bytes: Uint8Array): string {
  const is64 = bytes[4] === 2;
  const littleEndian = bytes[5] !== 2;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const u16 = (offset: number) =>
    offset + 2 <= bytes.length ? view.getUint16(offset, littleEndian) : undefined;
  const machine = ELF_MACHINES[u16(18) ?? -1];
  const kind = elfKind(u16(16), () => hasInterpreter(view, is64, littleEndian));
  return [`ELF ${is64 ? 64 : 32}-bit ${kind}`, machine].filter(Boolean).join(", ");
}

const ELF_MACHINES: Record<number, string> = {
  0x03: "x86",
  0x28: "ARM",
  0x3e: "x86-64",
  0xb7: "AArch64",
  0xf3: "RISC-V",
};

function elfKind(type: number | undefined, interpreted: () => boolean): string {
  if (type === 1) return "object file";
  if (type === 2) return "executable";
  if (type === 3) return interpreted() ? "executable" : "shared library";
  if (type === 4) return "core dump";
  return "file";
}

/** Whether the program headers ask for a dynamic loader, which a position-independent executable does and a library does not. */
function hasInterpreter(view: DataView, is64: boolean, littleEndian: boolean): boolean {
  const read = (offset: number, width: 2 | 4 | 8): number | undefined => {
    if (offset + width > view.byteLength) return undefined;
    if (width === 2) return view.getUint16(offset, littleEndian);
    if (width === 4) return view.getUint32(offset, littleEndian);
    return Number(view.getBigUint64(offset, littleEndian));
  };
  const tableOffset = is64 ? read(32, 8) : read(28, 4);
  const entrySize = read(is64 ? 54 : 42, 2);
  const entries = read(is64 ? 56 : 44, 2);
  if (tableOffset === undefined || !entrySize || entries === undefined) return false;
  for (let index = 0; index < entries; index += 1) {
    const PT_INTERP = 3;
    if (read(tableOffset + index * entrySize, 4) === PT_INTERP) return true;
  }
  return false;
}

function looksBinary(bytes: Uint8Array): boolean {
  return bytes.subarray(0, 8192).includes(0);
}

/** Minified JSON, indented. JSON its author already laid out is shown as written. */
function prettyJson(name: string, text: string): string | undefined {
  const trimmed = text.trim();
  if (trimmed.includes("\n")) return undefined;
  if (
    !name.toLowerCase().endsWith(".json") &&
    !trimmed.startsWith("{") &&
    !trimmed.startsWith("[")
  ) {
    return undefined;
  }
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return undefined;
  }
}

function startsWith(prefix: readonly number[], offset = 0): (bytes: Uint8Array) => boolean {
  return (bytes) =>
    bytes.length >= offset + prefix.length &&
    prefix.every((byte, index) => bytes[offset + index] === byte);
}

function ascii(value: string): number[] {
  return Array.from(value, (character) => character.charCodeAt(0));
}
