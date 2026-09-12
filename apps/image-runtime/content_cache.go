package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	clipstorage "github.com/beam-cloud/clip/pkg/storage"
)

const cacheReadChunk = 1024 * 1024

type cacheConnection struct {
	Endpoint string `json:"endpoint"`
	Token    string `json:"token"`
}

type httpContentCache struct {
	connection cacheConnection
	client     *http.Client
}

func newContentCache(connection cacheConnection) (*httpContentCache, error) {
	endpoint, err := url.Parse(connection.Endpoint)
	if err != nil || endpoint.Host == "" || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" || connection.Token == "" {
		return nil, errors.New("image content cache requires an HTTP endpoint and service token")
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.MaxIdleConnsPerHost = 32
	transport.DialContext = (&net.Dialer{Timeout: 500 * time.Millisecond, KeepAlive: 30 * time.Second}).DialContext
	transport.ResponseHeaderTimeout = 2 * time.Second
	return &httpContentCache{connection: connection, client: &http.Client{
		Transport:     transport,
		Timeout:       120 * time.Second,
		CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse },
	}}, nil
}

func normalizedCacheHash(value string) (string, error) {
	value = strings.TrimPrefix(value, "sha256:")
	decoded, err := hex.DecodeString(value)
	if err != nil || len(decoded) != sha256.Size || strings.ToLower(value) != value {
		return "", errors.New("invalid image content hash")
	}
	return value, nil
}

func (c *httpContentCache) request(method, path string, body io.Reader, size int64, headers map[string]string) (*http.Response, error) {
	req, err := http.NewRequestWithContext(context.Background(), method, strings.TrimRight(c.connection.Endpoint, "/")+path, body)
	if err != nil {
		return nil, errors.New("invalid image cache request")
	}
	req.ContentLength = size
	req.Header.Set("Authorization", "Bearer "+c.connection.Token)
	for key, value := range headers {
		req.Header.Set(key, value)
	}
	response, err := c.client.Do(req)
	if err != nil {
		slog.Error("image content cache request failed", "method", method)
		return nil, clipstorage.ErrContentCacheUnavailable
	}
	return response, nil
}

func (c *httpContentCache) ContentExists(hash string, _ struct{ RoutingKey string }) (bool, error) {
	hash, err := normalizedCacheHash(hash)
	if err != nil {
		return false, err
	}
	response, err := c.request(http.MethodHead, "/content/"+hash, nil, 0, nil)
	if err != nil {
		return false, err
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusNotFound {
		return false, nil
	}
	if response.StatusCode != http.StatusOK || response.Header.Get("X-Content-Hash") != hash {
		return false, fmt.Errorf("image cache HEAD failed: HTTP %d", response.StatusCode)
	}
	return true, nil
}

func (c *httpContentCache) GetContent(hash string, offset, length int64, opts struct{ RoutingKey string }) ([]byte, error) {
	if length < 0 || length > 32*cacheReadChunk {
		return nil, errors.New("image cache read exceeds 32 MiB")
	}
	data := make([]byte, int(length))
	_, err := c.ReadContentInto(hash, offset, data, opts)
	return data, err
}

func (c *httpContentCache) ReadContentInto(hash string, offset int64, dest []byte, _ struct{ RoutingKey string }) (int64, error) {
	hash, err := normalizedCacheHash(hash)
	if err != nil {
		return 0, err
	}
	if offset < 0 || offset+int64(len(dest)) < offset {
		return 0, errors.New("invalid image cache range")
	}
	var read int64
	for len(dest) > 0 {
		chunk := dest[:min(len(dest), cacheReadChunk)]
		end := offset + int64(len(chunk)) - 1
		response, err := c.request(http.MethodGet, "/content/"+hash, nil, 0, map[string]string{"Range": fmt.Sprintf("bytes=%d-%d", offset, end)})
		if err != nil {
			return read, err
		}
		if response.StatusCode == http.StatusNotFound {
			response.Body.Close()
			return read, clipstorage.ErrContentCacheMiss
		}
		expectedRange := fmt.Sprintf("bytes %d-%d/", offset, end)
		if response.StatusCode != http.StatusPartialContent || response.Header.Get("X-Content-Hash") != hash || response.ContentLength != int64(len(chunk)) || !strings.HasPrefix(response.Header.Get("Content-Range"), expectedRange) {
			response.Body.Close()
			slog.Error("image content cache returned an invalid range", "status", response.StatusCode)
			return read, errors.New("image cache response identity or range is invalid")
		}
		n, err := io.ReadFull(response.Body, chunk)
		response.Body.Close()
		read += int64(n)
		if err != nil {
			return read, errors.New("image cache response is incomplete")
		}
		offset += int64(n)
		dest = dest[n:]
	}
	return read, nil
}

func (c *httpContentCache) StoreContent(chunks chan []byte, hash string, opts struct{ RoutingKey string }) (string, error) {
	reader, writer := io.Pipe()
	done := make(chan struct{})
	go func() {
		defer close(done)
		defer writer.Close()
		for chunk := range chunks {
			if _, err := writer.Write(chunk); err != nil {
				// A producer must be released even when the server refuses an upload.
				for range chunks {
				}
				return
			}
		}
	}()
	defer func() { reader.Close(); <-done }()
	return c.store(reader, hash, -1)
}

func (c *httpContentCache) StoreContentFromLocalPath(path, hash string, _ struct{ RoutingKey string }) (string, error) {
	file, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return "", err
	}
	return c.store(file, hash, info.Size())
}

func (c *httpContentCache) store(body io.Reader, hash string, size int64) (string, error) {
	hash, err := normalizedCacheHash(hash)
	if err != nil {
		return "", err
	}
	query := url.Values{"expected_hash": {hash}}
	response, err := c.request(http.MethodPut, "/content?"+query.Encode(), body, size, nil)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	var result struct {
		Status string `json:"status"`
		Hash   string `json:"content_hash"`
		Size   int64  `json:"size_bytes"`
	}
	if err := json.NewDecoder(io.LimitReader(response.Body, 64*1024)).Decode(&result); err != nil {
		return "", errors.New("image cache store response is invalid")
	}
	if response.StatusCode != http.StatusCreated || (result.Status != "stored" && result.Status != "already-present") || result.Hash != hash || result.Size < 0 || (size >= 0 && result.Size != size) {
		return "", errors.New("image cache store failed: HTTP " + strconv.Itoa(response.StatusCode))
	}
	return hash, nil
}
