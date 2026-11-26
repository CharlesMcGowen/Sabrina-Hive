FROM mcr.microsoft.com/playwright:v1.65.1-noble

USER root
RUN python3 --version \
    && npm install --prefix /opt/browser-tools --ignore-scripts --no-audit --no-fund --save-exact \
        agent-browser@0.45.0 @playwright/cli@0.1.25 \
    && /opt/browser-tools/node_modules/.bin/agent-browser --version \
    && /opt/browser-tools/node_modules/.bin/playwright-cli --version \
    && chromium="$(find /ms-playwright -path '*/chrome-linux64/chrome' -print -quit)" \
    && test -x "$chromium" \
    && ln -s "$chromium" /usr/local/bin/sabrina-chromium

COPY __init__.py protocol.py worker.py /opt/agent_browser_service/
ENV PYTHONPATH=/opt HOME=/tmp PATH="/opt/browser-tools/node_modules/.bin:${PATH}"
USER pwuser
WORKDIR /tmp
ENTRYPOINT ["timeout", "--kill-after=5s", "15m", "python3", "-u", "-m", "agent_browser_service.worker"]
