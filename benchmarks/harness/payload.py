from __future__ import annotations

import hashlib
from pathlib import Path

PAYLOAD_BLOCK_SIZE = 64 * 1024


def deterministic_payload_range(nonce: str, label: str, offset: int, length: int) -> bytes:
    if offset < 0 or length < 0:
        msg = "payload offset and length must be non-negative"
        raise ValueError(msg)

    out = bytearray()
    cursor = offset
    while len(out) < length:
        block_index = cursor // PAYLOAD_BLOCK_SIZE
        block = _payload_block(nonce, label, block_index)
        block_offset = cursor % PAYLOAD_BLOCK_SIZE
        take = min(length - len(out), len(block) - block_offset)
        out.extend(block[block_offset : block_offset + take])
        cursor += take
    return bytes(out)


def deterministic_sha256(nonce: str, label: str, size: int) -> str:
    if size < 0:
        msg = "payload size must be non-negative"
        raise ValueError(msg)
    hasher = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk_size = min(PAYLOAD_BLOCK_SIZE, size - offset)
        hasher.update(deterministic_payload_range(nonce, label, offset, chunk_size))
        offset += chunk_size
    return hasher.hexdigest()


def write_deterministic_payload(path: Path, *, nonce: str, label: str, size: int) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    offset = 0
    with path.open("wb") as handle:
        while offset < size:
            chunk_size = min(PAYLOAD_BLOCK_SIZE, size - offset)
            chunk = deterministic_payload_range(nonce, label, offset, chunk_size)
            handle.write(chunk)
            hasher.update(chunk)
            offset += chunk_size
    return hasher.hexdigest()


def _payload_block(nonce: str, label: str, block_index: int) -> bytes:
    seed = f"{nonce}\0{label}\0{block_index}".encode()
    material = bytearray()
    counter = 0
    while len(material) < PAYLOAD_BLOCK_SIZE:
        digest = hashlib.blake2b(seed + counter.to_bytes(4, "big"), digest_size=64).digest()
        material.extend(digest)
        counter += 1
    return bytes(material[:PAYLOAD_BLOCK_SIZE])
