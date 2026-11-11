#!/usr/bin/env sh
set -euo pipefail

echo "[ghost-suite] initializing..."

mkdir -p /app/data /app/keys /app/logs /app/media /app/tmp || true
chmod 700 /app/keys || true

# Generate encryption key if missing
if [ ! -f /app/keys/master.key ]; then
  echo "[ghost-suite] generating master key..."
  python3 -c "from cryptography.fernet import Fernet; open('/app/keys/master.key','wb').write(Fernet.generate_key())" || true
fi

# Start the dashboard / API server with Gunicorn (production WSGI server).
# Flask's built-in dev server is NOT used in production.
echo "[ghost-suite] starting ghost server with gunicorn..."
cd /app
exec gunicorn \
  --bind 0.0.0.0:${GHOST_PORT:-5000} \
  --workers 2 \
  --timeout 120 \
  --graceful-timeout 30 \
  --access-logfile - \
  --error-logfile - \
  src.ghost_server:app
