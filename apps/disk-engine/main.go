// Command lazycloud-disk runs one durable disk operation and exits.
//
// Each disk is a qcow2 chain under <root>/<disk_id>/, served by a
// qemu-storage-daemon that outlives this process, exposed through a kernel NBD
// device, and mounted as ext4. Results are JSON on stdout; failures go to
// stderr with a non-zero exit.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"
)

const usage = `usage: lazycloud-disk <command> [flags]

commands:
  attach            --root R --disk D --size BYTES --mountpoint M --chain CHAIN.json --store STORE.json
  seal              --root R --disk D
  publish           --root R --disk D --store STORE.json --generation G --parent P [--flatten]
  commit-published  --root R --disk D --generation G
  detach            --root R --disk D
  compact           --root R --disk D
  recover           --root R
  evict             --root R --disk D
`

type command func(ctx context.Context, args []string) (any, error)

var commands = map[string]command{
	"attach":           runAttach,
	"seal":             runSeal,
	"publish":          runPublish,
	"commit-published": runCommitPublished,
	"detach":           runDetach,
	"compact":          runCompact,
	"recover":          runRecover,
	"evict":            runEvict,
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	name := os.Args[1]
	cmd, ok := commands[name]
	if !ok {
		fmt.Fprintf(os.Stderr, "lazycloud-disk: unknown command %q\n%s", name, usage)
		os.Exit(2)
	}
	// SIGTERM cancels the context instead of killing the process, so a seal
	// interrupted mid-freeze still thaws the filesystem on its way out.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	result, err := cmd(ctx, os.Args[2:])
	if err != nil {
		fmt.Fprintf(os.Stderr, "lazycloud-disk %s: %v\n", name, err)
		stop()
		os.Exit(1)
	}
	if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
		fmt.Fprintf(os.Stderr, "lazycloud-disk %s: write result: %v\n", name, err)
		stop()
		os.Exit(1)
	}
}

type flags struct {
	set        *flag.FlagSet
	root       *string
	disk       *string
	required   []string
	needsDisk  bool
	parsedArgs []string
}

func newFlags(name string, needsDisk bool) *flags {
	set := flag.NewFlagSet(name, flag.ContinueOnError)
	set.SetOutput(io.Discard)
	f := &flags{set: set, needsDisk: needsDisk}
	f.root = set.String("root", "", "directory holding every disk's local state")
	if needsDisk {
		f.disk = set.String("disk", "", "disk id")
	}
	return f
}

func (f *flags) require(names ...string) {
	f.required = append(f.required, names...)
}

func (f *flags) parse(args []string) error {
	if err := f.set.Parse(args); err != nil {
		return err
	}
	if f.set.NArg() > 0 {
		return fmt.Errorf("unexpected arguments: %v", f.set.Args())
	}
	given := map[string]bool{}
	f.set.Visit(func(fl *flag.Flag) { given[fl.Name] = true })
	required := append([]string{"root"}, f.required...)
	if f.needsDisk {
		required = append(required, "disk")
	}
	for _, name := range required {
		if !given[name] {
			return fmt.Errorf("--%s is required", name)
		}
	}
	if f.needsDisk {
		if err := validateDiskID(*f.disk); err != nil {
			return err
		}
	}
	return nil
}

func (f *flags) paths() diskPaths {
	return diskPaths{root: *f.root, id: *f.disk}
}

func readJSONFile(path string, into any) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	decoder := json.NewDecoder(file)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(into); err != nil {
		return fmt.Errorf("parse %s: %w", path, err)
	}
	if decoder.More() {
		return fmt.Errorf("parse %s: trailing data", path)
	}
	return nil
}

var errNotAttached = errors.New("disk is not attached")
