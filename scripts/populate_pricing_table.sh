#!/bin/bash

# make sure ../.env is set
source ../.env

python -m machines.services.platform.platform_manager --populate
