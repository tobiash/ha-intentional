package intentionalctl

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type Client struct {
	baseURL string
	token   string
	http    *http.Client
}

func NewClient(baseURL string, token string, timeout time.Duration) (*Client, error) {
	baseURL = strings.TrimRight(strings.TrimSpace(baseURL), "/")
	if baseURL == "" {
		return nil, fmt.Errorf("missing Home Assistant URL; set HASS_URL or pass --url")
	}
	if strings.TrimSpace(token) == "" {
		return nil, fmt.Errorf("missing Home Assistant token; set HASS_TOKEN or pass --token")
	}
	return &Client{
		baseURL: baseURL,
		token:   token,
		http:    &http.Client{Timeout: timeout},
	}, nil
}

func (c *Client) Get(ctx context.Context, path string) (json.RawMessage, error) {
	return c.do(ctx, http.MethodGet, path, nil)
}

func (c *Client) Post(ctx context.Context, path string, payload any) (json.RawMessage, error) {
	return c.do(ctx, http.MethodPost, path, payload)
}

func (c *Client) Put(ctx context.Context, path string, payload any) (json.RawMessage, error) {
	return c.do(ctx, http.MethodPut, path, payload)
}

func (c *Client) Patch(ctx context.Context, path string, payload any) (json.RawMessage, error) {
	return c.do(ctx, http.MethodPatch, path, payload)
}

func (c *Client) Delete(ctx context.Context, path string, payload any) (json.RawMessage, error) {
	return c.do(ctx, http.MethodDelete, path, payload)
}

func (c *Client) do(ctx context.Context, method string, path string, payload any) (json.RawMessage, error) {
	var body io.Reader
	if payload != nil {
		encoded, err := json.Marshal(payload)
		if err != nil {
			return nil, fmt.Errorf("encode request: %w", err)
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, body)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+c.token)
	request.Header.Set("Accept", "application/json")
	if payload != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	response, err := c.http.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()

	data, err := io.ReadAll(response.Body)
	if err != nil {
		return nil, err
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return nil, fmt.Errorf("%s %s failed: HTTP %d: %s", method, path, response.StatusCode, strings.TrimSpace(string(data)))
	}
	if len(bytes.TrimSpace(data)) == 0 {
		return json.RawMessage(`null`), nil
	}
	return json.RawMessage(data), nil
}
