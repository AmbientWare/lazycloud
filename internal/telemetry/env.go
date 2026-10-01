package telemetry

import (
	"fmt"
	"os"
	"strconv"
)

// ConfigFromEnv reads the telemetry settings every binary shares:
//
//	LAZYCLOUD_OTLP_ENDPOINT       OTLP/gRPC collector host:port; unset turns tracing off
//	LAZYCLOUD_OTLP_INSECURE       "true" sends spans without TLS
//	LAZYCLOUD_TRACE_SAMPLE_RATIO  share of new traces recorded, 0 to 1 (default 1)
//	LAZYCLOUD_METRICS_ADDR        address of the /metrics listener; unset serves none
func ConfigFromEnv(service, version string) (Config, error) {
	cfg := Config{
		Service: service, Version: version,
		OTLPEndpoint: os.Getenv("LAZYCLOUD_OTLP_ENDPOINT"),
		OTLPInsecure: os.Getenv("LAZYCLOUD_OTLP_INSECURE") == "true",
		MetricsAddr:  os.Getenv("LAZYCLOUD_METRICS_ADDR"),
		SampleRatio:  1,
	}
	if raw := os.Getenv("LAZYCLOUD_TRACE_SAMPLE_RATIO"); raw != "" {
		ratio, err := strconv.ParseFloat(raw, 64)
		if err != nil || ratio < 0 || ratio > 1 {
			return Config{}, fmt.Errorf("LAZYCLOUD_TRACE_SAMPLE_RATIO %q is not a number from 0 to 1", raw)
		}
		cfg.SampleRatio = ratio
	}
	return cfg, nil
}

// LogFormatFromEnv is LAZYCLOUD_LOG_FORMAT, text or json, or fallback.
func LogFormatFromEnv(fallback LogFormat) (LogFormat, error) {
	switch raw := os.Getenv("LAZYCLOUD_LOG_FORMAT"); LogFormat(raw) {
	case "":
		return fallback, nil
	case LogText, LogJSON:
		return LogFormat(raw), nil
	default:
		return "", fmt.Errorf("LAZYCLOUD_LOG_FORMAT %q is neither text nor json", raw)
	}
}
