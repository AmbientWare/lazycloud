#!/bin/bash

# Generate a random api key
API_KEY=$(python3 -c "from database.utils import generate_api_key; print(generate_api_key())")

# Print the api key
echo $API_KEY
