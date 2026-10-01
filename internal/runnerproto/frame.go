package runnerproto

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
)

// Frame size limits from contracts/runner.yaml.
const (
	MaxHeaderBytes  = 1 << 20
	MaxPayloadBytes = 64 << 20
)

// ErrFrameTooLarge reports a header or payload over its limit.
var ErrFrameTooLarge = errors.New("runner frame exceeds its size limit")

// FrameType is the `type` field that selects a header's schema.
type FrameType string

const (
	FrameLoad       FrameType = "load"
	FrameLoaded     FrameType = "loaded"
	FrameLoadFailed FrameType = "load_failed"
	FrameInvoke     FrameType = "invoke"
	FrameSucceeded  FrameType = "succeeded"
	FrameFailed     FrameType = "failed"
	FrameOutput     FrameType = "output"
)

// Frame is one decoded frame: its JSON header and raw payload.
type Frame struct {
	Type    FrameType
	Header  json.RawMessage
	Payload []byte
}

// Decode unmarshals the header into v, one of the generated message types.
func (f Frame) Decode(v any) error {
	if err := json.Unmarshal(f.Header, v); err != nil {
		return fmt.Errorf("decode %s header: %w", f.Type, err)
	}
	return nil
}

// WriteFrame writes header as JSON followed by payload in a single write, so a
// frame is never interleaved with another writer's bytes.
func WriteFrame(w io.Writer, header any, payload []byte) error {
	encoded, err := json.Marshal(header)
	if err != nil {
		return fmt.Errorf("encode frame header: %w", err)
	}
	if len(encoded) > MaxHeaderBytes || len(payload) > MaxPayloadBytes {
		return ErrFrameTooLarge
	}
	var headerLength, payloadLength [4]byte
	binary.BigEndian.PutUint32(headerLength[:], uint32(len(encoded)))  //nolint:gosec // bounded by MaxHeaderBytes
	binary.BigEndian.PutUint32(payloadLength[:], uint32(len(payload))) //nolint:gosec // bounded by MaxPayloadBytes
	buffers := net.Buffers{headerLength[:], encoded, payloadLength[:], payload}
	if _, err := buffers.WriteTo(w); err != nil {
		return fmt.Errorf("write frame: %w", err)
	}
	return nil
}

// ReadFrame reads one frame. It returns io.EOF only when the stream ends
// cleanly between frames.
func ReadFrame(r io.Reader) (Frame, error) {
	header, err := readSection(r, MaxHeaderBytes)
	if err != nil {
		return Frame{}, err
	}
	payload, err := readSection(r, MaxPayloadBytes)
	if err != nil {
		return Frame{}, unexpectedEOF(err)
	}
	var envelope struct {
		Type FrameType `json:"type"`
	}
	if err := json.Unmarshal(header, &envelope); err != nil {
		return Frame{}, fmt.Errorf("decode frame header: %w", err)
	}
	if envelope.Type == "" {
		return Frame{}, errors.New("frame header has no type")
	}
	return Frame{Type: envelope.Type, Header: header, Payload: payload}, nil
}

func readSection(r io.Reader, limit uint32) ([]byte, error) {
	var length [4]byte
	if _, err := io.ReadFull(r, length[:]); err != nil {
		if errors.Is(err, io.EOF) {
			return nil, io.EOF
		}
		return nil, fmt.Errorf("read frame length: %w", err)
	}
	n := binary.BigEndian.Uint32(length[:])
	if n > limit {
		return nil, ErrFrameTooLarge
	}
	data := make([]byte, n)
	if _, err := io.ReadFull(r, data); err != nil {
		return nil, fmt.Errorf("read frame: %w", unexpectedEOF(err))
	}
	return data, nil
}

func unexpectedEOF(err error) error {
	if errors.Is(err, io.EOF) {
		return io.ErrUnexpectedEOF
	}
	return err
}
