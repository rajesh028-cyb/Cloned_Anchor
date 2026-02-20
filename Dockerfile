FROM ollama/ollama:latest

ENV PORT=11434

EXPOSE 11434

CMD ["sh", "-c", "ollama serve --port ${PORT} & sleep 5 && ollama pull phi || true && wait"]