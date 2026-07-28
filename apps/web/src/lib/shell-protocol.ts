/**
 * Browser-side codec for the framed interactive shell protocol.
 *
 * Mirrors `shared.shell_protocol` (Python). Every frame is a 1-byte type, a
 * 4-byte big-endian payload length, then the payload. The gateway WebSocket
 * tunnel forwards these bytes verbatim to the in-container shell server, so the
 * browser must authenticate before sending stdin.
 */

const HEADER_SIZE = 5;
const MAX_PAYLOAD_BYTES = 1024 * 1024;

export const ShellFrameType = {
  Auth: 0x41, // "A"
  Data: 0x44, // "D" — stdin (client->server) or PTY output (server->client)
  Resize: 0x52, // "R"
  Ready: 0x4f, // "O"
  Error: 0x45, // "E"
  Exit: 0x58, // "X"
} as const;

export type ShellFrame = { type: number; payload: Uint8Array };

const encoder = new TextEncoder();

export function encodeShellFrame(
  type: number,
  payload: Uint8Array = new Uint8Array(),
): Uint8Array<ArrayBuffer> {
  const buffer = new ArrayBuffer(HEADER_SIZE + payload.length);
  const frame = new Uint8Array(buffer);
  frame[0] = type;
  new DataView(buffer).setUint32(1, payload.length, false);
  frame.set(payload, HEADER_SIZE);
  return frame;
}

export function encodeJsonFrame(type: number, value: unknown): Uint8Array<ArrayBuffer> {
  return encodeShellFrame(type, encoder.encode(JSON.stringify(value)));
}

/** Incremental decoder that reassembles frames across WebSocket message boundaries. */
export class ShellFrameDecoder {
  private buffer: Uint8Array = new Uint8Array(0);

  feed(data: Uint8Array): ShellFrame[] {
    this.buffer = concat(this.buffer, data);
    const frames: ShellFrame[] = [];
    for (;;) {
      if (this.buffer.length < HEADER_SIZE) break;
      const length = new DataView(this.buffer.buffer, this.buffer.byteOffset + 1, 4).getUint32(
        0,
        false,
      );
      if (length > MAX_PAYLOAD_BYTES) {
        throw new Error("shell frame payload exceeds maximum size");
      }
      const end = HEADER_SIZE + length;
      if (this.buffer.length < end) break;
      frames.push({
        type: this.buffer[0]!,
        payload: this.buffer.slice(HEADER_SIZE, end),
      });
      this.buffer = this.buffer.slice(end);
    }
    return frames;
  }
}

function concat(left: Uint8Array, right: Uint8Array): Uint8Array {
  if (left.length === 0) return right;
  const out = new Uint8Array(left.length + right.length);
  out.set(left, 0);
  out.set(right, left.length);
  return out;
}
