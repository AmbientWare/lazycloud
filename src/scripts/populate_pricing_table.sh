#!/bin/bash

# make sure .env is set
source .env

python -m src.services.platform.platform_manager --populate
