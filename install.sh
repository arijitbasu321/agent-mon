#!/usr/bin/env bash
set -euo pipefail

# agent-mon installer
# Run as root: sudo ./install.sh

CONFIG_DIR="/etc/agent-mon"
LOG_DIR="/var/log"
DATA_DIR="/var/lib/agent-mon"
MEMORY_DIR="${DATA_DIR}/memory"
SERVICE_USER="agent-mon"
ENV_FILE="${CONFIG_DIR}/env"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"

echo "=== agent-mon installer ==="
echo ""

# Must be root
if [[ $EUID -ne 0 ]]; then
    echo "Error: This script must be run as root (sudo ./install.sh)"
    exit 1
fi

# 1. Create service user
if ! id "$SERVICE_USER" &>/dev/null; then
    echo "Creating service user: ${SERVICE_USER}"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
else
    echo "Service user ${SERVICE_USER} already exists"
fi

# Add to docker group if docker is installed
if getent group docker &>/dev/null; then
    usermod -aG docker "$SERVICE_USER" 2>/dev/null || true
    echo "Added ${SERVICE_USER} to docker group"
fi

# 2. Install the package
echo "Installing agent-mon..."
pip install .

# 3. Create directories
echo "Creating directories..."
mkdir -p "$CONFIG_DIR" "$DATA_DIR" "$MEMORY_DIR"
chown "${SERVICE_USER}:${SERVICE_USER}" "$DATA_DIR" "$MEMORY_DIR"

# 4. Copy config (no overwrite)
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "Copying default config to ${CONFIG_FILE}"
    cp config.yaml "$CONFIG_FILE"
    chown "${SERVICE_USER}:${SERVICE_USER}" "$CONFIG_FILE"
else
    echo "Config file already exists at ${CONFIG_FILE}, skipping"
fi

# 5. Set up API keys in env file
if [[ -f "$ENV_FILE" ]]; then
    echo "Environment file already exists at ${ENV_FILE}, skipping"
else
    ANTHROPIC_KEY="${ANTHROPIC_API_KEY:-}"
    RESEND_KEY="${RESEND_API_KEY:-}"

    if [[ -n "$ANTHROPIC_KEY" ]]; then
        echo "Using ANTHROPIC_API_KEY from environment"
    elif [[ -t 0 ]]; then
        # stdin is a terminal — interactive prompt
        echo ""
        echo "--- API Key Setup ---"
        echo "Keys are stored in ${ENV_FILE} (mode 600, readable only by ${SERVICE_USER})."
        echo ""

        while true; do
            echo -n "ANTHROPIC_API_KEY (required): "
            read -rs ANTHROPIC_KEY
            echo ""
            if [[ -n "$ANTHROPIC_KEY" ]]; then
                break
            fi
            echo "  Error: ANTHROPIC_API_KEY cannot be empty. Get one at https://console.anthropic.com/"
        done

        echo -n "RESEND_API_KEY (optional, for email alerts/heartbeat — press Enter to skip): "
        read -rs RESEND_KEY
        echo ""
    else
        # Non-interactive and no env var — fail
        echo "Error: ANTHROPIC_API_KEY is not set and stdin is not a terminal."
        echo "Set it in the environment before running the installer:"
        echo "  ANTHROPIC_API_KEY=sk-ant-... RESEND_API_KEY=re_... sudo -E ./install.sh"
        exit 1
    fi

    cat > "$ENV_FILE" <<EOF
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
RESEND_API_KEY=${RESEND_KEY}
EOF
    chmod 600 "$ENV_FILE"
    chown "${SERVICE_USER}:${SERVICE_USER}" "$ENV_FILE"
    echo "Environment file created at ${ENV_FILE}"
fi

# 6. Install systemd service
echo "Installing systemd service..."
cp agent-mon.service /etc/systemd/system/agent-mon.service
systemctl daemon-reload
systemctl enable agent-mon

echo ""
echo "=== Installation complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit ${CONFIG_FILE} with your settings"
echo "  2. Edit ${ENV_FILE} with your API keys (if not set above)"
echo "  3. Start the service: sudo systemctl start agent-mon"
echo "  4. Check status: sudo systemctl status agent-mon"
echo "  5. View logs: sudo journalctl -u agent-mon -f"
