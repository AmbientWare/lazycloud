// Package api serves the public HTTP API from contracts/openapi.yaml. Handlers
// validate, authorize, invoke an owner and map typed results to responses.
package api

//go:generate go tool oapi-codegen -config oapi-codegen.yaml ../../contracts/openapi.yaml
