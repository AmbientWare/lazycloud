package agent

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io/fs"
	"net/http"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
)

const (
	testRegistryImage = "registry:3.1.2@sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8"
	testBuildBase     = platformimages.Mount
)

func (s *hostServer) CompleteImageBuild(_ context.Context, r *hostproto.CompleteImageBuildRequest) (*hostproto.CompleteImageBuildResponse, error) {
	s.builds <- r
	s.mu.Lock()
	answer := s.answerBuild
	s.mu.Unlock()
	if answer != nil {
		return answer(r), nil
	}
	return &hostproto.CompleteImageBuildResponse{}, nil
}

func (s *hostServer) AppendImageBuildLogs(_ context.Context, r *hostproto.AppendImageBuildLogsRequest) (*hostproto.AppendImageBuildLogsResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, line := range r.GetLines() {
		s.buildLogs = append(s.buildLogs, line.GetData())
	}
	return &hostproto.AppendImageBuildLogsResponse{}, nil
}

func (s *hostServer) buildOutput() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	return strings.Join(s.buildLogs, "\n")
}

// startTestRegistry runs a registry on a loopback port for the test.
func startTestRegistry(t *testing.T) string {
	t.Helper()
	out, err := exec.CommandContext(t.Context(), "docker", "run", "-d", "--rm", "-p", "127.0.0.1::5000", testRegistryImage).Output()
	if err != nil {
		t.Fatalf("start registry: %v", err)
	}
	id := strings.TrimSpace(string(out))
	t.Cleanup(func() { _ = exec.CommandContext(context.Background(), "docker", "rm", "-f", id).Run() })
	port, err := exec.CommandContext(t.Context(), "docker", "port", id, "5000/tcp").Output()
	if err != nil {
		t.Fatal(err)
	}
	address := strings.TrimSpace(strings.Split(string(port), "\n")[0])
	for deadline := time.Now().Add(30 * time.Second); ; {
		resp, err := http.Get("http://" + address + "/v2/") //nolint:noctx // Readiness probe.
		if err == nil {
			_ = resp.Body.Close()
			return address
		}
		if time.Now().After(deadline) {
			t.Fatalf("registry did not start: %v", err)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// pullBuilt pulls an image a test build pushed, to look inside it.
func pullBuilt(t *testing.T, reference string) {
	t.Helper()
	if out, err := exec.CommandContext(t.Context(), "docker", "pull", "--platform", "linux/amd64", reference).CombinedOutput(); err != nil {
		t.Fatalf("pull %s: %v\n%s", reference, err, out)
	}
}

func buildCommand(registry, dockerfile string) *hostproto.ServerMessage {
	return &hostproto.ServerMessage{CommandId: uuid.NewString(), Body: &hostproto.ServerMessage_Start{Start: &hostproto.StartContainer{
		ContainerId: uuid.NewString(),
		Resources:   &hostproto.Resources{CpuMillis: 1000, MemoryBytes: 1 << 30, MemoryLimitBytes: 4 << 30},
		Build: &hostproto.ImageBuild{
			BuildId: uuid.NewString(), Attempt: 1, Dockerfile: dockerfile, Platform: "linux/amd64",
			PushRepository: registry + "/lazycloud/images", CacheRef: registry + "/lazycloud/cache:test",
			InsecureRegistry: true, Deadline: timestamppb.New(time.Now().Add(10 * time.Minute)),
		},
	}}}
}

func TestAgentBuildsPushesAndPullsAnImageByDigest(t *testing.T) {
	e := newEnv(t)
	registry := startTestRegistry(t)
	e.startAgent()
	session := e.session()

	// Rendered shell steps are heredocs.
	start := buildCommand(registry, "FROM "+testBuildBase+"\nRUN <<'LAZYCLOUD_STEP'\necho built > /proof\nLAZYCLOUD_STEP\n")
	container := start.GetStart().GetContainerId()
	session.send(t, start)
	session.phase(t, container, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	var outcome *hostproto.CompleteImageBuildRequest
	select {
	case outcome = <-e.server.builds:
	case <-time.After(5 * time.Minute):
		t.Fatal("the build reported no outcome")
	}
	digest := outcome.GetDigest()
	if outcome.GetContainerId() != container || !strings.HasPrefix(digest, "sha256:") {
		t.Fatalf("want a pushed digest, got %v\noutput:\n%s", outcome, e.server.buildOutput())
	}
	exit := session.phase(t, container, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_STOPPED {
		t.Fatalf("a finished build exits as stopped: %v", exit)
	}
	if out := e.server.buildOutput(); !strings.Contains(out, "echo built > /proof") || !strings.Contains(out, "pushed "+registry) {
		t.Fatalf("build output streams to the server, got:\n%s", out)
	}

	// A host pulls the image by digest through the Docker Engine.
	reference := registry + "/lazycloud/images@" + digest
	t.Cleanup(func() { _ = exec.CommandContext(context.Background(), "docker", "image", "rm", "-f", reference).Run() })
	pullBuilt(t, reference)
	proof, err := exec.CommandContext(t.Context(), "docker", "run", "--rm", reference, "cat", "/proof").Output()
	if err != nil || strings.TrimSpace(string(proof)) != "built" {
		t.Fatalf("the pulled image holds the build's file: %q %v", proof, err)
	}
	if e.containers() != 0 {
		t.Fatal("the build container is removed after its exit report")
	}
}

func TestAgentReportsAFailedBuildWithItsOutputTail(t *testing.T) {
	e := newEnv(t)
	registry := startTestRegistry(t)
	e.startAgent()
	session := e.session()

	start := buildCommand(registry, "FROM "+testBuildBase+"\nRUN echo about to fail && exit 3\n")
	session.send(t, start)
	select {
	case outcome := <-e.server.builds:
		failure := outcome.GetFailure()
		if !strings.Contains(failure, "exit code") || !strings.Contains(failure, "about to fail") {
			t.Fatalf("the failure carries the end of the output: %q", failure)
		}
	case <-time.After(5 * time.Minute):
		t.Fatal("the build reported no outcome")
	}
	session.phase(t, start.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
}

// A build secret reaches the step that mounts it and nothing else: not the
// image's files, history or config, and not the host once the build ends.
func TestAgentBuildSecretsReachOnlyTheStepThatMountsThem(t *testing.T) {
	e := newEnv(t)
	registry := startTestRegistry(t)
	e.startAgent()
	session := e.session()

	const secret = "s3cr3t-0f-the-build-7d1e"
	start := buildCommand(registry, "FROM "+testBuildBase+
		"\nRUN --mount=type=secret,id=TOKEN,env=TOKEN,required=true <<'LAZYCLOUD_STEP'\n"+
		"printf %s \"$TOKEN\" | sha256sum | cut -d' ' -f1 > /proof\nLAZYCLOUD_STEP\n"+
		"RUN test -z \"$TOKEN\" && ! test -e /run/secrets/TOKEN\n")
	start.GetStart().GetBuild().Secrets = map[string]string{"TOKEN": secret}
	container := start.GetStart().GetContainerId()
	session.send(t, start)
	var outcome *hostproto.CompleteImageBuildRequest
	select {
	case outcome = <-e.server.builds:
	case <-time.After(5 * time.Minute):
		t.Fatal("the build reported no outcome")
	}
	if !strings.HasPrefix(outcome.GetDigest(), "sha256:") {
		t.Fatalf("want a pushed digest, got %v\noutput:\n%s", outcome, e.server.buildOutput())
	}
	session.phase(t, container, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
	if strings.Contains(e.server.buildOutput(), secret) {
		t.Fatal("the build output holds the secret")
	}

	reference := registry + "/lazycloud/images@" + outcome.GetDigest()
	t.Cleanup(func() { _ = exec.CommandContext(context.Background(), "docker", "image", "rm", "-f", reference).Run() })
	pullBuilt(t, reference)
	proof, err := exec.CommandContext(t.Context(), "docker", "run", "--rm", reference, "cat", "/proof").Output()
	sum := sha256.Sum256([]byte(secret))
	if err != nil || strings.TrimSpace(string(proof)) != hex.EncodeToString(sum[:]) {
		t.Fatalf("the step read the secret: %q %v", proof, err)
	}
	history, err := exec.CommandContext(t.Context(), "docker", "history", "--no-trunc", reference).Output()
	if err != nil {
		t.Fatal(err)
	}
	config, err := exec.CommandContext(t.Context(), "docker", "image", "inspect", reference).Output()
	if err != nil {
		t.Fatal(err)
	}
	created, err := exec.CommandContext(t.Context(), "docker", "create", reference).Output()
	if err != nil {
		t.Fatal(err)
	}
	id := strings.TrimSpace(string(created))
	t.Cleanup(func() { _ = exec.CommandContext(context.Background(), "docker", "rm", "-f", id).Run() })
	files, err := exec.CommandContext(t.Context(), "docker", "export", id).Output()
	if err != nil {
		t.Fatal(err)
	}
	for what, data := range map[string][]byte{"history": history, "config": config, "files": files} {
		if bytes.Contains(data, []byte(secret)) {
			t.Fatalf("the image's %s hold the secret", what)
		}
	}
	err = filepath.WalkDir(e.stateDir, func(path string, d fs.DirEntry, err error) error {
		if err == nil && d.Name() == "TOKEN" {
			return fmt.Errorf("the host still holds %s", path)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

// BuildKit keeps a secret's value out of the cache key, so a step reading a
// rotated secret reuses the cached layer unless the secret versions argument
// the images owner renders changes with it.
func TestAgentRebuildsASecretStepOnlyWhenItsVersionsChange(t *testing.T) {
	e := newEnv(t)
	registry := startTestRegistry(t)
	e.startAgent()
	session := e.session()
	build := func(versions, secret string) string {
		t.Helper()
		start := buildCommand(registry, "FROM "+testBuildBase+"\nARG LAZYCLOUD_BUILD_SECRET_VERSIONS="+versions+
			"\nRUN --mount=type=secret,id=TOKEN,env=TOKEN,required=true <<'LAZYCLOUD_STEP'\n"+
			"printf %s \"$TOKEN\" > /proof\nLAZYCLOUD_STEP\n")
		start.GetStart().GetBuild().Secrets = map[string]string{"TOKEN": secret}
		session.send(t, start)
		var outcome *hostproto.CompleteImageBuildRequest
		select {
		case outcome = <-e.server.builds:
		case <-time.After(5 * time.Minute):
			t.Fatal("the build reported no outcome")
		}
		session.phase(t, start.GetStart().GetContainerId(), hostproto.ContainerPhase_CONTAINER_PHASE_EXITED)
		reference := registry + "/lazycloud/images@" + outcome.GetDigest()
		t.Cleanup(func() { _ = exec.CommandContext(context.Background(), "docker", "image", "rm", "-f", reference).Run() })
		pullBuilt(t, reference)
		proof, err := exec.CommandContext(t.Context(), "docker", "run", "--rm", reference, "cat", "/proof").Output()
		if err != nil {
			t.Fatal(err)
		}
		return string(proof)
	}
	if got := build("v1", "first"); got != "first" {
		t.Fatalf("the first build read %q", got)
	}
	if got := build("v1", "second"); got != "first" {
		t.Fatalf("with the same versions the cached step stands: %q", got)
	}
	if got := build("v2", "second"); got != "second" {
		t.Fatalf("new versions run the step again: %q", got)
	}
}

func TestAgentStopsABuildWithoutAnOutcome(t *testing.T) {
	e := newEnv(t)
	registry := startTestRegistry(t)
	e.startAgent()
	session := e.session()

	start := buildCommand(registry, "FROM "+testBuildBase+"\nRUN sleep 300\n")
	container := start.GetStart().GetContainerId()
	session.send(t, start)
	session.phase(t, container, hostproto.ContainerPhase_CONTAINER_PHASE_READY)
	session.send(t, stopCommand(container, 10))
	exit := session.phase(t, container, hostproto.ContainerPhase_CONTAINER_PHASE_EXITED).GetExit()
	if exit.GetReason() != hostproto.ExitReason_EXIT_REASON_STOPPED {
		t.Fatalf("a stopped build exits as stopped: %v", exit)
	}
	select {
	case outcome := <-e.server.builds:
		t.Fatalf("a stopped build reports no outcome, got %v", outcome)
	default:
	}
}
