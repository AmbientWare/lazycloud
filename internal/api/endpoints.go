package api

import (
	"context"
	"errors"
)

// errNotYetServed marks endpoint operations this branch has not wired yet.
var errNotYetServed = errors.New("not yet served")

func (s *Server) GetEndpoint(context.Context, GetEndpointRequestObject) (GetEndpointResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) GetAsgi(context.Context, GetAsgiRequestObject) (GetAsgiResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) InvokeFunction(context.Context, InvokeFunctionRequestObject) (InvokeFunctionResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) InvokeFunctionVersion(context.Context, InvokeFunctionVersionRequestObject) (InvokeFunctionVersionResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) ListDomains(context.Context, ListDomainsRequestObject) (ListDomainsResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) RegisterDomain(context.Context, RegisterDomainRequestObject) (RegisterDomainResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) GetDomain(context.Context, GetDomainRequestObject) (GetDomainResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) RemoveDomain(context.Context, RemoveDomainRequestObject) (RemoveDomainResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) CreatePreview(context.Context, CreatePreviewRequestObject) (CreatePreviewResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) GetPreview(context.Context, GetPreviewRequestObject) (GetPreviewResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) StopPreview(context.Context, StopPreviewRequestObject) (StopPreviewResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) SyncPreviewFiles(context.Context, SyncPreviewFilesRequestObject) (SyncPreviewFilesResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) StreamPreviewOutput(context.Context, StreamPreviewOutputRequestObject) (StreamPreviewOutputResponseObject, error) {
	return nil, errNotYetServed
}

func (s *Server) SubmitPreviewTasks(context.Context, SubmitPreviewTasksRequestObject) (SubmitPreviewTasksResponseObject, error) {
	return nil, errNotYetServed
}
