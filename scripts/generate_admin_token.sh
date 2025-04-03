#!/bin/bash

# Generate a random token
TOKEN=$(python3 -c "from machines.database.utils import generate_token; print(generate_token())")

# Print the token
echo $TOKEN
