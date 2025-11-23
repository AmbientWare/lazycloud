#!/bin/bash

NETWORK_NAME="lazycloud"
SUBNET="172.20.0.0/16"

check_subnet_conflict() {
    local subnet=$1
    local conflicting_networks=$(docker network ls --format "{{.ID}}" | while read id; do
        local net_subnet=$(docker network inspect "$id" --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}' 2>/dev/null)
        if [ "$net_subnet" = "$subnet" ]; then
            local net_name=$(docker network inspect "$id" --format '{{.Name}}' 2>/dev/null)
            echo "$net_name"
        fi
    done)
    
    if [ -n "$conflicting_networks" ]; then
        echo "$conflicting_networks"
        return 1
    fi
    return 0
}

if docker network inspect "$NETWORK_NAME" > /dev/null 2>&1; then
    echo "Network '$NETWORK_NAME' already exists."
    CURRENT_SUBNET=$(docker network inspect "$NETWORK_NAME" --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}')
    
    if [ "$CURRENT_SUBNET" != "$SUBNET" ]; then
        echo "Warning: Network exists with subnet $CURRENT_SUBNET, but docker-compose.yml expects $SUBNET"
        
        conflict=$(check_subnet_conflict "$SUBNET")
        if [ $? -eq 1 ]; then
            echo "Error: Subnet $SUBNET is already in use by network(s): $conflict"
            echo "Please remove the conflicting network(s) or update docker-compose.yml to use a different subnet."
            exit 1
        fi
        
        echo "Removing existing network..."
        docker network rm "$NETWORK_NAME" || exit 1
        echo "Creating network '$NETWORK_NAME' with subnet $SUBNET..."
        docker network create --subnet="$SUBNET" "$NETWORK_NAME" || exit 1
        echo "✓ Network created successfully"
    else
        echo "✓ Network already configured correctly with subnet $SUBNET"
    fi
else
    conflict=$(check_subnet_conflict "$SUBNET")
    if [ $? -eq 1 ]; then
        echo "Error: Subnet $SUBNET is already in use by network(s): $conflict"
        echo "Please remove the conflicting network(s) or update docker-compose.yml to use a different subnet."
        exit 1
    fi
    
    echo "Creating network '$NETWORK_NAME' with subnet $SUBNET..."
    docker network create --subnet="$SUBNET" "$NETWORK_NAME" || {
        echo "✗ Failed to create network"
        exit 1
    }
    echo "✓ Network created successfully"
fi

