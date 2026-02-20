FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install required dependencies
RUN apt-get update && \
    apt-get install -y curl zstd && \
    rm -rf /var/lib/apt/lists/*

# Install Ollama
RUN curl -fsSL https://ollama.com/install.sh | sh

# Pull lightweight phi model at build time
RUN ollama serve & \
    sleep 5 && \
    ollama pull phi && \
    pkill ollama

# Railway injects PORT dynamically
ENV PORT=11434

EXPOSE 11434

# Start Ollama using Railway dynamic port
CMD ["sh", "-c", "ollama serve --port ${PORT}"]