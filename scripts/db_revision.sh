#!/bin/bash

while getopts ":m:h" opt; do
  case ${opt} in
    m )
      message=$OPTARG
      ;;
    h )
      echo "Usage from main directory: ./scripts/db_revision.sh -m <message>"
      exit 0
      ;;
    \? )
      echo "Invalid option: $OPTARG" 1>&2
      exit 1
      ;;
    : )
      echo "Invalid option: $OPTARG requires an argument" 1>&2
      exit 1
      ;;
  esac
done

# check if message is empty
if [ -z "$message" ]; then
  echo "Revision message is required"
  exit 1
fi

# create a new revision
echo "Creating a new revision..."
echo "Revision message: $message"

docker compose up -d postgres pgbouncer
docker compose run --rm --no-deps api uv run alembic revision -m "$message" --autogenerate
