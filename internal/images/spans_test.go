package images_test

import (
	"context"
	"testing"

	"go.opentelemetry.io/otel/codes"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"

	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// A build that ends records a span in the trace that asked for it, failed
// when the build failed: whether its host reported the failure or the
// workspace that started it was deleted.
func TestFailedBuildsAreTracedAsFailures(t *testing.T) {
	f := newFixture(t)
	spans := tracetest.NewSpanRecorder()
	provider := sdktrace.NewTracerProvider(sdktrace.WithSpanProcessor(spans))
	ctx, request := provider.Tracer("").Start(context.Background(), "request")
	defer request.End()
	host := f.host(t)
	failed := map[string]bool{}

	reported, err := f.images.Build(ctx, f.workspace(t, "a"), withEnv("reported"), false)
	if err != nil || reported.Build == nil {
		t.Fatalf("build: %+v %v", reported, err)
	}
	if _, err := f.images.CompleteBuild(ctx, host, placeAndStart(t, f, host), images.BuildOutcome{Failure: "a step failed"}); err != nil {
		t.Fatal(err)
	}
	failed[reported.Build.ID.String()] = true

	deleted := f.workspace(t, "b")
	orphaned, err := f.images.Build(ctx, deleted, withEnv("orphaned"), false)
	if err != nil || orphaned.Build == nil {
		t.Fatalf("build: %+v %v", orphaned, err)
	}
	if err := f.images.ReleaseWorkspace(ctx, deleted); err != nil {
		t.Fatal(err)
	}
	failed[orphaned.Build.ID.String()] = true

	for _, span := range spans.Ended() {
		if span.Name() != "images.build" {
			continue
		}
		for _, a := range span.Attributes() {
			if string(a.Key) == telemetry.AttrBuild && failed[a.Value.AsString()] {
				if span.Status().Code != codes.Error {
					t.Errorf("failed build %s is traced as %v", a.Value.AsString(), span.Status())
				}
				delete(failed, a.Value.AsString())
			}
		}
	}
	if len(failed) > 0 {
		t.Fatalf("failed builds %v recorded no span", failed)
	}
}
