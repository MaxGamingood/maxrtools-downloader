#!/bin/bash
set -euo pipefail

WARP_PROXY="socks5h://127.0.0.1:25344"

if [ "${USE_WARP:-0}" = "1" ]; then
    mkdir -p /tmp/warp
    cd /tmp/warp

    # A fresh free WARP identity is created for each container. No personal
    # account or credentials are involved.
    if [ ! -s wgcf-account.toml ]; then
        wgcf register --accept-tos
    fi

    wgcf generate
    printf '\n[Socks5]\nBindAddress = 127.0.0.1:25344\n' >> wgcf-profile.conf

    wireproxy -c wgcf-profile.conf -s &
    WARP_PID=$!

    ready=0
    for _ in $(seq 1 30); do
        if python3 -c "import socket; s=socket.create_connection(('127.0.0.1', 25344), 1); s.close()" 2>/dev/null; then
            ready=1
            break
        fi
        if ! kill -0 "$WARP_PID" 2>/dev/null; then
            echo "wireproxy exited during startup" >&2
            exit 1
        fi
        sleep 1
    done
    if [ "$ready" != "1" ]; then
        echo "WARP SOCKS5 proxy did not become ready" >&2
        exit 1
    fi
    export YTDLP_PROXY="$WARP_PROXY"
    echo "WARP egress ready on 127.0.0.1:25344" >&2
else
    # Home/self-host mode: use the host's own connection, not WARP.
    if [ "${YTDLP_PROXY:-}" = "$WARP_PROXY" ]; then
        export YTDLP_PROXY=""
    fi
fi

cd /app
exec python3 server.py
