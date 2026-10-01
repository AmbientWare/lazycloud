package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"

	"github.com/getkin/kin-openapi/openapi3filter"
	middleware "github.com/oapi-codegen/nethttp-middleware"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// maxRequestBytes bounds a request body. A submit of large inputs is the
// biggest request; one 16 MiB cloudpickle input is about 22 MiB as base64.
const maxRequestBytes = 64 << 20

// Owners are the domain owners the API invokes.
type Owners struct {
	Identity  *identity.Identity
	Control   *control.Control
	Storage   *storage.Storage
	Execution *execution.Execution
	Images    *images.Images
	// Listener wakes waits on task, image build and queue changes. It must
	// listen on database.ChannelTask, ChannelImageBuild, ChannelImageBuildLog
	// and storage.ChannelQueue.
	Listener *database.Listener
}

// NewHandler serves the public API: it limits the body, authenticates the
// bearer token, validates the request against the OpenAPI document and
// dispatches to the operation.
func NewHandler(owners Owners, logger *slog.Logger) (http.Handler, error) {
	spec, err := GetSwagger()
	if err != nil {
		return nil, fmt.Errorf("load openapi document: %w", err)
	}
	s := &Server{owners: owners, logger: logger}
	strict := NewStrictHandlerWithOptions(s, nil, StrictHTTPServerOptions{
		RequestErrorHandlerFunc: func(w http.ResponseWriter, r *http.Request, err error) {
			s.writeError(w, r, fmt.Errorf("%w: %w", errInvalidRequest, err))
		},
		ResponseErrorHandlerFunc: s.writeError,
	})
	validate := middleware.OapiRequestValidatorWithOptions(spec, &middleware.Options{
		Options: openapi3filter.Options{
			// authenticate runs first and enforces the bearer scheme.
			AuthenticationFunc: openapi3filter.NoopAuthenticationFunc,
		},
		SilenceServersWarning: true,
		ErrorHandlerWithOpts: func(_ context.Context, err error, w http.ResponseWriter, r *http.Request, opts middleware.ErrorHandlerOpts) {
			var tooLarge *http.MaxBytesError
			switch {
			case errors.As(err, &tooLarge):
				s.writeError(w, r, err)
			case opts.StatusCode == http.StatusNotFound || opts.StatusCode == http.StatusMethodNotAllowed:
				writeJSONError(w, opts.StatusCode, apitypes.NotFound, "no such operation")
			case opts.StatusCode >= http.StatusInternalServerError:
				s.writeError(w, r, err)
			default:
				// kin-openapi puts the useful sentence on the first line.
				message, _, _ := strings.Cut(err.Error(), "\n")
				writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, message)
			}
		},
	})
	handler := HandlerWithOptions(strict, StdHTTPServerOptions{
		ErrorHandlerFunc: func(w http.ResponseWriter, r *http.Request, err error) {
			s.writeError(w, r, fmt.Errorf("%w: %w", errInvalidRequest, err))
		},
	})
	return s.limitBody(s.authenticate(validate(handler))), nil
}

type principalKey struct{}

func principalFrom(ctx context.Context) (identity.Principal, bool) {
	p, ok := ctx.Value(principalKey{}).(identity.Principal)
	return p, ok
}

func (s *Server) limitBody(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, maxRequestBytes)
		next.ServeHTTP(w, r)
	})
}

// authenticate resolves the bearer token of every request to a principal.
func (s *Server) authenticate(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		scheme, token, ok := strings.Cut(r.Header.Get("Authorization"), " ")
		if !ok || !strings.EqualFold(scheme, "bearer") || token == "" {
			s.writeError(w, r, identity.ErrUnauthenticated)
			return
		}
		p, err := s.owners.Identity.Authenticate(r.Context(), token)
		if err != nil {
			s.writeError(w, r, err)
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), principalKey{}, p)))
	})
}

// errInvalidRequest marks request problems found outside schema validation.
var errInvalidRequest = errors.New("invalid request")

// writeError maps an owner error to the Error schema. Unexpected errors are
// logged and reported without detail.
func (s *Server) writeError(w http.ResponseWriter, r *http.Request, err error) {
	var (
		tooMany       *execution.TooManyPendingError
		invalidSpec   *control.InvalidSpecError
		sourceMissing *control.SourceMissingError
		tooLarge      *http.MaxBytesError
		storageInput  *storage.InvalidError
		storageState  *storage.ConflictError
		invalidImage  *images.InvalidError
	)
	switch {
	case errors.Is(err, identity.ErrUnauthenticated):
		writeJSONError(w, http.StatusUnauthorized, apitypes.Unauthenticated, "a valid bearer token is required")
	case errors.Is(err, identity.ErrForbidden):
		writeJSONError(w, http.StatusForbidden, apitypes.Forbidden, "the token cannot reach this workspace")
	case errors.Is(err, identity.ErrNotFound), errors.Is(err, control.ErrNotFound), errors.Is(err, execution.ErrNotFound),
		errors.Is(err, images.ErrNotFound):
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "not found")
	case errors.Is(err, execution.ErrNoResult):
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "the task finished without a result")
	case errors.As(err, &invalidSpec):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, invalidSpec.Error())
	case errors.As(err, &sourceMissing):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, sourceMissing.Error())
	case errors.Is(err, storage.ErrInvalidDigest), errors.Is(err, errInvalidRequest):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, err.Error())
	case errors.As(err, &invalidImage):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, err.Error())
	case errors.Is(err, images.ErrUnsupported):
		writeJSONError(w, http.StatusBadRequest, apitypes.Unsupported, err.Error())
	case errors.Is(err, images.ErrNotReady):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, err.Error())
	case errors.Is(err, images.ErrRegistryUnavailable):
		writeJSONError(w, http.StatusServiceUnavailable, apitypes.Unavailable, err.Error())
	case errors.As(err, &tooLarge), errors.Is(err, execution.ErrPayloadTooLarge):
		writeJSONError(w, http.StatusRequestEntityTooLarge, apitypes.PayloadTooLarge, "the request or a payload in it is too large")
	case errors.As(err, &tooMany):
		writeJSONError(w, http.StatusTooManyRequests, apitypes.TooManyPendingTasks, tooMany.Error())
	case errors.Is(err, execution.ErrNotAccepting):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, "the function is stopped or its app is paused")
	case errors.Is(err, execution.ErrTaskNotFinished):
		writeJSONError(w, http.StatusConflict, apitypes.TaskNotFinished, "the task has not finished")
	case errors.Is(err, storage.ErrNotFound):
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "not found")
	case errors.As(err, &storageInput):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, storageInput.Error())
	case errors.As(err, &storageState):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, storageState.Error())
	case errors.Is(err, storage.ErrTooLarge):
		writeJSONError(w, http.StatusRequestEntityTooLarge, apitypes.PayloadTooLarge, "a value is larger than 1 MiB")
	case errors.Is(err, storage.ErrBucketsUnconfigured):
		writeJSONError(w, http.StatusNotImplemented, apitypes.Unsupported, "this server has no workspace bucket provider")
	default:
		s.logger.ErrorContext(r.Context(), "request failed", "method", r.Method, "path", r.URL.Path, "error", err)
		writeJSONError(w, http.StatusInternalServerError, apitypes.Internal, "internal error")
	}
}

func writeJSONError(w http.ResponseWriter, status int, code apitypes.ErrorCode, message string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(apitypes.Error{Code: code, Message: message}) //nolint:errchkjson // The client is gone if this fails.
}
