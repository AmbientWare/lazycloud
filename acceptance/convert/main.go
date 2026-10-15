// Command convert converts the platform images and the managed image for
// this host's architecture into a database, as a server whose host named
// them does. The acceptance tests, built with the race detector, run it
// against their template database: conversion under the instrumentation
// takes many times longer, and every test's database clones the result.
//
// It reads a JSON spec on standard input: the database URL, the object
// store, the images owner's settings and the managed Python version.
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"runtime"
	"time"

	"golang.org/x/sync/errgroup"

	"github.com/AmbientWare/lazycloud/internal/compute"
	"github.com/AmbientWare/lazycloud/internal/database"
	"github.com/AmbientWare/lazycloud/internal/execution"
	"github.com/AmbientWare/lazycloud/internal/images"
	"github.com/AmbientWare/lazycloud/internal/platformimages"
	"github.com/AmbientWare/lazycloud/internal/secrets"
	"github.com/AmbientWare/lazycloud/internal/storage"
)

// Spec is what the command reads on standard input.
type Spec struct {
	Database string
	Store    storage.Config
	Images   images.Config
	Python   string
}

func main() {
	if err := run(context.Background()); err != nil {
		fmt.Fprintln(os.Stderr, "convert:", err)
		os.Exit(1)
	}
}

func run(ctx context.Context) error {
	var spec Spec
	if err := json.NewDecoder(os.Stdin).Decode(&spec); err != nil {
		return fmt.Errorf("read the spec: %w", err)
	}
	pool, err := database.Open(ctx, spec.Database)
	if err != nil {
		return err
	}
	defer pool.Close()
	key, err := secrets.NewFileKey(make([]byte, 32))
	if err != nil {
		return err
	}
	exec := execution.NewExecution(pool)
	im, err := images.NewImages(pool, exec, secrets.NewSecrets(pool, key), storage.NewStorage(pool, spec.Store, compute.NewCompute(pool, nil, compute.Config{})), spec.Images)
	if err != nil {
		return err
	}
	source, err := im.ManagedSource(ctx, spec.Python)
	if err != nil {
		return err
	}
	references := append(platformimages.All(), source)
	g, converting := errgroup.WithContext(ctx)
	for _, reference := range references {
		g.Go(func() error {
			began := time.Now()
			if err := im.ConvertPlatformImage(converting, reference, runtime.GOARCH); err != nil {
				return err
			}
			fmt.Printf("converted %s in %s\n", reference, time.Since(began).Round(time.Millisecond))
			return nil
		})
	}
	if err := g.Wait(); err != nil {
		return err //nolint:wrapcheck // The conversion names its image.
	}
	var converted int
	if err := pool.QueryRow(ctx, "select count(*) from platform_images where mirror is not null").Scan(&converted); err != nil {
		return fmt.Errorf("count the converted images: %w", err)
	}
	if converted != len(references) {
		return fmt.Errorf("%d of %d images converted", converted, len(references))
	}
	return nil
}
