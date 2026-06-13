package intentionalctl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestHealthUsesEnvAndPrintsJSON(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/api/intentional/health" {
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer token-1" {
			t.Fatalf("unexpected auth header %q", got)
		}
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}))
	defer server.Close()

	var stdout strings.Builder
	var stderr strings.Builder
	exit := Run(context.Background(), []string{"health"}, &stdout, &stderr, mapEnv(map[string]string{
		"HASS_URL":   server.URL,
		"HASS_TOKEN": "token-1",
	}), "test")
	if exit != 0 {
		t.Fatalf("exit=%d stderr=%s", exit, stderr.String())
	}
	var body map[string]string
	if err := json.Unmarshal([]byte(stdout.String()), &body); err != nil {
		t.Fatalf("invalid JSON output: %v", err)
	}
	if body["status"] != "ok" {
		t.Fatalf("unexpected body %#v", body)
	}
}

func TestRulesSaveSendsGenerationAndContents(t *testing.T) {
	tmp := t.TempDir()
	ruleFile := filepath.Join(tmp, "rule.yaml")
	if err := os.WriteFile(ruleFile, []byte("- id: test\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut || r.URL.Path != "/api/intentional/rules/document" {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		var payload map[string]string
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload["contents"] != "- id: test\n" || payload["expected_generation"] != "gen-1" {
			t.Fatalf("unexpected payload %#v", payload)
		}
		_, _ = w.Write([]byte(`{"status":"saved","generation":"gen-2"}`))
	}))
	defer server.Close()

	var stdout strings.Builder
	var stderr strings.Builder
	exit := Run(context.Background(), []string{"--url", server.URL, "--token", "token", "rules-save", "--file", ruleFile, "--expected-generation", "gen-1"}, &stdout, &stderr, mapEnv(nil), "test")
	if exit != 0 {
		t.Fatalf("exit=%d stderr=%s", exit, stderr.String())
	}
	if !strings.Contains(stdout.String(), "gen-2") {
		t.Fatalf("unexpected stdout %s", stdout.String())
	}
}

func TestEnvFileFallback(t *testing.T) {
	tmp := t.TempDir()
	envFile := filepath.Join(tmp, ".ha-env")
	if err := os.WriteFile(envFile, []byte("HASS_URL=https://example.invalid\nHASS_TOKEN=secret\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	config, err := loadConfig(mapEnv(nil), "", "", envFile, 0, false)
	if err != nil {
		t.Fatal(err)
	}
	if config.URL != "https://example.invalid" || config.Token != "secret" {
		t.Fatalf("unexpected config %#v", config)
	}
}

func TestSimulateAcceptsTimelineObjectFile(t *testing.T) {
	tmp := t.TempDir()
	ruleFile := filepath.Join(tmp, "rule.yaml")
	if err := os.WriteFile(ruleFile, []byte("- id: test\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	timelineFile := filepath.Join(tmp, "timeline.json")
	if err := os.WriteFile(timelineFile, []byte(`{"timeline":[{"states":{"binary_sensor.test.state":"on"}}]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/intentional/simulate" {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		var payload struct {
			Contents string           `json:"contents"`
			Timeline []map[string]any `json:"timeline"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload.Contents != "- id: test\n" || len(payload.Timeline) != 1 {
			t.Fatalf("unexpected payload %#v", payload)
		}
		_, _ = w.Write([]byte(`{"valid":true,"steps":[],"errors":[]}`))
	}))
	defer server.Close()

	var stdout strings.Builder
	var stderr strings.Builder
	exit := Run(context.Background(), []string{"--url", server.URL, "--token", "token", "simulate", "--file", ruleFile, "--timeline", timelineFile}, &stdout, &stderr, mapEnv(nil), "test")
	if exit != 0 {
		t.Fatalf("exit=%d stderr=%s", exit, stderr.String())
	}
}

func mapEnv(values map[string]string) envGetter {
	return func(key string) string {
		return values[key]
	}
}
