FROM python:3.10-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code and tests
COPY auth_setup.py .
COPY bridge_daemon.py .
COPY config.json .
COPY tests/ ./tests/

# Make scripts executable
RUN chmod +x auth_setup.py bridge_daemon.py

# Command to run tests by default (can be overridden in docker-compose.yml)
CMD ["pytest", "-v"]
