package main

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	smithyhttp "github.com/aws/smithy-go/transport/http"
)

// storeConfig is STORE.json: the workspace bucket credentials the worker holds.
type storeConfig struct {
	EndpointURL    string `json:"endpoint_url"`
	Region         string `json:"region"`
	Bucket         string `json:"bucket"`
	AccessKey      string `json:"access_key"`
	SecretKey      string `json:"secret_key"`
	SessionToken   string `json:"session_token"`
	ForcePathStyle bool   `json:"force_path_style"`
}

type objectStore struct {
	client *s3.Client
	bucket string
}

func openStore(path string) (*objectStore, error) {
	var config storeConfig
	if err := readJSONFile(path, &config); err != nil {
		return nil, err
	}
	for _, field := range [][2]string{
		{"region", config.Region}, {"bucket", config.Bucket},
		{"access_key", config.AccessKey}, {"secret_key", config.SecretKey},
	} {
		if field[1] == "" {
			return nil, fmt.Errorf("%s has no %s", path, field[0])
		}
	}
	options := s3.Options{
		Region: config.Region,
		Credentials: credentials.NewStaticCredentialsProvider(
			config.AccessKey, config.SecretKey, config.SessionToken),
		UsePathStyle: config.ForcePathStyle,
		// Checksums only where S3 requires them: S3-compatible stores reject the
		// streaming trailers the SDK otherwise adds to every upload.
		RequestChecksumCalculation: aws.RequestChecksumCalculationWhenRequired,
		ResponseChecksumValidation: aws.ResponseChecksumValidationWhenRequired,
		RetryMaxAttempts:           5,
	}
	if config.EndpointURL != "" {
		options.BaseEndpoint = aws.String(config.EndpointURL)
	}
	return &objectStore{client: s3.New(options), bucket: config.Bucket}, nil
}

func (s *objectStore) exists(ctx context.Context, key string) (bool, error) {
	_, err := s.client.HeadObject(ctx, &s3.HeadObjectInput{Bucket: &s.bucket, Key: &key})
	if err == nil {
		return true, nil
	}
	var response *smithyhttp.ResponseError
	if errors.As(err, &response) && response.HTTPStatusCode() == http.StatusNotFound {
		return false, nil
	}
	return false, fmt.Errorf("head s3://%s/%s: %w", s.bucket, key, err)
}

func (s *objectStore) put(ctx context.Context, key string, body []byte, contentType string) error {
	_, err := s.client.PutObject(ctx, &s3.PutObjectInput{
		Bucket:        &s.bucket,
		Key:           &key,
		Body:          bytes.NewReader(body),
		ContentLength: aws.Int64(int64(len(body))),
		ContentType:   aws.String(contentType),
	})
	if err != nil {
		return fmt.Errorf("put s3://%s/%s: %w", s.bucket, key, err)
	}
	return nil
}

func (s *objectStore) get(ctx context.Context, key string, limit int64) ([]byte, error) {
	output, err := s.client.GetObject(ctx, &s3.GetObjectInput{Bucket: &s.bucket, Key: &key})
	if err != nil {
		return nil, fmt.Errorf("get s3://%s/%s: %w", s.bucket, key, err)
	}
	defer output.Body.Close()
	data, err := io.ReadAll(io.LimitReader(output.Body, limit+1))
	if err != nil {
		return nil, fmt.Errorf("read s3://%s/%s: %w", s.bucket, key, err)
	}
	if int64(len(data)) > limit {
		return nil, fmt.Errorf("s3://%s/%s is larger than %d bytes", s.bucket, key, limit)
	}
	return data, nil
}
