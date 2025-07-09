# focusproxy_server.py

import asyncio
import json
import logging
import multiprocessing
import os
import shlex
import sys
import time
from logging.handlers import RotatingFileHandler
from typing import List, Set

try:
    from fastmcp import FastMCP
    import uvloop
except ImportError as e:
    missing_module = str(e).split("'")[1]
    print(f"Error: Required library '{missing_module}' not installed.", file=sys.stderr)
    print("Please install dependencies: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

import config

# --- 1. Logging Setup ---
logger = logging.getLogger("BlockerServerLogger")
logger.setLevel(logging.INFO)
if logger.hasHandlers():
    logger.handlers.clear()

# File handler for persistent logs
file_handler = RotatingFileHandler(config.LOG_FILE, maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')
file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(file_formatter)
logger.addHandler(file_handler)

# Console handler for real-time output (e.g., when running directly or via systemd logs)
console_handler = logging.StreamHandler(sys.stdout)
console_formatter = logging.Formatter('%(levelname)s: %(message)s')
console_handler.setFormatter(console_formatter)
logger.addHandler(console_handler)


# --- 2. Proxy Worker Logic ---
BLOCKING_ACTIVE = False
SITES_TO_BLOCK: Set[bytes] = set()
BUFFER_SIZE = 65536
WORKER_PIDS: List[int] = [] # This will be populated in the main scope

async def update_block_status_and_list():
    """
    Periodically checks control files to update the blocking status and site list for this worker.
    """
    global BLOCKING_ACTIVE, SITES_TO_BLOCK
    
    while True:
        try:
            is_currently_active = False
            try:
                with open(config.CONTROL_FILE_PATH, 'r') as f:
                    end_time_str = f.read().strip()
                    # A value of -1 means the lock is indefinite.
                    if end_time_str == '-1':
                        is_currently_active = True
                    else:
                        end_time = float(end_time_str)
                        if time.time() < end_time:
                            is_currently_active = True
                        else:
                            # Clean up expired lock file
                            os.remove(config.CONTROL_FILE_PATH)
            except (FileNotFoundError, ValueError):
                pass # Lock file does not exist or is invalid, so blocking is not active.

            if is_currently_active != BLOCKING_ACTIVE:
                BLOCKING_ACTIVE = is_currently_active
                status_str = "ENABLED" if BLOCKING_ACTIVE else "DISABLED"
                # Log status change only from the first worker to avoid log spam
                if os.getpid() == WORKER_PIDS[0]:
                    logger.info(f"Blocking status changed to: {status_str}")

            if BLOCKING_ACTIVE:
                with open(config.SITES_STORAGE_FILE, 'r', encoding='utf-8') as f:
                    sites_list = json.load(f)
                    sites_bytes = {s.encode('utf-8') for s in sites_list}
                    if sites_bytes != SITES_TO_BLOCK:
                        SITES_TO_BLOCK = sites_bytes
                        if os.getpid() == WORKER_PIDS[0]:
                            logger.info(f"Blocklist reloaded. Total sites: {len(SITES_TO_BLOCK)}")
        except FileNotFoundError:
             if BLOCKING_ACTIVE:
                 BLOCKING_ACTIVE = False
                 SITES_TO_BLOCK.clear()
                 if os.getpid() == WORKER_PIDS[0]:
                    logger.warning("Sites file not found, disabling block.")
        except Exception as e:
            logger.error(f"Worker (pid: {os.getpid()}) error in update loop: {e}")

        await asyncio.sleep(5)


async def pipe_data(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Reads from a reader and writes to a writer until EOF or error."""
    try:
        while not reader.at_eof():
            data = await reader.read(BUFFER_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
        pass # Common network errors, ignore quietly
    finally:
        writer.close()


async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
    """Handles an incoming client connection to the proxy."""
    server_writer = None
    try:
        # Read the initial request headers
        headers = await asyncio.wait_for(client_reader.readuntil(b'\r\n\r\n'), timeout=5.0)
        first_line = headers.split(b'\n', 1)[0]
        parts = first_line.split(b' ')
        if len(parts) < 3:
            return # Malformed request
        
        method, target, _ = parts
        
        # Core blocking logic
        if BLOCKING_ACTIVE and any(site in target for site in SITES_TO_BLOCK):
            #logger.debug(f"Blocking request for target: {target.decode(errors='ignore')}")
            client_writer.close()
            return

        # Determine destination host and port
        if method == b'CONNECT': # HTTPS traffic
            host, port_str = target.split(b':')
            port = int(port_str)
        else: # HTTP traffic
            host_header = next((line for line in headers.split(b'\r\n') if line.lower().startswith(b'host:')), None)
            if not host_header:
                return
            host = host_header.split(b' ', 1)[1]
            port = 80
        
        # Establish connection to the target server
        server_reader, server_writer = await asyncio.open_connection(host, port)
        
        # Finalize connection setup with client
        if method == b'CONNECT':
            client_writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            await client_writer.drain()
        else:
            server_writer.write(headers)
            await server_writer.drain()

        # Create two-way pipes for data transfer
        pipe1 = asyncio.create_task(pipe_data(client_reader, server_writer))
        pipe2 = asyncio.create_task(pipe_data(server_reader, client_writer))
        await asyncio.gather(pipe1, pipe2)

    except (asyncio.TimeoutError, ConnectionRefusedError, OSError):
        pass # Common network errors
    except Exception:
        # For unexpected errors, you might want to log them, but be careful as it can be noisy.
        # logger.exception(f"Unexpected error in handle_client")
        pass
    finally:
        client_writer.close()
        if server_writer:
            server_writer.close()


async def main_async_worker():
    """Main async function for a single proxy worker process."""
    asyncio.create_task(update_block_status_and_list())
    server = await asyncio.start_server(
        handle_client, config.PROXY_HOST, config.PROXY_PORT, reuse_port=True
    )
    if os.getpid() == WORKER_PIDS[0]:
        logger.info(f"Proxy server started on {config.PROXY_HOST}:{config.PROXY_PORT} in {len(WORKER_PIDS)} processes.")
    
    async with server:
        await server.serve_forever()


def run_proxy_worker():
    """Entry point for each proxy worker process."""
    uvloop.install()
    asyncio.run(main_async_worker())


# --- 3. MCP Control Server Logic ---
WORKER_PROCESSES: List[multiprocessing.Process] = []

mcp = FastMCP(
    name="FocusProxyControl",
    instructions="This server helps the user focus by blocking distracting websites for a set duration."
)


def _get_persistent_sites() -> Set[str]:
    """Reads and returns the saved list of sites from the JSON file."""
    if not os.path.exists(config.SITES_STORAGE_FILE):
        return set()
    try:
        with open(config.SITES_STORAGE_FILE, 'r', encoding='utf-8') as f:
            return set(json.load(f))
    except (json.JSONDecodeError, IOError):
        return set()


def _save_persistent_sites(sites: Set[str]):
    """Saves the list of sites to the JSON file."""
    try:
        with open(config.SITES_STORAGE_FILE, 'w', encoding='utf-8') as f:
            json.dump(sorted(list(sites)), f, indent=2)
    except IOError as e:
        logger.error(f"Failed to save sites file: {e}")


def _parse_sites_from_string(sites_str: str) -> Set[str]:
    """Reliably parses a string of sites (space or comma-separated) into a set."""
    if not sites_str:
        return set()
    return {
        site.strip().replace(',', '') 
        for site in shlex.split(sites_str.replace(',', ' ')) 
        if site.strip()
    }


def _start_workers():
    """Starts the proxy worker processes if they are not already running."""
    if WORKER_PROCESSES:
        return
    logger.info(f"Starting {config.NUM_WORKERS} proxy workers...")
    
    # Clear old PIDs and start fresh
    WORKER_PIDS.clear() 
    
    for _ in range(config.NUM_WORKERS):
        process = multiprocessing.Process(target=run_proxy_worker, daemon=True)
        WORKER_PROCESSES.append(process)
        process.start()
        WORKER_PIDS.append(process.pid)
    logger.info(f"Workers started with PIDs: {WORKER_PIDS}")


def _stop_workers():
    """Stops all running proxy worker processes."""
    if not WORKER_PROCESSES:
        return
    logger.info("Stopping proxy workers...")
    for process in WORKER_PROCESSES:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2) # Wait for graceful termination
    WORKER_PROCESSES.clear()
    WORKER_PIDS.clear()
    logger.info("All workers stopped.")


def _get_status_message() -> str:
    """Generates a comprehensive status message."""
    sites = _get_persistent_sites()
    sites_str = ", ".join(sorted(list(sites))) if sites else "is empty"
    
    try:
        with open(config.CONTROL_FILE_PATH, 'r') as f:
            end_time_str = f.read().strip()
            if end_time_str == '-1':
                 status = "ENABLED INDEFINITELY"
                 time_left_str = "until stopped manually."
            else:
                end_time = float(end_time_str)
                time_left_sec = end_time - time.time()
                if time_left_sec > 0:
                    status = "ENABLED"
                    time_left_str = f"for another {int(time_left_sec / 60)} minutes."
                else:
                    status = "DISABLED (timer expired)"
                    time_left_str = ""
    except (FileNotFoundError, ValueError):
        status = "DISABLED"
        time_left_str = ""
    
    return f"Focus mode is currently {status} {time_left_str}\nThe current persistent blocklist {sites_str}."


@mcp.tool
def block_sites(sites_str: str, duration_minutes: int = -1):
    """
    Adds new sites to the blocklist AND enables focus mode. This is the primary tool for starting a session.
    Example: block_sites(sites_str="youtube.com, reddit.com", duration_minutes=60)

    Args:
        sites_str: A comma or space-separated string of sites to add and block.
        duration_minutes: The duration in minutes for the block. Use -1 for an indefinite block.
    """
    logger.info(f"Tool 'block_sites' called with sites: '{sites_str}', duration: {duration_minutes} min.")
    
    # 1. Add sites to the persistent list
    current_sites = _get_persistent_sites()
    new_sites_to_add = _parse_sites_from_string(sites_str)
    current_sites.update(new_sites_to_add)
    _save_persistent_sites(current_sites)
    logger.info(f"Sites added to persistent list via block_sites: {new_sites_to_add}")

    # 2. Enable focus mode by creating the control file
    end_time = -1 if duration_minutes == -1 else time.time() + duration_minutes * 60
    time_str = "indefinitely" if duration_minutes == -1 else f"for {duration_minutes} minutes"
    with open(config.CONTROL_FILE_PATH, 'w') as f:
        f.write(str(end_time))
    
    _start_workers() # Ensure workers are running
    logger.info(f"Focus mode enabled {time_str}.")
    return f"OK. Sites added and focus mode is now enabled {time_str}. " + _get_status_message()


@mcp.tool
def enable_focus_mode(duration_minutes: int = -1):
    """
    Enables focus mode for the SITES ALREADY IN THE LIST. Does not add new sites.
    Use this to start a session with your pre-configured list.
    
    Args:
        duration_minutes: The duration in minutes for the block. Use -1 for an indefinite block.
    """
    sites = _get_persistent_sites()
    if not sites:
        return "Error: The blocklist is empty. Cannot enable focus mode. Use 'block_sites' to add sites first."

    end_time = -1 if duration_minutes == -1 else time.time() + duration_minutes * 60
    time_str = "indefinitely" if duration_minutes == -1 else f"for {duration_minutes} minutes"
    with open(config.CONTROL_FILE_PATH, 'w') as f:
        f.write(str(end_time))
    _start_workers()
    logger.info(f"Focus mode enabled {time_str}.")
    return f"Focus mode is now enabled {time_str}. " + _get_status_message()
    
@mcp.tool
def disable_focus_mode() -> str:
    """
    Disables focus mode immediately by removing the control file.
    The list of sites remains saved for future use.
    """
    try:
        if os.path.exists(config.CONTROL_FILE_PATH):
            os.remove(config.CONTROL_FILE_PATH)
            logger.info("Focus mode disabled by user command.")
            # Note: workers will stop by themselves after the update loop sees the file is gone.
            # We can also explicitly stop them if desired, but letting them self-disable is fine.
            return "Focus mode has been disabled. " + _get_status_message()
        else:
            return "Focus mode was not active. " + _get_status_message()
    except OSError as e:
        logger.error(f"Error removing control file: {e}")
        return f"Error disabling focus mode: {e}"


@mcp.tool
def add_sites_to_list(sites_to_add_str: str) -> str:
    """
    ONLY adds sites to the permanent list. Does NOT enable or disable focus mode.
    Use this to configure your list without starting a blocking session.
    
    Args:
        sites_to_add_str: A comma or space-separated string of domain names to add.
    """
    current_sites = _get_persistent_sites()
    new_sites = _parse_sites_from_string(sites_to_add_str)
    if not new_sites:
        return "No valid sites provided to add. " + _get_status_message()
    current_sites.update(new_sites)
    _save_persistent_sites(current_sites)
    logger.info(f"Sites added to persistent list: {new_sites}")
    return "Sites have been added to the list. " + _get_status_message()


@mcp.tool
def remove_sites_from_list(sites_to_remove_str: str) -> str:
    """
    Permanently removes sites from the blocklist.
    
    Args:
        sites_to_remove_str: A comma or space-separated string of domain names to remove.
    """
    current_sites = _get_persistent_sites()
    sites_to_remove = _parse_sites_from_string(sites_to_remove_str)
    if not sites_to_remove:
        return "No valid sites provided to remove. " + _get_status_message()
    current_sites.difference_update(sites_to_remove)
    _save_persistent_sites(current_sites)
    logger.info(f"Sites removed from persistent list: {sites_to_remove}")
    return "Sites have been removed from the list. " + _get_status_message()


@mcp.tool
def get_current_status() -> str:
    """Returns the current state of the focus mode and the full blocklist."""
    return _get_status_message()


# --- 4. Server Execution ---
def _cleanup_on_shutdown(signum=None, frame=None):
    logger.info("Shutdown signal received. Cleaning up.")
    _stop_workers()
    # Also remove the lock file on a clean shutdown if it exists
    if os.path.exists(config.CONTROL_FILE_PATH):
        try:
            os.remove(config.CONTROL_FILE_PATH)
            logger.info("Removed active session lock file on shutdown.")
        except OSError:
            pass
    logger.info("Shutdown complete.")
    sys.exit(0)


if __name__ == "__main__":
    logger.info(f"Starting Focus Mode Control Server on http://{config.MCP_HOST}:{config.MCP_PORT}/mcp/")
    
    # Ensure a sites file exists on first run
    if not os.path.exists(config.SITES_STORAGE_FILE):
        _save_persistent_sites(set())
        logger.info(f"Created empty sites storage file at: {config.SITES_STORAGE_FILE}")

    # On startup, if a lock file exists, it means we're resuming a session (e.g., after a crash/reboot).
    # So, we should start the workers immediately.
    if os.path.exists(config.CONTROL_FILE_PATH):
        logger.warning("Active session file found on startup. Starting proxy workers immediately.")
        _start_workers()

    # The MCP server runs in the main process.
    # It will block here until it's stopped.
    try:
        mcp.run(transport="http", host=config.MCP_HOST, port=config.MCP_PORT)
    except (KeyboardInterrupt, SystemExit):
        pass # The finally block will handle cleanup
    finally:
        _cleanup_on_shutdown()