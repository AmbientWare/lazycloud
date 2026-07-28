export type DecodedValue =
  { kind: "empty" } | { kind: "binary"; size: number } | { kind: "text"; value: string };

export function decodeEncodedValue(valueBase64: string): DecodedValue {
  if (!valueBase64) return { kind: "empty" };
  try {
    const binary = atob(valueBase64);
    const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
    if (isBinary(bytes)) return { kind: "binary", size: bytes.length };
    const text = new TextDecoder("utf-8", { fatal: false }).decode(bytes);
    try {
      return { kind: "text", value: JSON.stringify(JSON.parse(text), null, 2) };
    } catch {
      return { kind: "text", value: text };
    }
  } catch {
    return { kind: "binary", size: 0 };
  }
}

function isBinary(bytes: Uint8Array): boolean {
  const sample = bytes.subarray(0, 512);
  if (sample.some((byte) => byte === 0)) return true;
  let controlCharacters = 0;
  for (const byte of sample) {
    if (byte < 9 || (byte > 13 && byte < 32)) controlCharacters += 1;
  }
  return sample.length > 0 && controlCharacters / sample.length > 0.1;
}
