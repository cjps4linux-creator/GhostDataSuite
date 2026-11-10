#!/usr/bin/env sh
set -euo pipefail

echo "[ghost-suite] initializing..."

mkdir -p /app/data /app/keys /app/logs /app/tmp || true
chmod 700 /app/keys || true

# Generate encryption key if missing
if [ ! -f /app/keys/master.key ]; then
  echo "[ghost-suite] generating master key..."
  python3 -c "from cryptography.fernet import Fernet; open('/app/keys/master.key','wb').write(Fernet.generate_key())" || true
fi

# Generate SSH host keys if possible; tolerate read-only root
mkdir -p /var/run/sshd || true
if [ -w /etc/ssh ]; then
  ssh-keygen -A >/dev/null 2>&1 || true
else
  echo "[ghost-suite] read-only /etc/ssh, skipping host key generation"
fi

# Start sshd only if host keys exist
if [ -f /etc/ssh/ssh_host_rsa_key ]; then
  echo "[ghost-suite] starting sshd..."
  /usr/sbin/sshd || echo "[ghost-suite] sshd failed to start"
else
  echo "[ghost-suite] sshd not started: missing host keys"
fi

# Start the dashboard / API server
echo "[ghost-suite] starting ghost server..."
cd /app
exec python3 -m src.ghost_server
