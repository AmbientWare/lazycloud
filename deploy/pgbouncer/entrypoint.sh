#!/bin/sh
set -eu

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
umask 077
awk 'BEGIN {
    password = ENVIRON["POSTGRES_PASSWORD"]
    if (password ~ /[\r\n]/) {
        print "POSTGRES_PASSWORD must not contain line breaks" > "/dev/stderr"
        exit 1
    }
    gsub(/"/, "\"\"", password)
    printf "\"lazycloud\" \"%s\"\n", password
}' > /run/pgbouncer/userlist.txt

exec pgbouncer /etc/pgbouncer/pgbouncer.ini
