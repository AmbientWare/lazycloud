FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install required packages
RUN apt-get update \
    && apt-get install -y \
    openssh-server \
    ca-certificates \
    curl \
    gnupg \
    lsb-release \
    procps \
    mount \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives

# install latest docker
RUN curl -fsSL https://get.docker.com -o get-docker.sh
RUN sh get-docker.sh

# Configure SSH
RUN cp /etc/ssh/sshd_config /etc/ssh/sshd_config-original \
    && sed -i 's/^#\s*Port.*/Port 2222/' /etc/ssh/sshd_config \
    && sed -i 's/^#\s*PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config \
    && sed -i 's/^#\s*ListenAddress.*/ListenAddress 0.0.0.0/' /etc/ssh/sshd_config \
    && sed -i 's/^#\s*AddressFamily.*/AddressFamily any/' /etc/ssh/sshd_config \
    && mkdir -p /root/.ssh \
    && chmod 700 /root/.ssh \
    && mkdir /var/run/sshd \
    && chmod 755 /var/run/sshd

# Create initial volume directory
RUN mkdir -p /volume/root

ADD ./fly/docker_files/ubuntu/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 2222

ENTRYPOINT ["/entrypoint.sh"]
