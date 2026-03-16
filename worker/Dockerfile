FROM node:22-slim

USER root

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Engine: Claude Code
RUN npm install -g @anthropic-ai/claude-code && npm cache clean --force

# Engine: OpenCode
RUN curl -fsSL https://opencode.ai/install | bash 2>/dev/null || true

ENV CLAUDE_CODE_DISABLE_TELEMETRY=1
ENV DISABLE_AUTOUPDATER=1

COPY entrypoint.sh /opt/entrypoint.sh
COPY run-job.sh /opt/run-job.sh
COPY parse-job.js /opt/parse-job.js
RUN chmod +x /opt/entrypoint.sh /opt/run-job.sh

RUN echo "agent:x:1000:1000:agent:/home/agent:/bin/sh" >> /etc/passwd \
    && echo "agent:x:1000:" >> /etc/group \
    && mkdir -p /home/agent /output /workspace \
    && chown 1000:1000 /home/agent /output /workspace

WORKDIR /workspace
USER agent

ENTRYPOINT ["/opt/entrypoint.sh"]
