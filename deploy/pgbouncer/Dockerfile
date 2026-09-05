FROM alpine:3.23@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40 AS build

RUN apk add --no-cache build-base c-ares-dev curl libevent-dev openssl-dev pkgconf
WORKDIR /build
RUN curl --fail --silent --show-error --location \
      https://www.pgbouncer.org/downloads/files/1.25.2/pgbouncer-1.25.2.tar.gz \
      --output pgbouncer.tar.gz \
    && printf '%s  %s\n' \
      924ad35113fd0a71c8e2dbe85b5d03445532e2b7b37a9f8a48983beea238b332 \
      pgbouncer.tar.gz | sha256sum -c - \
    && tar -xzf pgbouncer.tar.gz --strip-components=1 \
    && ./configure --prefix=/usr --with-cares \
    && make -j2 pgbouncer

FROM alpine:3.23@sha256:fd791d74b68913cbb027c6546007b3f0d3bc45125f797758156952bc2d6daf40

RUN apk add --no-cache c-ares libevent libssl3 postgresql18-client \
    && addgroup -g 10001 pgbouncer \
    && adduser -D -H -u 10001 -G pgbouncer pgbouncer
COPY --from=build /build/pgbouncer /usr/bin/pgbouncer
COPY pgbouncer.ini /etc/pgbouncer/pgbouncer.ini
COPY --chmod=755 entrypoint.sh /usr/local/bin/pgbouncer-entrypoint
USER 10001:10001
EXPOSE 6432
ENTRYPOINT ["/usr/local/bin/pgbouncer-entrypoint"]
