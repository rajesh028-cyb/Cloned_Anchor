FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install curl
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

# Pull lightweight phi model at build time
RUN ollama serve & \
    sleep 5 && \
    ollama pull phi && \
    pkill ollama

# Railway injects PORT dynamically
ENV PORT=11434

EXPOSE ${PORT}

# Start Ollama on Railway's dynamic PORT
CMD ollama serve --port ${PORT}