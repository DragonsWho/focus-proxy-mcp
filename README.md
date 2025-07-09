# focus-proxy-mcp

FocusProxy is a simple, high-performance local HTTP/HTTPS proxy server designed to help you focus by blocking distracting websites. The server is controlled via a simple HTTP API, making it easy to integrate with scripts or command-line tools.

It uses `asyncio` with `uvloop` and multiple processes for high-performance proxying, and `FastMCP` to provide a clean, tool-based API for control.

## Features

-   **High-Performance Proxy**: Built with Python's `asyncio` and accelerated by `uvloop`.
-   **Multi-Process**: Spawns multiple worker processes to handle concurrent connections efficiently.
-   **API Controlled**: Use simple `curl` commands or any HTTP client to control the blocker.
-   **Persistent Blocklist**: Your list of sites to block is saved in a `json` file.
-   **Timed and Indefinite Blocking**: Block sites for a specific duration or until you manually disable it.
-   **Systemd Service**: Includes a `systemd` service file for running the server as a background service on Linux.

## Installation

1.  **Clone the Repository**
    (Assuming you named your repository `focus-proxy`)
    ```bash
    git clone <your-repo-url>
    cd focus-proxy
    ```

2.  **Create and Activate a Virtual Environment**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Install Dependencies**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Review Configuration**
    Open `config.py` to review the default ports and file paths. You probably won't need to change anything if you run it locally.

## Running as a Systemd Service (Linux)

This is the recommended way to run the server in the background and have it start on boot.

1.  **Verify Paths in the Service File**:
    The provided `focusproxy.service` file is configured for a specific user (`dw`) and path (`/home/dw/Documents/Coding/focus-proxy/`). If your username or project path is different, **you must edit the `User`, `Group`, `WorkingDirectory`, and `ExecStart` lines** in the `focusproxy.service` file to match your setup.

2.  **Install the Service**:
    Copy the service file to the systemd directory.
    ```bash
    sudo cp focusproxy.service /etc/systemd/system/
    ```

3.  **Reload Systemd, Enable, and Start the Service**:
    ```bash
    # Reload the systemd manager configuration
    sudo systemctl daemon-reload

    # Enable the service to start on boot
    sudo systemctl enable focusproxy.service

    # Start the service immediately
    sudo systemctl start focusproxy.service
    ```

4.  **Check the Service Status**:
    ```bash
    sudo systemctl status focusproxy.service
    ```
    You can also view its logs in real-time:
    ```bash
    journalctl -u focusproxy.service -f
    ```

## Usage (API via `curl`)

Once the server is running, you control it by sending JSON-RPC requests to the MCP server (default: `http://127.0.0.1:8003/mcp/`).

#### **Block Sites for 60 Minutes**
This adds sites to your list and immediately starts blocking.
```bash
curl -X POST http://127.0.0.1:8003/mcp/ \
-H "Content-Type: application/json" \
-d '{
    "jsonrpc": "2.0",
    "method": "block_sites",
    "params": {
        "sites_str": "youtube.com, reddit.com, twitter.com",
        "duration_minutes": 60
    },
    "id": 1
}'
```

#### **Enable Indefinite Blocking**
This enables blocking for the sites *already in your list* until you manually stop it.
```bash
curl -X POST http://127.0.0.1:8003/mcp/ \
-H "Content-Type: application/json" \
-d '{
    "jsonrpc": "2.0",
    "method": "enable_focus_mode",
    "params": {"duration_minutes": -1},
    "id": 1
}'
```

#### **Disable Blocking**
```bash
curl -X POST http://127.0.0.1:8003/mcp/ \
-H "Content-Type: application/json" \
-d '{"jsonrpc": "2.0", "method": "disable_focus_mode", "params": {}, "id": 1}'
```

#### **Add Sites to the List (without starting a session)**
```bash
curl -X POST http://127.0.0.1:8003/mcp/ \
-H "Content-Type: application/json" \
-d '{
    "jsonrpc": "2.0",
    "method": "add_sites_to_list",
    "params": {"sites_to_add_str": "instagram.com"},
    "id": 1
}'
```

#### **Remove Sites from the List**
```bash
curl -X POST http://127.0.0.1:8003/mcp/ \
-H "Content-Type: application/json" \
-d '{
    "jsonrpc": "2.0",
    "method": "remove_sites_from_list",
    "params": {"sites_to_remove_str": "twitter.com"},
    "id": 1
}'
```

#### **Get Current Status**
```bash
curl -X POST http://127.0.0.1:8003/mcp/ \
-H "Content-Type: application/json" \
-d '{"jsonrpc": "2.0", "method": "get_current_status", "params": {}, "id": 1}'
```

## Configuring Your System to Use the Proxy

For the blocking to work, you must configure your operating system or web browser to use the local proxy.

-   **Address**: `127.0.0.1`
-   **Port**: `8080` (or whatever you set `PROXY_PORT` to in `config.py`)

You can usually find these settings in your system's Network Settings.