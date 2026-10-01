package runnerproto

import (
	"bytes"
	"encoding/binary"
	"errors"
	"io"
	"testing"
)

func TestFrameRoundTripAndLimits(t *testing.T) {
	var buf bytes.Buffer
	payload := []byte{0, 1, 2, 0xff}
	invoke := Invoke{Type: InvokeTypeInvoke, TaskId: "t", AttemptId: "a", InputEncoding: Cloudpickle}
	if err := WriteFrame(&buf, invoke, payload); err != nil {
		t.Fatal(err)
	}
	encoded := bytes.Clone(buf.Bytes())

	frame, err := ReadFrame(&buf)
	if err != nil {
		t.Fatal(err)
	}
	var got Invoke
	if err := frame.Decode(&got); err != nil {
		t.Fatal(err)
	}
	if frame.Type != FrameInvoke || got != invoke || !bytes.Equal(frame.Payload, payload) {
		t.Fatalf("round trip: %+v %+v %x", frame.Type, got, frame.Payload)
	}
	if _, err := ReadFrame(&buf); !errors.Is(err, io.EOF) {
		t.Fatalf("clean end: %v", err)
	}

	if _, err := ReadFrame(bytes.NewReader(encoded[:len(encoded)-1])); !errors.Is(err, io.ErrUnexpectedEOF) {
		t.Fatalf("truncated frame: %v", err)
	}

	var oversized [4]byte
	binary.BigEndian.PutUint32(oversized[:], MaxHeaderBytes+1)
	if _, err := ReadFrame(bytes.NewReader(oversized[:])); !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("oversized header: %v", err)
	}
	if err := WriteFrame(io.Discard, Loaded{Type: LoadedTypeLoaded}, make([]byte, MaxPayloadBytes+1)); !errors.Is(err, ErrFrameTooLarge) {
		t.Fatalf("oversized payload: %v", err)
	}
}
