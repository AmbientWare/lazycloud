package api

import (
	"errors"
	"net/http"
	"net/url"
	"time"

	"github.com/AmbientWare/lazycloud/internal/identity"
)

// Cookies. The __Host- prefix makes browsers refuse them unless they are
// Secure, host-only and for /, so a sibling subdomain cannot plant one.
// Browsers treat http://localhost as secure, so local development works.
const (
	SessionCookie = "__Host-lazycloud_session"
	SignInCookie  = "__Host-lazycloud_sign_in"
)

// Browser sign-in routes. They answer with redirects because the caller is
// a browser navigating; failures land on the dashboard's sign-in page with
// a reason from a closed set.
const (
	signInStartPath   = "/auth/github/start"
	signInPagePath    = "/signin"
	signInDefaultPath = "/dashboard"
)

// startSignIn sets the sign-in cookie and sends the browser to GitHub. A
// browser whose session cookie is still live goes straight to its return
// path, so a sign-in link never makes a signed-in person sign in again.
func (s *Server) startSignIn(w http.ResponseWriter, r *http.Request) {
	returnTo := r.URL.Query().Get("return_to")
	if cookie, err := r.Cookie(SessionCookie); err == nil && cookie.Value != "" {
		if _, err := s.owners.Identity.AuthenticateSession(r.Context(), cookie.Value); err == nil {
			if identity.CheckReturnPath(returnTo) != nil {
				signInFailed(w, r, "invalid_return_to")
				return
			}
			if returnTo == "" {
				returnTo = signInDefaultPath
			}
			http.Redirect(w, r, returnTo, http.StatusSeeOther) //nolint:gosec // CheckReturnPath allows only a path on this origin.
			return
		}
	}
	start, err := s.owners.Identity.BeginSignIn(returnTo)
	switch {
	case errors.Is(err, identity.ErrReturnPath):
		signInFailed(w, r, "invalid_return_to")
		return
	case errors.Is(err, identity.ErrSignInUnavailable):
		s.logger.ErrorContext(r.Context(), "github sign-in is not configured: set LAZYCLOUD_GITHUB_CLIENT_ID and LAZYCLOUD_GITHUB_CLIENT_SECRET")
		signInFailed(w, r, "provider_unavailable")
		return
	case err != nil:
		s.logger.ErrorContext(r.Context(), "start sign-in", "error", err)
		signInFailed(w, r, "provider_unavailable")
		return
	}
	http.SetCookie(w, &http.Cookie{
		Name: SignInCookie, Value: start.Cookie, Path: "/", MaxAge: int(identity.SignInTTL / time.Second),
		HttpOnly: true, Secure: true, SameSite: http.SameSiteLaxMode,
	})
	http.Redirect(w, r, start.AuthorizeURL, http.StatusFound)
}

// completeSignIn redeems GitHub's code, sets the session cookie and lands
// the browser on its return path. GitHub's own error text is never shown.
func (s *Server) completeSignIn(w http.ResponseWriter, r *http.Request) {
	query := r.URL.Query()
	http.SetCookie(w, expiredCookie(SignInCookie))
	if query.Get("error") != "" {
		signInFailed(w, r, "access_denied")
		return
	}
	cookie, err := r.Cookie(SignInCookie)
	if err != nil {
		signInFailed(w, r, "invalid_state")
		return
	}
	session, err := s.owners.Identity.CompleteSignIn(r.Context(), cookie.Value, query.Get("state"), query.Get("code"))
	var ghErr *identity.GitHubError
	switch {
	case errors.Is(err, identity.ErrSignInState):
		signInFailed(w, r, "invalid_state")
		return
	case errors.Is(err, identity.ErrAccountDisabled):
		signInFailed(w, r, "account_disabled")
		return
	case errors.As(err, &ghErr) && ghErr.Refused:
		s.logger.WarnContext(r.Context(), "github refused sign-in", "error", err)
		signInFailed(w, r, "provider_refused")
		return
	case err != nil:
		s.logger.ErrorContext(r.Context(), "complete sign-in", "error", err)
		signInFailed(w, r, "provider_unavailable")
		return
	}
	http.SetCookie(w, &http.Cookie{
		Name: SessionCookie, Value: session.Token, Path: "/", Expires: session.ExpiresAt,
		MaxAge: int(time.Until(session.ExpiresAt) / time.Second), HttpOnly: true, Secure: true,
		SameSite: http.SameSiteLaxMode,
	})
	target := session.ReturnTo
	if target == "" {
		target = signInDefaultPath
	}
	http.Redirect(w, r, target, http.StatusSeeOther)
}

func signInFailed(w http.ResponseWriter, r *http.Request, reason string) {
	http.Redirect(w, r, signInPagePath+"?error="+url.QueryEscape(reason), http.StatusSeeOther)
}

func expiredCookie(name string) *http.Cookie {
	return &http.Cookie{
		Name: name, Value: "", Path: "/", MaxAge: -1, HttpOnly: true, Secure: true, SameSite: http.SameSiteLaxMode,
	}
}
