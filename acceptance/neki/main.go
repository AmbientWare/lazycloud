// Command neki executes every sqlc query through PlanetScale Neki's query
// router and lists the ones the router fails. It creates a scratch database
// of its own on the router's branch (lc_neki_check_ and a random suffix, so
// runs never share one), migrates it with `server migrate`, then prepares,
// binds and executes each query with typed dummy values inside a
// transaction it rolls back, and drops that database before it exits.
//
// Only router failures count: SQLSTATE NK*, 22023 BindParameters errors,
// "not implemented", and syntax, undefined-name (class 42) and internal (XX)
// errors, which valid queries get only from the router's rewrite.
// Constraint violations and other ordinary PostgreSQL errors from dummy
// values do not. seed.sql gives the core tables one row keyed by the zero
// UUID the checker binds, so per-row router paths run; tables it leaves
// empty exercise only their empty-input path. The base URL comes from
// NEKI_DATABASE_URL; no URL, user, host or password is ever printed.
//
//	NEKI_DATABASE_URL=... go run ./acceptance/neki [-repo dir] [-v]
package main

import (
	"context"
	"crypto/rand"
	_ "embed"
	"encoding/hex"
	"errors"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"net/netip"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
)

//go:embed seed.sql
var seed string

const (
	// scratchPrefix starts every run's scratch database name.
	scratchPrefix = "lc_neki_check_"
	workers       = 8
	queryTimeout  = 30 * time.Second
)

func main() {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	code := run(ctx)
	stop()
	os.Exit(code)
}

func run(ctx context.Context) int {
	repo := flag.String("repo", ".", "repository root holding internal/*/*.sql.go and cmd/server")
	verbose := flag.Bool("v", false, "also list ordinary PostgreSQL errors")
	flag.Parse()
	base := os.Getenv("NEKI_DATABASE_URL")
	if base == "" {
		fmt.Fprintln(os.Stderr, "neki: NEKI_DATABASE_URL is empty")
		return 2
	}
	name, err := scratchName()
	if err != nil {
		fmt.Fprintln(os.Stderr, "neki:", err)
		return 2
	}
	red, scratch, err := scratchURL(base, name)
	if err != nil {
		fmt.Fprintln(os.Stderr, "neki:", err)
		return 2
	}
	queries, err := loadQueries(*repo)
	if err != nil {
		fmt.Fprintln(os.Stderr, "neki:", red.clean(err.Error()))
		return 2
	}
	if err := withScratch(ctx, base, name, func() error {
		if err := migrate(ctx, *repo, scratch, red); err != nil {
			return err
		}
		return checkAll(ctx, scratch, queries, red, *verbose)
	}); err != nil {
		fmt.Fprintln(os.Stderr, "neki:", red.clean(err.Error()))
		return 1
	}
	return 0
}

// redactor removes the URL, its credentials and resolved addresses from
// anything printed.
type redactor struct {
	secrets []string
	address *regexp.Regexp
}

func (r redactor) clean(s string) string {
	for _, v := range r.secrets {
		if v != "" {
			s = strings.ReplaceAll(s, v, "<redacted>")
		}
	}
	return r.address.ReplaceAllString(s, "<address>")
}

// scratchName is this run's scratch database: the prefix and 12 random hex
// digits.
func scratchName() (string, error) {
	var b [6]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", fmt.Errorf("name the scratch database: %w", err)
	}
	return scratchPrefix + hex.EncodeToString(b[:]), nil
}

// scratchURL swaps the database in base's path for the scratch database.
func scratchURL(base, name string) (redactor, string, error) {
	u, err := url.Parse(base)
	if err != nil {
		return redactor{}, "", errors.New("parse database url failed") // the parse error quotes the URL
	}
	pass, _ := u.User.Password()
	red := redactor{
		secrets: []string{base, pass, u.User.Username(), u.Hostname()},
		address: regexp.MustCompile(`\d+\.\d+\.\d+\.\d+|\[?[0-9a-fA-F]*:[0-9a-fA-F:]+:[0-9a-fA-F]+\]?`),
	}
	u.Path = "/" + name
	scratch := u.String()
	red.secrets = append([]string{scratch}, red.secrets...)
	return red, scratch, nil
}

// withScratch creates the scratch database name through base, runs fn and
// drops that database again, also when fn fails or the run is interrupted.
// It touches no other database, so concurrent runs do not collide.
func withScratch(ctx context.Context, base, name string, fn func() error) (err error) {
	admin, err := pgx.Connect(ctx, base)
	if err != nil {
		return fmt.Errorf("connect: %w", err)
	}
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
		defer cancel()
		if cerr := admin.Close(closeCtx); cerr != nil && err == nil {
			err = fmt.Errorf("close: %w", cerr)
		}
	}()
	if _, err := admin.Exec(ctx, "create database "+name); err != nil {
		return fmt.Errorf("create scratch database: %w", err)
	}
	defer func() {
		dropCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), time.Minute)
		defer cancel()
		if _, derr := admin.Exec(dropCtx, "drop database "+name); derr != nil {
			err = errors.Join(err, fmt.Errorf("drop scratch database: %w", derr))
			return
		}
		fmt.Println("dropped", name)
	}()
	return fn()
}

// migrate applies the migration chain to the scratch database the way a
// release does.
func migrate(ctx context.Context, repo, scratch string, red redactor) error {
	cmd := exec.CommandContext(ctx, "go", "run", "./cmd/server", "migrate")
	cmd.Dir = repo
	cmd.Env = append(os.Environ(), "LAZYCLOUD_DATABASE_URL="+scratch, "LAZYCLOUD_DATABASE_SESSION_URL="+scratch)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("migrate scratch database: %w\n%s", err, red.clean(string(out)))
	}
	return nil
}

type query struct{ owner, name, sql string }

func (q query) String() string { return q.owner + "." + q.name }

// loadQueries reads the sqlc query constants of every owner package.
func loadQueries(repo string) ([]query, error) {
	files, err := filepath.Glob(filepath.Join(repo, "internal", "*", "*.sql.go"))
	if err != nil {
		return nil, fmt.Errorf("list query files: %w", err)
	}
	var qs []query
	for _, f := range files {
		file, err := parser.ParseFile(token.NewFileSet(), f, nil, 0)
		if err != nil {
			return nil, fmt.Errorf("parse %s: %w", f, err)
		}
		for _, d := range file.Decls {
			g, ok := d.(*ast.GenDecl)
			if !ok || g.Tok != token.CONST {
				continue
			}
			for _, s := range g.Specs {
				v, ok := s.(*ast.ValueSpec)
				if !ok || len(v.Values) != 1 {
					continue
				}
				lit, ok := v.Values[0].(*ast.BasicLit)
				if !ok || lit.Kind != token.STRING {
					continue
				}
				text, err := strconv.Unquote(lit.Value)
				if err != nil || !strings.HasPrefix(text, "-- name:") {
					continue
				}
				qs = append(qs, query{filepath.Base(filepath.Dir(f)), strings.Fields(text)[2], text})
			}
		}
	}
	if len(qs) == 0 {
		return nil, fmt.Errorf("no sqlc queries under %s", repo)
	}
	sort.Slice(qs, func(i, j int) bool { return qs[i].String() < qs[j].String() })
	return qs, nil
}

type outcome int

const (
	passed outcome = iota
	ordinary
	routerFailure
	checkerError
)

type result struct {
	outcome outcome
	err     error
}

// checkAll runs every query on a bounded set of connections and prints the
// failures. It fails when the router fails any query or a query could not be
// checked.
func checkAll(ctx context.Context, scratch string, qs []query, red redactor, verbose bool) error {
	conn, err := pgx.Connect(ctx, scratch)
	if err != nil {
		return fmt.Errorf("connect scratch database: %w", err)
	}
	_, err = conn.Exec(ctx, seed)
	if err != nil {
		err = fmt.Errorf("seed: %w", err)
	}
	var types typeCatalog
	if err == nil {
		types, err = loadTypes(ctx, conn)
	}
	if cerr := conn.Close(ctx); cerr != nil && err == nil {
		err = fmt.Errorf("close: %w", cerr)
	}
	if err != nil {
		return err
	}

	results := make([]result, len(qs))
	next := make(chan int)
	errs := make([]error, workers)
	var wg sync.WaitGroup
	for w := range workers {
		wg.Go(func() { errs[w] = worker(ctx, scratch, types, qs, next, results) })
	}
	for i := range qs {
		select {
		case next <- i:
		case <-ctx.Done():
		}
		if ctx.Err() != nil {
			break
		}
	}
	close(next)
	wg.Wait()
	if err := errors.Join(append(errs, ctx.Err())...); err != nil {
		return err
	}

	counts := map[outcome]int{}
	for i, r := range results {
		counts[r.outcome]++
		switch r.outcome {
		case routerFailure:
			fmt.Printf("ROUTER\t%s\t%s\n", qs[i], red.clean(r.err.Error()))
		case checkerError:
			fmt.Printf("UNCHECKED\t%s\t%s\n", qs[i], red.clean(r.err.Error()))
		case ordinary:
			if verbose {
				fmt.Printf("postgres\t%s\t%s\n", qs[i], red.clean(r.err.Error()))
			}
		case passed:
		}
	}
	fmt.Printf("checked %d: %d passed, %d ordinary postgres errors, %d router failures, %d unchecked\n",
		len(qs), counts[passed], counts[ordinary], counts[routerFailure], counts[checkerError])
	if counts[routerFailure] > 0 || counts[checkerError] > 0 {
		return errors.New("router failures or unchecked queries")
	}
	return nil
}

func worker(ctx context.Context, scratch string, types typeCatalog, qs []query, next <-chan int, results []result) (err error) {
	conn, err := pgx.Connect(ctx, scratch)
	if err != nil {
		return fmt.Errorf("connect scratch database: %w", err)
	}
	defer func() {
		closeCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
		defer cancel()
		if cerr := conn.Close(closeCtx); cerr != nil && err == nil {
			err = fmt.Errorf("close: %w", cerr)
		}
	}()
	for i := range next {
		results[i] = check(ctx, conn, types, qs[i].sql)
		// A session advisory lock outlives the rollback, and the router
		// refuses transaction advisory locks beside one.
		sessionLock := strings.Contains(qs[i].sql, "pg_advisory_lock(") || strings.Contains(qs[i].sql, "pg_try_advisory_lock(")
		if !sessionLock && !conn.IsClosed() {
			continue
		}
		if err := conn.Close(ctx); err != nil {
			return fmt.Errorf("close: %w", err)
		}
		fresh, err := pgx.Connect(ctx, scratch)
		if err != nil {
			return fmt.Errorf("reconnect scratch database: %w", err)
		}
		conn = fresh
	}
	return nil
}

// check prepares sql, binds a dummy value of each parameter's type and
// executes it in a transaction it rolls back.
func check(ctx context.Context, conn *pgx.Conn, types typeCatalog, sql string) result {
	ctx, cancel := context.WithTimeout(ctx, queryTimeout)
	defer cancel()
	sd, err := conn.Prepare(ctx, "", sql)
	if err != nil {
		return classify(fmt.Errorf("prepare: %w", err))
	}
	// Empty arrays skip what joins their elements; one-element arrays reach it.
	var r result
	for _, arrayLen := range []int{0, 1} {
		args := make([]any, len(sd.ParamOIDs))
		arrays := false
		for i, oid := range sd.ParamOIDs {
			if args[i], err = types.dummy(oid, arrayLen); err != nil {
				return result{checkerError, fmt.Errorf("parameter $%d: %w", i+1, err)}
			}
			arrays = arrays || types[oid].category == 'A'
		}
		if arrayLen == 1 && !arrays {
			break
		}
		tx, err := conn.Begin(ctx)
		if err != nil {
			return result{checkerError, fmt.Errorf("begin: %w", err)}
		}
		r = classify(execute(ctx, tx, sql, args))
		if err := tx.Rollback(ctx); err != nil && !errors.Is(err, pgx.ErrTxClosed) {
			return result{checkerError, fmt.Errorf("rollback: %w", err)}
		}
		if r.outcome == routerFailure || r.outcome == checkerError {
			break
		}
	}
	return r
}

func execute(ctx context.Context, tx pgx.Tx, sql string, args []any) error {
	rows, err := tx.Query(ctx, sql, args...)
	if err != nil {
		return fmt.Errorf("execute: %w", err)
	}
	for rows.Next() {
		if _, err := rows.Values(); err != nil {
			rows.Close()
			return fmt.Errorf("read row: %w", err)
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return fmt.Errorf("execute: %w", err)
	}
	return nil
}

func classify(err error) result {
	if err == nil {
		return result{passed, nil}
	}
	var pe *pgconn.PgError
	if !errors.As(err, &pe) {
		if strings.Contains(err.Error(), "not implemented") {
			return result{routerFailure, err}
		}
		return result{checkerError, err}
	}
	// sqlc checks every query against the schema and owner tests run them
	// on PostgreSQL, so a syntax or undefined-name error (class 42) or an
	// internal error (XX) comes from the router's rewrite.
	if strings.HasPrefix(pe.Code, "NK") || strings.HasPrefix(pe.Code, "42") || strings.HasPrefix(pe.Code, "XX") ||
		(pe.Code == "22023" && strings.Contains(pe.Message, "BindParameters")) ||
		strings.Contains(strings.ToLower(pe.Message), "not implemented") {
		return result{routerFailure, err}
	}
	return result{ordinary, err}
}

type pgType struct {
	name     string
	kind     byte // typtype: b base, e enum, d domain, ...
	category byte // typcategory: A array, ...
	elem     uint32
	base     uint32
	label    string // first enum label
}

type typeCatalog map[uint32]pgType

func loadTypes(ctx context.Context, conn *pgx.Conn) (typeCatalog, error) {
	rows, err := conn.Query(ctx, `select oid::int8, typname::text, typtype::text, typcategory::text, typelem::int8, typbasetype::int8 from pg_type`)
	if err != nil {
		return nil, fmt.Errorf("read types: %w", err)
	}
	types := typeCatalog{}
	for rows.Next() {
		var oid, elem, base int64
		var name, kind, category string
		if err := rows.Scan(&oid, &name, &kind, &category, &elem, &base); err != nil {
			rows.Close()
			return nil, fmt.Errorf("read types: %w", err)
		}
		types[uint32(oid)] = pgType{name: name, kind: kind[0], category: category[0], elem: uint32(elem), base: uint32(base)} //nolint:gosec // OIDs are uint32.
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("read types: %w", err)
	}
	labels, err := conn.Query(ctx, `select enumtypid::int8, enumlabel::text from pg_enum order by enumtypid, enumsortorder`)
	if err != nil {
		return nil, fmt.Errorf("read enum labels: %w", err)
	}
	for labels.Next() {
		var oid int64
		var label string
		if err := labels.Scan(&oid, &label); err != nil {
			labels.Close()
			return nil, fmt.Errorf("read enum labels: %w", err)
		}
		if t := types[uint32(oid)]; t.label == "" { //nolint:gosec // OIDs are uint32.
			t.label = label
			types[uint32(oid)] = t //nolint:gosec // OIDs are uint32.
		}
	}
	labels.Close()
	if err := labels.Err(); err != nil {
		return nil, fmt.Errorf("read enum labels: %w", err)
	}
	return types, nil
}

// dummy returns a value of the parameter type oid in the Go type the owners
// bind, so pgx picks the same format the services send: zero UUIDs and
// numbers, empty strings, false, now and arrays of arrayLen such elements.
func (c typeCatalog) dummy(oid uint32, arrayLen int) (any, error) {
	t, ok := c[oid]
	if !ok {
		return nil, fmt.Errorf("unknown type oid %d", oid)
	}
	switch {
	case t.kind == 'd':
		return c.dummy(t.base, arrayLen)
	case t.kind == 'e':
		return t.label, nil
	case t.category == 'A':
		elem, err := c.dummy(t.elem, arrayLen)
		if err != nil {
			return nil, err
		}
		s := reflect.MakeSlice(reflect.SliceOf(reflect.TypeOf(elem)), arrayLen, arrayLen)
		for i := range arrayLen {
			s.Index(i).Set(reflect.ValueOf(elem))
		}
		return s.Interface(), nil
	}
	switch t.name {
	case "uuid":
		return uuid.UUID{}, nil
	case "int2", "int4", "int8", "oid", "numeric":
		return int64(0), nil
	case "float4", "float8":
		return float64(0), nil
	case "bool":
		return false, nil
	case "text", "varchar", "bpchar", "name", "citext":
		return "", nil
	case "timestamptz", "timestamp", "date":
		return time.Now(), nil
	case "interval":
		return time.Duration(0), nil
	case "bytea":
		return []byte{}, nil
	case "json", "jsonb":
		return "{}", nil
	case "inet", "cidr":
		return netip.MustParsePrefix("0.0.0.0/32"), nil
	}
	return nil, fmt.Errorf("no dummy value for type %s", t.name)
}
