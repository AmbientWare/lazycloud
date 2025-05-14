#!/bin/bash

# make sure .env is set
source .env

python -m src.lazycloud_api.services.platform.platform_manager --populate
