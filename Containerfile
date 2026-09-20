FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates curl git jq procps python3 python3-pip \
       python3-venv ripgrep unzip wget \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global pnpm @openai/codex

WORKDIR /workspace
CMD ["bash"]
