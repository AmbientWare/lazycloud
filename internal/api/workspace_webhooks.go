package api

import (
	"context"
)

// ListWebhooks returns a page of the workspace's webhooks.
func (s *Server) ListWebhooks(ctx context.Context, req ListWebhooksRequestObject) (ListWebhooksResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	cursor, limit := pageArgs(req.Params.Cursor, req.Params.Limit)
	page, err := s.owners.Webhooks.List(ctx, ws.ID, cursor, limit)
	if err != nil {
		return nil, err
	}
	return ListWebhooks200JSONResponse(page), nil
}

// CreateWebhook registers a webhook.
func (s *Server) CreateWebhook(ctx context.Context, req CreateWebhookRequestObject) (CreateWebhookResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	webhook, err := s.owners.Webhooks.Create(ctx, ws.ID, *req.Body)
	if err != nil {
		return nil, err
	}
	return CreateWebhook201JSONResponse(webhook), nil
}

// GetWebhook returns one webhook.
func (s *Server) GetWebhook(ctx context.Context, req GetWebhookRequestObject) (GetWebhookResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	webhook, err := s.owners.Webhooks.Get(ctx, ws.ID, req.Webhook)
	if err != nil {
		return nil, err
	}
	return GetWebhook200JSONResponse(webhook), nil
}

// UpdateWebhook changes the fields the body names.
func (s *Server) UpdateWebhook(ctx context.Context, req UpdateWebhookRequestObject) (UpdateWebhookResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	webhook, err := s.owners.Webhooks.Update(ctx, ws.ID, req.Webhook, *req.Body)
	if err != nil {
		return nil, err
	}
	return UpdateWebhook200JSONResponse(webhook), nil
}

// DeleteWebhook deletes a webhook.
func (s *Server) DeleteWebhook(ctx context.Context, req DeleteWebhookRequestObject) (DeleteWebhookResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	if err := s.owners.Webhooks.Delete(ctx, ws.ID, req.Webhook); err != nil {
		return nil, err
	}
	return DeleteWebhook204Response{}, nil
}

// TestWebhook queues a webhook.test event for the webhook.
func (s *Server) TestWebhook(ctx context.Context, req TestWebhookRequestObject) (TestWebhookResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	delivery, err := s.owners.Webhooks.Test(ctx, ws.ID, req.Webhook)
	if err != nil {
		return nil, err
	}
	return TestWebhook202JSONResponse(delivery), nil
}

// ListWebhookDeliveries returns a page of the webhook's deliveries.
func (s *Server) ListWebhookDeliveries(ctx context.Context, req ListWebhookDeliveriesRequestObject) (ListWebhookDeliveriesResponseObject, error) {
	ws, err := s.workspace(ctx, req.Workspace)
	if err != nil {
		return nil, err
	}
	cursor, limit := pageArgs(req.Params.Cursor, req.Params.Limit)
	page, err := s.owners.Webhooks.Deliveries(ctx, ws.ID, req.Webhook, cursor, limit)
	if err != nil {
		return nil, err
	}
	return ListWebhookDeliveries200JSONResponse(page), nil
}
