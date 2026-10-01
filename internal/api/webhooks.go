package api

import (
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/AmbientWare/lazycloud/internal/apitypes"
	"github.com/AmbientWare/lazycloud/internal/notifications"
)

// maxWebhookBytes bounds a provider delivery.
const maxWebhookBytes = 1 << 20

// receiveResendWebhook records an email delivery report. The caller is
// Resend, which holds no token: the Svix signature stands in for one. Event
// types delivery records do not follow are acknowledged so Resend does not
// retry them.
func (s *Server) receiveResendWebhook(w http.ResponseWriter, r *http.Request) {
	if s.cfg.ResendWebhookSecret == "" {
		s.logger.ErrorContext(r.Context(), "a resend delivery arrived but LAZYCLOUD_RESEND_WEBHOOK_SECRET is not set")
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, "email webhooks are not configured")
		return
	}
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, maxWebhookBytes))
	if err != nil {
		writeJSONError(w, http.StatusRequestEntityTooLarge, apitypes.PayloadTooLarge, "the delivery is too large")
		return
	}
	err = notifications.VerifyWebhook(s.cfg.ResendWebhookSecret,
		r.Header.Get(notifications.WebhookIDHeader), r.Header.Get(notifications.WebhookTimestampHeader),
		r.Header.Get(notifications.WebhookSignatureHeader), body, time.Now())
	if errors.Is(err, notifications.ErrBadSignature) {
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, err.Error())
		return
	}
	if err != nil {
		s.writeError(w, r, err)
		return
	}
	event, err := notifications.ParseWebhook(body)
	if err != nil {
		writeJSONError(w, http.StatusBadRequest, apitypes.InvalidRequest, "the delivery is not a readable Resend event")
		return
	}
	if event != nil {
		if _, err := s.owners.Notifications.RecordDelivery(r.Context(), *event); err != nil {
			s.writeError(w, r, err)
			return
		}
	}
	w.WriteHeader(http.StatusNoContent)
}
