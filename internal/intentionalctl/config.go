package intentionalctl

import (
	"bufio"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type envGetter func(string) string

type Config struct {
	URL     string
	Token   string
	EnvFile string
	Timeout time.Duration
	Compact bool
}

func loadConfig(getenv envGetter, urlFlag string, tokenFlag string, envFile string, timeout time.Duration, compact bool) (Config, error) {
	values := map[string]string{}
	if envFile != "" {
		loaded, err := readEnvFile(envFile)
		if err != nil && !os.IsNotExist(err) {
			return Config{}, err
		}
		values = loaded
	}
	urlValue := firstNonEmpty(urlFlag, getenv("HASS_URL"), getenv("HOMEASSISTANT_URL"), values["HASS_URL"], values["HOMEASSISTANT_URL"])
	tokenValue := firstNonEmpty(tokenFlag, getenv("HASS_TOKEN"), getenv("HOMEASSISTANT_TOKEN"), values["HASS_TOKEN"], values["HOMEASSISTANT_TOKEN"])
	return Config{URL: urlValue, Token: tokenValue, EnvFile: envFile, Timeout: timeout, Compact: compact}, nil
}

func defaultEnvFile() string {
	home, err := os.UserHomeDir()
	if err != nil || home == "" {
		return ""
	}
	return filepath.Join(home, ".ha-env")
}

func readEnvFile(path string) (map[string]string, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	values := map[string]string{}
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		values[strings.TrimSpace(key)] = strings.Trim(strings.TrimSpace(value), `"'`)
	}
	if err := scanner.Err(); err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	return values, nil
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return strings.TrimSpace(value)
		}
	}
	return ""
}
