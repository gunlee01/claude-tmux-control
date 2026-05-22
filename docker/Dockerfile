FROM node:22-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    ca-certificates \
    git \
    procps \
    python3 \
    python3-pip \
    python3-venv \
    ripgrep \
    tmux \
  && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

COPY . /opt/claude-tmux-control
RUN python3 -m venv /opt/ctc-venv \
  && /opt/ctc-venv/bin/pip install --no-cache-dir /opt/claude-tmux-control \
  && ln -s /opt/ctc-venv/bin/ctc /usr/local/bin/ctc \
  && ln -s /opt/ctc-venv/bin/claude-tmux-control /usr/local/bin/claude-tmux-control

COPY docker/entrypoint.sh /usr/local/bin/ctc-docker-entrypoint
RUN chmod 0755 /usr/local/bin/ctc-docker-entrypoint \
  && useradd -m -s /bin/bash ctc \
  && chown -R ctc:ctc /home/ctc

USER ctc
WORKDIR /repo

ENTRYPOINT ["ctc-docker-entrypoint"]
CMD ["ctc", "--help"]
