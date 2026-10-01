package agent

import (
	"context"
	"net/http"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/AmbientWare/lazycloud/internal/hostproto"
)

const (
	testRegistryImage = "registry:3.1.2@sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8"
	testBuildBase     = "docker.io/library/busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
)

func (s *hostServer) CompleteImageBuild(_ context.Context, r *hostproto.CompleteImageBuildRequest) (*hostproto.CompleteImageBuildResponse, error) {
	s.builds <- r
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

	start := buildCommand(registry, "FROM "+testBuildBase+"\nRUN echo built > /proof\n")
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
	if out := e.server.buildOutput(); !strings.Contains(out, "RUN echo built > /proof") || !strings.Contains(out, "pushed "+registry) {
		t.Fatalf("build output streams to the server, got:\n%s", out)
	}

	// A host pulls the image by digest through the Docker Engine.
	reference := registry + "/lazycloud/images@" + digest
	t.Cleanup(func() { _ = exec.CommandContext(context.Background(), "docker", "image", "rm", "-f", reference).Run() })
	images := &imageCache{docker: e.docker}
	if _, err := images.ensure(t.Context(), reference, nil, "linux/amd64"); err != nil {
		t.Fatal(err)
	}
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
