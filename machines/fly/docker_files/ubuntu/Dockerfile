FROM ubuntu:jammy

RUN apt-get update && apt-get install --no-install-recommends -y \
    ca-certificates curl sudo openssh-server bash git \
    iproute2 apt-transport-https gnupg-agent software-properties-common && \
    apt-get autoremove -y

# Create the user
ARG USER=admin
RUN test -n "${USER}"

# Create your user
RUN adduser --disabled-password --gecos '' --home /data/home ${USER}
# passwordless sudo for your user's group
RUN echo "%${USER} ALL=(ALL) NOPASSWD: ALL" >> /etc/sudoers

ENV USER_ARG=${USER}

# Install Docker and add user to docker group
RUN curl -fsSL https://get.docker.com -o get-docker.sh \
    && sh get-docker.sh \
    && usermod -aG docker ${USER}

# Setup the SSH server daemon, copy pre-generated keys
RUN rm -rf /etc/ssh/ssh_host_*_key*
COPY ./fly/docker_files/ubuntu/etc/ssh/sshd_config /etc/ssh/sshd_config

COPY ./fly/docker_files/ubuntu/entrypoint ./entrypoint
COPY ./fly/docker_files/ubuntu/docker-entrypoint.d/* ./docker-entrypoint.d/

# expose the ssh port, we wet to 2222 in the sshd_config
EXPOSE 2222

ENTRYPOINT ["./entrypoint"]

CMD ["/usr/sbin/sshd", "-D"]
