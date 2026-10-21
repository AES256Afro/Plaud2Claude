# Plaud2Claude reuses OpenPlaud's image: Node + the Plaud MCP server, the headless Plaud sign-in,
# the Claude scheduled-task sender and the page styles. It adds only its own loop and page.
FROM ghcr.io/aes256afro/openplaud:0.5.0

COPY plaud2claude.py /app/plaud2claude.py

ENV P2C_PORT=8796 \
    P2C_DATA=/data
EXPOSE 8796
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD curl -fsS http://127.0.0.1:8796/healthz || exit 1
CMD ["python3", "/app/plaud2claude.py"]
