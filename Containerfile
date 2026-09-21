# This file is copied to the plugin config directory on setup. Edit that copy
# to add project-specific tooling; do not edit an installed plugin checkout.
ARG BASE_IMAGE=node:22-bookworm-slim
FROM ${BASE_IMAGE}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl git jq nodejs npm procps python3 python3-pip \
       python3-venv ripgrep unzip wget \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global pnpm @openai/codex

# Add project-specific system tools and language runtimes below. The default
# boilerplate assumes a Debian/Ubuntu-compatible base with apt-get available.

WORKDIR /workspace
CMD ["bash"]
