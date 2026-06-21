package intentionalctl

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/url"
	"os"
	"strings"
	"time"
)

const usageText = `intentionalctl - agent-oriented client for the Intentional Home Assistant API

Configuration:
  --url       Home Assistant URL. Defaults to HASS_URL, HOMEASSISTANT_URL, or ~/.ha-env.
  --token     Home Assistant long-lived token. Defaults to HASS_TOKEN, HOMEASSISTANT_TOKEN, or ~/.ha-env.
  --env-file  Env file to read when env vars are absent. Defaults to ~/.ha-env.

Commands:
  health                         Print integration health.
  schema                         Print machine-readable DSL capabilities.
  world                          Print desired/actual world model.
  state                          Print active intents grouped by target.
  explain TARGET                 Explain one target.
  card [--target TARGET]          Print Lovelace-friendly explain data.
  preview [--file FILE] [--state KEY=VALUE]
                                  Preview desired-vs-actual target diffs.
  dashboard                      Print suggested Lovelace room cards.
  diagnostics [--limit N]         Print recent runtime diagnostics.
  rules-list                     List rule files exposed by the API.
  rules-get [--contents]          Read storage-backed authored rule document.
  rules-save --file FILE [--expected-generation GEN]
                                  Save full rule document.
  rules-delete [--expected-generation GEN]
                                  Clear the storage-backed authored rule document.
  validate --file FILE            Validate proposed rule YAML.
  dry-run --file FILE [--state KEY=VALUE]
                                  Evaluate proposed YAML without applying.
  simulate --file FILE --timeline FILE
                                  Simulate proposed YAML over a timeline JSON file.
  replay [--file FILE] [--history FILE | --from TIME --to TIME --entity ENTITY]
                                  Replay rules over HA history-shaped data.
  history                        List rule document history.
  history-get GENERATION [--contents]
                                  Read one previous rule document generation.
  rollback --generation GEN --expected-generation GEN
                                  Restore a previous rule document generation.
  patch-rule --rule-id ID --file FILE --expected-generation GEN
                                  Replace one authored rule by ID.
  reload                         Reload stored rules.
  version                        Print CLI version.
`

func Run(ctx context.Context, args []string, stdout io.Writer, stderr io.Writer, getenv envGetter, version string) int {
	if err := run(ctx, args, stdout, stderr, getenv, version); err != nil {
		fmt.Fprintln(stderr, err)
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 1
	}
	return 0
}

func run(ctx context.Context, args []string, stdout io.Writer, stderr io.Writer, getenv envGetter, version string) error {
	root := flag.NewFlagSet("intentionalctl", flag.ContinueOnError)
	root.SetOutput(stderr)
	urlFlag := root.String("url", "", "Home Assistant URL")
	tokenFlag := root.String("token", "", "Home Assistant token")
	envFile := root.String("env-file", defaultEnvFile(), "env file containing HASS_URL/HASS_TOKEN")
	timeout := root.Duration("timeout", 30*time.Second, "HTTP timeout")
	compact := root.Bool("compact", false, "print compact JSON")
	root.Usage = func() { fmt.Fprint(stderr, usageText) }
	if err := root.Parse(args); err != nil {
		return err
	}
	remaining := root.Args()
	if len(remaining) == 0 {
		root.Usage()
		return flag.ErrHelp
	}
	command := remaining[0]
	if command == "version" {
		fmt.Fprintln(stdout, version)
		return nil
	}

	config, err := loadConfig(getenv, *urlFlag, *tokenFlag, *envFile, *timeout, *compact)
	if err != nil {
		return err
	}
	client, err := NewClient(config.URL, config.Token, config.Timeout)
	if err != nil {
		return err
	}
	return executeCommand(ctx, client, config, command, remaining[1:], stdout, stderr)
}

func executeCommand(ctx context.Context, client *Client, config Config, command string, args []string, stdout io.Writer, stderr io.Writer) error {
	switch command {
	case "health":
		return getJSON(ctx, client, config, stdout, "/api/intentional/health")
	case "schema":
		return getJSON(ctx, client, config, stdout, "/api/intentional/schema")
	case "world":
		return getJSON(ctx, client, config, stdout, "/api/intentional/world")
	case "state":
		return getJSON(ctx, client, config, stdout, "/api/intentional/state")
	case "explain":
		if len(args) != 1 {
			return fmt.Errorf("usage: intentionalctl explain TARGET")
		}
		return getJSON(ctx, client, config, stdout, "/api/intentional/explain/"+url.PathEscape(args[0]))
	case "card":
		return card(ctx, client, config, args, stdout, stderr)
	case "preview":
		return preview(ctx, client, config, args, stdout, stderr)
	case "dashboard":
		return getJSON(ctx, client, config, stdout, "/api/intentional/dashboard")
	case "diagnostics":
		flags := flag.NewFlagSet("diagnostics", flag.ContinueOnError)
		flags.SetOutput(stderr)
		limit := flags.Int("limit", 50, "diagnostic event limit")
		if err := flags.Parse(args); err != nil {
			return err
		}
		return getJSON(ctx, client, config, stdout, fmt.Sprintf("/api/intentional/diagnostics?limit=%d", *limit))
	case "rules-list":
		return getJSON(ctx, client, config, stdout, "/api/intentional/rules")
	case "rules-get":
		flags := flag.NewFlagSet("rules-get", flag.ContinueOnError)
		flags.SetOutput(stderr)
		contentsOnly := flags.Bool("contents", false, "print only YAML contents")
		if err := flags.Parse(args); err != nil {
			return err
		}
		response, err := client.Get(ctx, "/api/intentional/rules/document")
		if err != nil {
			return err
		}
		if *contentsOnly {
			var document struct {
				Contents string `json:"contents"`
			}
			if err := json.Unmarshal(response, &document); err != nil {
				return err
			}
			fmt.Fprint(stdout, document.Contents)
			return nil
		}
		return writeJSON(stdout, response, config.Compact)
	case "rules-save":
		return saveDocument(ctx, client, config, args, stdout, stderr)
	case "rules-delete":
		return deleteDocument(ctx, client, config, args, stdout, stderr)
	case "validate":
		return postContentsFile(ctx, client, config, args, stdout, stderr, "validate", "/api/intentional/validate")
	case "dry-run":
		return dryRun(ctx, client, config, args, stdout, stderr)
	case "simulate":
		return simulate(ctx, client, config, args, stdout, stderr)
	case "replay":
		return replay(ctx, client, config, args, stdout, stderr)
	case "history":
		return getJSON(ctx, client, config, stdout, "/api/intentional/rules/history")
	case "history-get":
		return getHistory(ctx, client, config, args, stdout, stderr)
	case "rollback":
		return rollback(ctx, client, config, args, stdout, stderr)
	case "patch-rule":
		return patchRule(ctx, client, config, args, stdout, stderr)
	case "reload":
		response, err := client.Post(ctx, "/api/intentional/reload", map[string]any{})
		if err != nil {
			return err
		}
		return writeJSON(stdout, response, config.Compact)
	default:
		return fmt.Errorf("unknown command %q", command)
	}
}

func deleteDocument(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("rules-delete", flag.ContinueOnError)
	flags.SetOutput(stderr)
	expectedGeneration := flags.String("expected-generation", "", "generation read before deleting")
	if err := flags.Parse(args); err != nil {
		return err
	}
	payload := map[string]any{}
	if *expectedGeneration != "" {
		payload["expected_generation"] = *expectedGeneration
	}
	response, err := client.Delete(ctx, "/api/intentional/rules/document", payload)
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func getJSON(ctx context.Context, client *Client, config Config, stdout io.Writer, path string) error {
	response, err := client.Get(ctx, path)
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func card(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("card", flag.ContinueOnError)
	flags.SetOutput(stderr)
	target := flags.String("target", "", "target entity to explain")
	if err := flags.Parse(args); err != nil {
		return err
	}
	path := "/api/intentional/card"
	if *target != "" {
		path += "?target=" + url.QueryEscape(*target)
	}
	return getJSON(ctx, client, config, stdout, path)
}

func preview(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("preview", flag.ContinueOnError)
	flags.SetOutput(stderr)
	file := flags.String("file", "", "rule YAML file; omitted previews current live intents")
	states := multiFlag{}
	flags.Var(&states, "state", "state override KEY=VALUE; repeatable")
	if err := flags.Parse(args); err != nil {
		return err
	}
	payload := map[string]any{}
	if *file != "" {
		contents, err := readRequiredFile(*file)
		if err != nil {
			return err
		}
		payload["contents"] = contents
	}
	if len(states) > 0 {
		payload["state_overrides"] = parseKeyValues(states)
	}
	response, err := client.Post(ctx, "/api/intentional/preview", payload)
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func saveDocument(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("rules-save", flag.ContinueOnError)
	flags.SetOutput(stderr)
	file := flags.String("file", "", "rule document YAML file")
	expectedGeneration := flags.String("expected-generation", "", "generation read before editing")
	if err := flags.Parse(args); err != nil {
		return err
	}
	contents, err := readRequiredFile(*file)
	if err != nil {
		return err
	}
	payload := map[string]any{"contents": contents}
	if *expectedGeneration != "" {
		payload["expected_generation"] = *expectedGeneration
	}
	response, err := client.Put(ctx, "/api/intentional/rules/document", payload)
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func postContentsFile(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer, name string, path string) error {
	flags := flag.NewFlagSet(name, flag.ContinueOnError)
	flags.SetOutput(stderr)
	file := flags.String("file", "", "rule YAML file")
	if err := flags.Parse(args); err != nil {
		return err
	}
	contents, err := readRequiredFile(*file)
	if err != nil {
		return err
	}
	response, err := client.Post(ctx, path, map[string]any{"contents": contents})
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func dryRun(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("dry-run", flag.ContinueOnError)
	flags.SetOutput(stderr)
	file := flags.String("file", "", "rule YAML file")
	states := multiFlag{}
	flags.Var(&states, "state", "state override KEY=VALUE; repeatable")
	if err := flags.Parse(args); err != nil {
		return err
	}
	contents, err := readRequiredFile(*file)
	if err != nil {
		return err
	}
	payload := map[string]any{"contents": contents}
	if len(states) > 0 {
		payload["state_overrides"] = parseKeyValues(states)
	}
	response, err := client.Post(ctx, "/api/intentional/dry-run", payload)
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func simulate(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("simulate", flag.ContinueOnError)
	flags.SetOutput(stderr)
	file := flags.String("file", "", "rule YAML file")
	timelineFile := flags.String("timeline", "", "timeline JSON file")
	if err := flags.Parse(args); err != nil {
		return err
	}
	contents, err := readRequiredFile(*file)
	if err != nil {
		return err
	}
	timelineBytes, err := os.ReadFile(*timelineFile)
	if err != nil {
		return fmt.Errorf("read timeline file: %w", err)
	}
	var timeline any
	if err := json.Unmarshal(timelineBytes, &timeline); err != nil {
		return fmt.Errorf("parse timeline JSON: %w", err)
	}
	if object, ok := timeline.(map[string]any); ok {
		if nested, exists := object["timeline"]; exists {
			timeline = nested
		}
	}
	response, err := client.Post(ctx, "/api/intentional/simulate", map[string]any{"contents": contents, "timeline": timeline})
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func replay(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("replay", flag.ContinueOnError)
	flags.SetOutput(stderr)
	file := flags.String("file", "", "rule YAML file; omitted uses storage-backed rules")
	historyFile := flags.String("history", "", "HA history JSON file")
	from := flags.String("from", "", "history start time, e.g. 2026-06-14T20:00:00+00:00")
	to := flags.String("to", "", "history end time")
	entities := multiFlag{}
	flags.Var(&entities, "entity", "entity to fetch from HA history; repeatable")
	if err := flags.Parse(args); err != nil {
		return err
	}
	payload := map[string]any{}
	if *file != "" {
		contents, err := readRequiredFile(*file)
		if err != nil {
			return err
		}
		payload["contents"] = contents
	}
	if *historyFile != "" {
		history, err := readJSONFile(*historyFile)
		if err != nil {
			return err
		}
		payload["history"] = history
	} else if *from != "" {
		if len(entities) == 0 {
			return fmt.Errorf("replay --from requires at least one --entity")
		}
		path := "/api/history/period/" + url.PathEscape(*from) + "?filter_entity_id=" + url.QueryEscape(strings.Join(entities, ","))
		if *to != "" {
			path += "&end_time=" + url.QueryEscape(*to)
		}
		history, err := client.Get(ctx, path)
		if err != nil {
			return err
		}
		var decoded any
		if err := json.Unmarshal(history, &decoded); err != nil {
			return err
		}
		payload["history"] = decoded
	} else {
		return fmt.Errorf("replay requires --history or --from")
	}
	response, err := client.Post(ctx, "/api/intentional/replay", payload)
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func getHistory(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("history-get", flag.ContinueOnError)
	flags.SetOutput(stderr)
	contentsOnly := flags.Bool("contents", false, "print only YAML contents")
	reorderedArgs := moveHistoryGetFlagsBeforeGeneration(args)
	if err := flags.Parse(reorderedArgs); err != nil {
		return err
	}
	remaining := flags.Args()
	if len(remaining) != 1 {
		return fmt.Errorf("usage: intentionalctl history-get GENERATION [--contents]")
	}
	response, err := client.Get(ctx, "/api/intentional/rules/history/"+url.PathEscape(remaining[0]))
	if err != nil {
		return err
	}
	if *contentsOnly {
		var document struct {
			Contents string `json:"contents"`
		}
		if err := json.Unmarshal(response, &document); err != nil {
			return err
		}
		fmt.Fprint(stdout, document.Contents)
		return nil
	}
	return writeJSON(stdout, response, config.Compact)
}

func moveHistoryGetFlagsBeforeGeneration(args []string) []string {
	if len(args) <= 1 {
		return args
	}
	reordered := make([]string, 0, len(args))
	positionals := make([]string, 0, len(args))
	for _, arg := range args {
		if strings.HasPrefix(arg, "-") {
			reordered = append(reordered, arg)
			continue
		}
		positionals = append(positionals, arg)
	}
	return append(reordered, positionals...)
}

func rollback(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("rollback", flag.ContinueOnError)
	flags.SetOutput(stderr)
	generation := flags.String("generation", "", "history generation to restore")
	expectedGeneration := flags.String("expected-generation", "", "current generation")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *generation == "" || *expectedGeneration == "" {
		return fmt.Errorf("rollback requires --generation and --expected-generation")
	}
	response, err := client.Post(ctx, "/api/intentional/rules/rollback", map[string]any{"generation": *generation, "expected_generation": *expectedGeneration})
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func patchRule(ctx context.Context, client *Client, config Config, args []string, stdout io.Writer, stderr io.Writer) error {
	flags := flag.NewFlagSet("patch-rule", flag.ContinueOnError)
	flags.SetOutput(stderr)
	ruleID := flags.String("rule-id", "", "authored rule ID")
	file := flags.String("file", "", "replacement rule YAML file")
	expectedGeneration := flags.String("expected-generation", "", "current generation")
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *ruleID == "" || *expectedGeneration == "" {
		return fmt.Errorf("patch-rule requires --rule-id and --expected-generation")
	}
	contents, err := readRequiredFile(*file)
	if err != nil {
		return err
	}
	response, err := client.Patch(ctx, "/api/intentional/rules/id/"+url.PathEscape(*ruleID), map[string]any{"contents": contents, "expected_generation": *expectedGeneration})
	if err != nil {
		return err
	}
	return writeJSON(stdout, response, config.Compact)
}

func readRequiredFile(path string) (string, error) {
	if strings.TrimSpace(path) == "" {
		return "", fmt.Errorf("missing --file")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return string(data), nil
}

func readJSONFile(path string) (any, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var value any
	if err := json.Unmarshal(data, &value); err != nil {
		return nil, fmt.Errorf("parse JSON file: %w", err)
	}
	return value, nil
}

func writeJSON(stdout io.Writer, raw json.RawMessage, compact bool) error {
	if compact {
		_, err := fmt.Fprintln(stdout, string(raw))
		return err
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		_, writeErr := fmt.Fprintln(stdout, string(raw))
		return writeErr
	}
	encoded, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	_, err = fmt.Fprintln(stdout, string(encoded))
	return err
}

type multiFlag []string

func (m *multiFlag) String() string { return strings.Join(*m, ",") }

func (m *multiFlag) Set(value string) error {
	*m = append(*m, value)
	return nil
}

func parseKeyValues(values []string) map[string]string {
	result := map[string]string{}
	for _, value := range values {
		key, item, ok := strings.Cut(value, "=")
		if !ok {
			result[value] = ""
			continue
		}
		result[key] = item
	}
	return result
}
