# Local services

`docker compose up -d --wait postgres object-store` starts PostgreSQL on
127.0.0.1:25432 and Garage's S3 API on 127.0.0.1:23900.
`docker compose run --rm object-store-bootstrap` creates the `lazycloud` bucket
and the development key. Both steps are idempotent.

Owner tests use `docker compose -f compose.test.yaml up -d --wait`, a disposable
PostgreSQL on 127.0.0.1:15442.
