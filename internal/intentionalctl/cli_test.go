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

func TestPreviewSendsOptionalContentsAndStates(t *testing.T) {
	tmp := t.TempDir()
	ruleFile := filepath.Join(tmp, "rule.yaml")
	if err := os.WriteFile(ruleFile, []byte("- id: test\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/intentional/preview" {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		var payload struct {
			Contents       string            `json:"contents"`
			StateOverrides map[string]string `json:"state_overrides"`
		}
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload.Contents != "- id: test\n" || payload.StateOverrides["media_player.tv.state"] != "playing" {
			t.Fatalf("unexpected payload %#v", payload)
		}
		_, _ = w.Write([]byte(`{"valid":true,"preview":[],"errors":[]}`))
	}))
	defer server.Close()

	var stdout strings.Builder
	var stderr strings.Builder
	exit := Run(context.Background(), []string{"--url", server.URL, "--token", "token", "preview", "--file", ruleFile, "--state", "media_player.tv.state=playing"}, &stdout, &stderr, mapEnv(nil), "test")
	if exit != 0 {
		t.Fatalf("exit=%d stderr=%s", exit, stderr.String())
	}
}

func TestCardUsesTargetQuery(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.URL.Path != "/api/intentional/card" {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		if r.URL.Query().Get("target") != "light.sofa" {
			t.Fatalf("unexpected query %s", r.URL.RawQuery)
		}
		_, _ = w.Write([]byte(`{"targets":[],"count":0}`))
	}))
	defer server.Close()

	var stdout strings.Builder
	var stderr strings.Builder
	exit := Run(context.Background(), []string{"--url", server.URL, "--token", "token", "card", "--target", "light.sofa"}, &stdout, &stderr, mapEnv(nil), "test")
	if exit != 0 {
		t.Fatalf("exit=%d stderr=%s", exit, stderr.String())
	}
}

func TestReplayPostsHistoryFile(t *testing.T) {
	tmp := t.TempDir()
	historyFile := filepath.Join(tmp, "history.json")
	if err := os.WriteFile(historyFile, []byte(`[[{"entity_id":"binary_sensor.test","state":"on"}]]`), 0o600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/intentional/replay" {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		var payload map[string]any
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if _, ok := payload["history"]; !ok {
			t.Fatalf("missing history payload %#v", payload)
		}
		_, _ = w.Write([]byte(`{"valid":true,"steps":[],"errors":[]}`))
	}))
	defer server.Close()

	var stdout strings.Builder
	var stderr strings.Builder
	exit := Run(context.Background(), []string{"--url", server.URL, "--token", "token", "replay", "--history", historyFile}, &stdout, &stderr, mapEnv(nil), "test")
	if exit != 0 {
		t.Fatalf("exit=%d stderr=%s", exit, stderr.String())
	}
}

func TestMigrateHAProposeCanPrintYAML(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost || r.URL.Path != "/api/intentional/migrate-ha/propose" {
			t.Fatalf("unexpected request %s %s", r.Method, r.URL.Path)
		}
		var payload map[string]string
		if err := json.NewDecoder(r.Body).Decode(&payload); err != nil {
			t.Fatal(err)
		}
		if payload["entity_id"] != "automation.hall" {
			t.Fatalf("unexpected payload %#v", payload)
		}
		_, _ = w.Write([]byte(`{"supported":true,"yaml":"- id: hall\n"}`))
	}))
	defer server.Close()

	var stdout strings.Builder
	var stderr strings.Builder
	exit := Run(context.Background(), []string{"--url", server.URL, "--token", "token", "migrate-ha", "propose", "automation.hall", "--output", "yaml"}, &stdout, &stderr, mapEnv(nil), "test")
	if exit != 0 || stdout.String() != "- id: hall\n" {
		t.Fatalf("exit=%d stdout=%q stderr=%s", exit, stdout.String(), stderr.String())
	}
}

func TestMigrateHAUnsupportedYAMLReturnsFailureWithDiagnostic(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"supported":false,"yaml":"","diagnostics":[{"message":"attribute trigger unsupported"}]}`))
	}))
	defer server.Close()
	var stdout strings.Builder
	var stderr strings.Builder
	exit := Run(context.Background(), []string{"--url", server.URL, "--token", "token", "migrate-ha", "propose", "automation.hall", "--output", "yaml"}, &stdout, &stderr, mapEnv(nil), "test")
	if exit == 0 || !strings.Contains(stderr.String(), "attribute trigger unsupported") || stdout.Len() != 0 {
		t.Fatalf("exit=%d stdout=%q stderr=%q", exit, stdout.String(), stderr.String())
	}
}

func TestMigrateHAInvalidInvocationsReturnUsageWithoutRequest(t *testing.T) {
	tests := []struct {
		name string
		args []string
	}{
		{name: "missing subcommand", args: []string{"migrate-ha"}},
		{name: "unknown subcommand", args: []string{"migrate-ha", "unknown"}},
		{name: "inspect missing entity", args: []string{"migrate-ha", "inspect"}},
		{name: "propose missing entity", args: []string{"migrate-ha", "propose"}},
		{name: "propose flag in entity position", args: []string{"migrate-ha", "propose", "--output", "yaml"}},
		{name: "propose unknown flag", args: []string{"migrate-ha", "propose", "automation.hall", "--bogus"}},
		{name: "list extra argument", args: []string{"migrate-ha", "list", "extra"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			var stdout strings.Builder
			var stderr strings.Builder
			args := append([]string{"--url", "http://127.0.0.1:1", "--token", "token"}, tt.args...)
			exit := Run(context.Background(), args, &stdout, &stderr, mapEnv(nil), "test")
			if exit == 0 || stdout.Len() != 0 || !strings.Contains(stderr.String(), "migrate-ha") {
				t.Fatalf("exit=%d stdout=%q stderr=%q", exit, stdout.String(), stderr.String())
			}
		})
	}
}

func mapEnv(values map[string]string) envGetter {
	return func(key string) string {
		return values[key]
	}
}
