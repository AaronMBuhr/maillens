#!/bin/bash
set -e

echo "MailLens starting up..."

# Wait for Ollama to be ready, then pull embedding model if needed
echo "Ensuring embedding model is available..."
python -m backend.setup_models

# Run database migrations
echo "Running database setup..."
python -m backend.storage.init_db

# Start the FastAPI server
echo "Starting MailLens server..."
exec uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers 1
