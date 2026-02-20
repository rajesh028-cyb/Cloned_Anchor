#!/bin/bash
set -e

# Start Ollama in the background
ollama serve &

# Wait for Ollama to be ready
sleep 5

# Pre-pull the model so it's ready for Turn 1
ollama pull "${OLLAMA_MODEL:-phi}"

# Start Flask API via gunicorn
exec gunicorn anchor_api_server:app --bind "0.0.0.0:${PORT:-8080}" --workers 2 --timeout 120