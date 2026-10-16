# Cadence backend in a container — for the Step 7 host deployment.
#
# The backend is pure stdlib Python, so there is nothing to `pip install` and the
# image stays tiny. That is the whole reason "installing" this project is one file.
#
# Build from the repo root:
#     docker build -t cadence-backend .
#
# The image deliberately contains NO .env and no student data: the key and the
# memory files arrive at runtime (environment + mounted volumes), which is the
# same split the local version keeps.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CADENCE_HOST=0.0.0.0 \
    CADENCE_PORT=8787

WORKDIR /app

# Only the files the server reads at runtime. No extension/, no spike/, no tests.
COPY config/ ./config/
COPY skills/ ./skills/
COPY backend/ ./backend/

# The writable directories, created empty and declared as volumes so a host can
# mount persistent storage over them. memory/ and progress/ are the student's
# data; backend/logs/ is the action log. Losing any of them is not fatal - the
# server recreates the directory - but a deploy without a volume loses the
# student's notes on every restart.
RUN mkdir -p /app/memory /app/progress /app/backend/logs
VOLUME ["/app/memory", "/app/progress", "/app/backend/logs"]

EXPOSE 8787

# Exec form, no shell: the server is PID 1 and receives the platform's stop
# signal directly instead of a wrapper eating it.
CMD ["python3", "backend/server.py"]
