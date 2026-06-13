package main

import (
	"context"
	"os"
	"os/signal"

	"github.com/tobiash/ha-intentional/internal/intentionalctl"
)

var version = "dev"

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()

	exitCode := intentionalctl.Run(ctx, os.Args[1:], os.Stdout, os.Stderr, os.Getenv, version)
	if exitCode != 0 {
		os.Exit(exitCode)
	}
}
