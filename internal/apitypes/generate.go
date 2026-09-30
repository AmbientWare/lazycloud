// Package apitypes holds the public API models generated from
// contracts/openapi.yaml. Owners use them where the wire meaning is the domain
// meaning, such as a release's function spec.
package apitypes

//go:generate go tool oapi-codegen -config oapi-codegen.yaml ../../contracts/openapi.yaml
