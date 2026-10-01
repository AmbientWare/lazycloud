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
	"github.com/AmbientWare/lazycloud/internal/billing"
	"github.com/AmbientWare/lazycloud/internal/control"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/identity"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/notifications"
	"github.com/AmbientWare/lazycloud/internal/observability"
	"github.com/AmbientWare/lazycloud/internal/schedules"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
	"github.com/AmbientWare/lazycloud/internal/telemetry"
)

// maxRequestBytes bounds a request body. A submit of large inputs is the
// biggest request; one 16 MiB cloudpickle input is about 22 MiB as base64.
const maxRequestBytes = 64 << 20

// Owners are the domain owners the API invokes.
type Owners struct {
	Identity      *identity.Identity
	Control       *control.Control
	Storage       *storage.Storage
	Execution     *execution.Execution
	Images        *images.Images
	Notifications *notifications.Notifications
	Secrets       *secrets.Secrets
	Schedules     *schedules.Schedules
	Billing       *billing.Billing
	Observability *observability.Observability
	// Changes fans out the workspace change streams.
	Changes *observability.Changes
	// Listener wakes waits on task, image build and queue changes. It must
	// listen on database.ChannelTask, ChannelImageBuild, ChannelImageBuildLog
	// and storage.ChannelQueue.
	Listener *database.Listener
}

// Config is what the transport needs beyond the owners.
type Config struct {
	// PublicURL is the dashboard origin. Cookie-authenticated requests that
	// change state must send it as their Origin, and device logins point
	// people at its /activate page.
	PublicURL string
	// ResendWebhookSecret verifies delivery reports; empty refuses them.
	ResendWebhookSecret string
	// ClientReleaseVersion, when set, is sent on every response so older
	// CLIs tell their users to run `lazycloud update`.
	ClientReleaseVersion string
}

// RecommendedClientHeader carries Config.ClientReleaseVersion.
const RecommendedClientHeader = "X-Lazycloud-Recommended-Client-Version"

// NewHandler serves the public API: it limits the body, authenticates the
// bearer token or session cookie, validates the request against the OpenAPI
// document and dispatches to the operation. Browser sign-in and provider
// webhooks are served beside it.
func NewHandler(owners Owners, cfg Config, logger *slog.Logger) (http.Handler, error) {
	s, ops, err := newServer(owners, cfg, logger, requireScheme)
	if err != nil {
		return nil, err
	}
	mux := http.NewServeMux()
	mux.HandleFunc("GET "+signInStartPath, s.startSignIn)
	mux.HandleFunc("GET "+identity.GitHubCallbackPath, s.completeSignIn)
	mux.HandleFunc("POST /webhooks/resend", s.receiveResendWebhook)
	mux.HandleFunc("POST /webhooks/stripe", s.receiveStripeWebhook)
	mux.Handle("/", s.authenticate(ops))
	return s.recommendClient(s.limitBody(mux)), nil
}

// NewContainerHandler serves the same operations to container API requests.
// Their context carries the container principal from WithContainer; they
// have no bearer token or session.
func NewContainerHandler(owners Owners, cfg Config, logger *slog.Logger) (http.Handler, error) {
	s, ops, err := newServer(owners, cfg, logger, requireContainerScheme)
	if err != nil {
		return nil, err
	}
	return s.limitBody(s.requireContainer(ops)), nil
}

// newServer returns the server and the validated operation handler, whose
// security requirements authenticated applies.
func newServer(owners Owners, cfg Config, logger *slog.Logger, authenticated openapi3filter.AuthenticationFunc) (*Server, http.Handler, error) {
	spec, err := GetSwagger()
	if err != nil {
		return nil, nil, fmt.Errorf("load openapi document: %w", err)
	}
	cfg.PublicURL = strings.TrimRight(cfg.PublicURL, "/")
	s := &Server{owners: owners, cfg: cfg, logger: logger}
	strict := NewStrictHandlerWithOptions(s, []StrictMiddlewareFunc{nameOperation}, StrictHTTPServerOptions{
		RequestErrorHandlerFunc: func(w http.ResponseWriter, r *http.Request, err error) {
			s.writeError(w, r, fmt.Errorf("%w: %w", errInvalidRequest, err))
		},
		ResponseErrorHandlerFunc: s.writeError,
	})
	validate := middleware.OapiRequestValidatorWithOptions(spec, &middleware.Options{
		Options: openapi3filter.Options{
			// authenticate resolves credentials first; this applies each
			// operation's security requirement, so an operation is
			// authenticated unless the document says otherwise.
			AuthenticationFunc: authenticated,
		},
		SilenceServersWarning: true,
		ErrorHandlerWithOpts: func(_ context.Context, err error, w http.ResponseWriter, r *http.Request, opts middleware.ErrorHandlerOpts) {
			var tooLarge *http.MaxBytesError
			switch {
			case errors.As(err, &tooLarge):
				s.writeError(w, r, err)
			case opts.StatusCode == http.StatusUnauthorized:
				s.writeError(w, r, identity.ErrUnauthenticated)
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
	return s, validate(handler), nil
}

// nameOperation labels the request's metrics and span with its operation.
func nameOperation(next StrictHandlerFunc, operationID string) StrictHandlerFunc {
	return func(ctx context.Context, w http.ResponseWriter, r *http.Request, request any) (any, error) {
		telemetry.SetRoute(ctx, operationID)
		return next(ctx, w, r, request)
	}
}

type principalKey struct{}

func principalFrom(ctx context.Context) (identity.Principal, bool) {
	p, ok := ctx.Value(principalKey{}).(identity.Principal)
	return p, ok
}

type containerKey struct{}

// WithContainer marks ctx as a container API request from p.
func WithContainer(ctx context.Context, p identity.ContainerPrincipal) context.Context {
	return context.WithValue(ctx, containerKey{}, p)
}

func containerFrom(ctx context.Context) (identity.ContainerPrincipal, bool) {
	p, ok := ctx.Value(containerKey{}).(identity.ContainerPrincipal)
	return p, ok
}

// requireContainerScheme lets a container principal through operations a
// bearer token may call; it holds no browser session.
func requireContainerScheme(ctx context.Context, input *openapi3filter.AuthenticationInput) error {
	if _, ok := containerFrom(ctx); ok && input.SecuritySchemeName == "bearer" {
		return nil
	}
	return identity.ErrUnauthenticated
}

// requireContainer admits only requests carrying a container principal.
func (s *Server) requireContainer(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, ok := containerFrom(r.Context()); !ok {
			s.writeError(w, r, identity.ErrUnauthenticated)
			return
		}
		next.ServeHTTP(w, r)
	})
}

// requireScheme accepts a security scheme when authenticate resolved a
// principal from that kind of credential. ctx is the request's context.
func requireScheme(ctx context.Context, input *openapi3filter.AuthenticationInput) error {
	p, ok := principalFrom(ctx)
	switch {
	case !ok:
	case input.SecuritySchemeName == "bearer" && p.Token != nil:
		return nil
	case input.SecuritySchemeName == "session" && p.Session != nil:
		return nil
	}
	return identity.ErrUnauthenticated
}

func (s *Server) limitBody(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, maxRequestBytes)
		next.ServeHTTP(w, r)
	})
}

func (s *Server) recommendClient(next http.Handler) http.Handler {
	if s.cfg.ClientReleaseVersion == "" {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set(RecommendedClientHeader, s.cfg.ClientReleaseVersion)
		next.ServeHTTP(w, r)
	})
}

// authenticate resolves the request's credential to a principal: a bearer
// token, or else the session cookie. A presented token that does not
// authenticate is refused at once; an expired session cookie is ignored, so
// the operation's security requirement decides. A cookie-authenticated
// request that changes state must come from the dashboard's origin, because
// browsers attach the cookie to requests other sites start.
func (s *Server) authenticate(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if header := r.Header.Get("Authorization"); header != "" {
			scheme, token, ok := strings.Cut(header, " ")
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
			return
		}
		cookie, err := r.Cookie(SessionCookie)
		if err != nil || cookie.Value == "" {
			next.ServeHTTP(w, r)
			return
		}
		p, err := s.owners.Identity.AuthenticateSession(r.Context(), cookie.Value)
		if errors.Is(err, identity.ErrUnauthenticated) {
			next.ServeHTTP(w, r)
			return
		}
		if err != nil {
			s.writeError(w, r, err)
			return
		}
		if !safeMethod(r.Method) && (s.cfg.PublicURL == "" || r.Header.Get("Origin") != s.cfg.PublicURL) {
			writeJSONError(w, http.StatusForbidden, apitypes.Forbidden, "a browser request must come from the dashboard")
			return
		}
		next.ServeHTTP(w, r.WithContext(context.WithValue(r.Context(), principalKey{}, p)))
	})
}

func safeMethod(method string) bool {
	return method == http.MethodGet || method == http.MethodHead || method == http.MethodOptions
}

// errInvalidRequest marks request problems found outside schema validation.
var errInvalidRequest = errors.New("invalid request")

// writeError maps an owner error to the Error schema. Unexpected errors are
// logged and reported without detail.
func (s *Server) writeError(w http.ResponseWriter, r *http.Request, err error) {
	var (
		tooMany         *execution.TooManyPendingError
		unknownTask     *execution.UnknownTaskError
		invalidSpec     *control.InvalidSpecError
		sourceMissing   *control.SourceMissingError
		tooLarge        *http.MaxBytesError
		invalidImage    *images.InvalidError
		conflict        *identity.ConflictError
		invalid         *identity.InvalidError
		roleErr         *identity.RoleError
		accountErr      *identity.AccountError
		secretMissing   *secrets.NotFoundError
		secretExists    *secrets.ExistsError
		secretReserved  *secrets.ReservedNameError
		storageInput    *storage.InvalidError
		storageState    *storage.ConflictError
		payment         *billing.PaymentRequiredError
		limit           *billing.LimitError
		billingConflict *billing.ConflictError
		billingInvalid  *billing.InvalidError
	)
	switch {
	case errors.Is(err, identity.ErrUnauthenticated):
		writeJSONError(w, http.StatusUnauthorized, apitypes.Unauthenticated, "a valid bearer token or session is required")
	case errors.Is(err, identity.ErrForbidden):
		writeJSONError(w, http.StatusForbidden, apitypes.Forbidden, "the token cannot reach this workspace")
	case errors.Is(err, identity.ErrAdminRequired):
		writeJSONError(w, http.StatusForbidden, apitypes.Forbidden, identity.ErrAdminRequired.Error())
	case errors.As(err, &roleErr):
		writeJSONError(w, http.StatusForbidden, apitypes.Forbidden, roleErr.Error())
	case errors.As(err, &accountErr):
		writeJSONError(w, http.StatusForbidden, apitypes.Forbidden, accountErr.Error())
	case errors.As(err, &conflict):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, conflict.Error())
	case errors.As(err, &invalid):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, invalid.Error())
	case errors.Is(err, identity.ErrExists):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, "a workspace with that name already exists")
	case errors.Is(err, identity.ErrNotFound), errors.Is(err, control.ErrNotFound), errors.Is(err, execution.ErrNotFound),
		errors.Is(err, images.ErrNotFound), errors.Is(err, observability.ErrNotFound):
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "not found")
	case errors.Is(err, execution.ErrNoResult):
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "the task finished without a result")
	case errors.Is(err, control.ErrVersionNotFound):
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "the deployment has no such version")
	case errors.As(err, &unknownTask):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, unknownTask.Error())
	case errors.Is(err, control.ErrInvalidCursor), errors.Is(err, execution.ErrInvalidCursor):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, "the cursor is not from this listing")
	case errors.Is(err, execution.ErrInvalidFilter), errors.Is(err, execution.ErrInvalidSubmit),
		errors.Is(err, observability.ErrInvalidRange):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, err.Error())
	case errors.As(err, &invalidSpec):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, invalidSpec.Error())
	case errors.As(err, &sourceMissing):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, sourceMissing.Error())
	case errors.Is(err, storage.ErrInvalidDigest), errors.Is(err, errInvalidRequest), errors.Is(err, control.ErrNothingToDeploy):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, err.Error())
	case errors.As(err, &invalidImage):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, err.Error())
	case errors.Is(err, images.ErrUnsupported):
		writeJSONError(w, http.StatusBadRequest, apitypes.Unsupported, err.Error())
	case errors.Is(err, images.ErrNotReady):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, err.Error())
	case errors.Is(err, observability.ErrTooManyStreams):
		writeJSONError(w, http.StatusTooManyRequests, apitypes.Unavailable, err.Error())
	case errors.Is(err, observability.ErrTooManySubscribers):
		writeJSONError(w, http.StatusServiceUnavailable, apitypes.Unavailable, err.Error())
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
	case errors.As(err, &secretMissing):
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, secretMissing.Error())
	case errors.As(err, &secretExists):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, secretExists.Error())
	case errors.As(err, &secretReserved):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, secretReserved.Error())
	case errors.As(err, &payment):
		writeJSONError(w, http.StatusPaymentRequired, apitypes.PaymentRequired, payment.Error())
	case errors.As(err, &limit):
		writeJSONError(w, http.StatusConflict, apitypes.LimitReached, limit.Error())
	case errors.As(err, &billingConflict):
		writeJSONError(w, http.StatusConflict, apitypes.Conflict, billingConflict.Error())
	case errors.As(err, &billingInvalid):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, billingInvalid.Error())
	case errors.Is(err, billing.ErrNotFound):
		writeJSONError(w, http.StatusNotFound, apitypes.NotFound, "not found")
	case errors.Is(err, billing.ErrPaymentsUnavailable):
		writeJSONError(w, http.StatusServiceUnavailable, apitypes.Unavailable, billing.ErrPaymentsUnavailable.Error())
	case errors.Is(err, secrets.ErrInvalidCursor), errors.Is(err, schedules.ErrInvalidCursor):
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, "the cursor is not from a previous page")
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
