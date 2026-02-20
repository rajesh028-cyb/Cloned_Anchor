FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && \
    apt-get install -y curl zstd && \
    rm -rf /var/lib/apt/lists/*

# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

ENV PORT=11434

EXPOSE 11434

# Pull model at runtime instead of build time
CMD ["sh", "-c", "ollama serve --port ${PORT} & sleep 5 && ollama pull phi && wait"]