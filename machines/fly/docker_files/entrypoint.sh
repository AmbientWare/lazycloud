#!/bin/bash -e

echo "Starting setup..."

# Create the persistent root directory
mkdir -p /volume/root

# Copy the entire root filesystem if it doesn't exist
if [ ! -f "/volume/root/etc/passwd" ]; then
    echo "Initializing persistent root..."
    # Copy all system directories
    for dir in bin boot etc home lib lib64 opt root sbin usr var; do
        if [ -d "/$dir" ]; then
            echo "Copying /$dir..."
            mkdir -p "/volume/root/$dir"
            cp -a "/$dir"/* "/volume/root/$dir/" 2>/dev/null || true
        fi
    done
fi

# Set up SSH configuration
mkdir -p /volume/root/root/.ssh
chmod 700 /volume/root/root/.ssh

# Handle authorized keys
if [ ! -z "$AUTHORIZED_KEYS" ]; then
    echo "$AUTHORIZED_KEYS" > /volume/root/root/.ssh/authorized_keys
    chmod 600 /volume/root/root/.ssh/authorized_keys
fi

# Handle SSH host keys
mkdir -p /volume/root/etc/ssh
if [ ! -f "/volume/root/etc/ssh/ssh_host_rsa_key" ]; then
    echo "Generating new SSH host keys..."
    ssh-keygen -t rsa -f /volume/root/etc/ssh/ssh_host_rsa_key -N ""
    ssh-keygen -t ecdsa -f /volume/root/etc/ssh/ssh_host_ecdsa_key -N ""
    ssh-keygen -t ed25519 -f /volume/root/etc/ssh/ssh_host_ed25519_key -N ""
    chmod 600 /volume/root/etc/ssh/*_key
else
    echo "Using existing SSH host keys..."
    cp /volume/root/etc/ssh/*_key /etc/ssh/
    chmod 600 /etc/ssh/*_key
fi

# Set proper permissions
chown -R root:root /volume/root
chmod -R 755 /volume/root/bin /volume/root/usr/bin /volume/root/usr/local/bin /volume/root/opt/bin 2>/dev/null || true

# Bind mount the persistent directories over the system directories
for dir in bin boot etc home lib lib64 opt root sbin usr var; do
    if [ -d "/volume/root/$dir" ]; then
        echo "Mounting $dir..."
        mount --bind "/volume/root/$dir" "/$dir"
    fi
done

# Initialize and start Docker daemon
echo "Starting Docker daemon..."
dockerd > /var/log/dockerd.log 2>&1 &
DOCKER_PID=$!

# Wait for Docker daemon to be ready
echo "Waiting for Docker daemon to be ready..."
for i in {1..30}; do
    if docker info >/dev/null 2>&1; then
        echo "Docker daemon is ready"
        break
    fi
    if [ $i -eq 30 ]; then
        echo "Failed to start Docker daemon"
        exit 1
    fi
    sleep 1
done

# Start sshd directly (no chroot needed)
exec /usr/sbin/sshd -D

# If we get here, something went wrong
exit 1
