FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY gmail_auth.py gmail_client.py server.py ./
COPY token_cache.json /root/.gmail-mcp/token_cache.json

RUN pip install --no-cache-dir fastmcp mcp

# Token cache location; override with GMAIL_TOKEN_DIR to persist elsewhere.
ENV GMAIL_TOKEN_DIR=/root/.gmail-mcp

# Cloud Run injects $PORT (default 8080); serve MCP over streamable-http.
EXPOSE 8080
CMD ["python", "server.py", "--transport", "streamable-http", "--host", "0.0.0.0"]
