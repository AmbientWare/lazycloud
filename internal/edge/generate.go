// Package edge owns HTTP workload traffic: the routes deployed workloads and
// custom domains answer on, admission and authentication of each request,
// the in-flight demand edges publish for autoscaling, and the data
// connections agents open to carry requests to containers. Execution decides
// how many containers run; the edge decides which one serves a request.
package edge

//go:generate go tool sqlc generate
