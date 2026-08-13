import asyncio
import os
import platform
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

import cv2
import numpy as np
import websockets

LOCAL_PORT = 8765


def print_banner():
    banner = r"""
██████╗ ███████╗███╗   ███╗ ██████╗ ████████╗███████╗        ███████╗██╗   ██╗███████╗
██╔══██╗██╔════╝████╗ ████║██╔═══██╗╚══██╔══╝██╔════╝        ██╔════╝╚██╗ ██╔╝██╔════╝
██████╔╝█████╗  ██╔████╔██║██║   ██║   ██║   █████╗          █████╗   ╚████╔╝ █████╗  
██╔══██╗██╔══╝  ██║╚██╔╝██║██║   ██║   ██║   ██╔══╝          ██╔══╝    ╚██╔╝  ██╔══╝  
██║  ██║███████╗██║ ╚═╝ ██║╚██████╔╝   ██║   ███████╗███████╗███████╗   ██║   ███████╗
╚═╝  ╚═╝╚══════╝╚═╝     ╚═╝ ╚═════╝    ╚═╝   ╚══════╝╚══════╝╚══════╝   ╚═╝   ╚══════╝
[ remote_eye :: live surveillance relay :: r4msolo ]
    """
    print("\033[92m" + banner + "\033[0m")


def extract_ngrok_url(log_output: str):
    if not log_output:
        return None

    for line in log_output.splitlines():
        for match in re.findall(r"https?://[^\s\"']+", line):
            if "ngrok" in match.lower() or ".app" in match.lower() or ".io" in match.lower():
                return match.rstrip(")")

    match = re.search(r"https?://[A-Za-z0-9.-]+(?:\.ngrok(?:-free)?\.app|\.ngrok\.io)", log_output)
    if match:
        return match.group(0)

    return None


def get_ngrok_config_path():
    candidates = []
    if platform.system() == "Windows":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "ngrok" / "ngrok.yml")
        candidates.extend([
            Path.home() / ".ngrok2" / "ngrok.yml",
            Path.home() / "AppData" / "Local" / "ngrok" / "ngrok.yml",
            Path.home() / "AppData" / "Roaming" / "ngrok" / "ngrok.yml",
        ])
    else:
        candidates.extend([
            Path.home() / ".config" / "ngrok" / "ngrok.yml",
            Path.home() / ".ngrok2" / "ngrok.yml",
        ])

    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else Path.home() / ".ngrok2" / "ngrok.yml"


def has_ngrok_auth_token():
    config_path = get_ngrok_config_path()
    if not config_path.exists():
        return False
    try:
        content = config_path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return "authtoken:" in content.lower()


def ensure_ngrok_binary():
    binary_path = shutil.which("ngrok")
    if binary_path:
        return binary_path

    install_dir = Path.home() / ".ngrok"
    install_dir.mkdir(parents=True, exist_ok=True)

    if platform.system() == "Windows":
        binary_name = "ngrok.exe"
        download_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"
        archive_name = "ngrok.zip"
    elif platform.system() == "Linux":
        binary_name = "ngrok"
        download_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz"
        archive_name = "ngrok.tgz"
    else:
        binary_name = "ngrok"
        download_url = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-darwin-amd64.tgz"
        archive_name = "ngrok.tgz"

    downloaded_binary = install_dir / binary_name
    if not downloaded_binary.exists():
        archive_path = install_dir / archive_name
        print("ngrok not found. Downloading...")
        urllib.request.urlretrieve(download_url, archive_path)
        if archive_name.endswith(".zip"):
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(install_dir)
        else:
            with tarfile.open(archive_path) as archive:
                archive.extractall(install_dir)

    if downloaded_binary.exists():
        return str(downloaded_binary)
    for candidate in install_dir.iterdir():
        if candidate.name.lower().startswith("ngrok") and candidate.is_file():
            return str(candidate)
    raise FileNotFoundError("The ngrok binary could not be installed or found.")


def register_ngrok_auth_token(ngrok_binary):
    if has_ngrok_auth_token():
        print("ngrok auth token already configured. Continuing without prompt.")
        return

    token = input("Enter your ngrok auth token: ").strip()
    if not token:
        raise ValueError("The ngrok auth token is required.")

    subprocess.run([ngrok_binary, "authtoken", token], check=True, capture_output=True, text=True)
    print("ngrok auth token configured successfully.")


def start_ngrok_tunnel(ngrok_binary):
    process = subprocess.Popen(
        [ngrok_binary, "http", str(LOCAL_PORT), "--log=stdout"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    deadline = time.monotonic() + 30
    combined_output = ""
    while time.monotonic() < deadline:
        if process.stdout is None:
            break
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                break
            time.sleep(0.2)
            continue
        combined_output += line
        public_url = extract_ngrok_url(combined_output)
        if public_url:
            return process, public_url
        time.sleep(0.2)

    final_url = extract_ngrok_url(combined_output)
    if final_url:
        return process, final_url

    raise RuntimeError("Could not detect the generated ngrok URL.")


async def receive_stream(websocket):
    print("Client connected! Receiving stream...")
    try:
        async for message in websocket:
            np_arr = np.frombuffer(message, dtype=np.uint8)
            frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if frame is not None:
                cv2.imshow("Stream | Remote Eye", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    except websockets.exceptions.ConnectionClosed:
        print("Client disconnected.")
    finally:
        cv2.destroyAllWindows()


async def main():
    print_banner()
    use_ngrok = input("Use ngrok to expose the web port? (Y/n): ").strip().lower() not in {"", "n", "no"}

    if use_ngrok:
        ngrok_binary = ensure_ngrok_binary()
        if not has_ngrok_auth_token():
            register_ngrok_auth_token(ngrok_binary)
        ngrok_process, public_url = start_ngrok_tunnel(ngrok_binary)
        print(f"Local port running: {LOCAL_PORT}")
        print(f"Public ngrok URL: {public_url}")
    else:
        ngrok_process = None
        print(f"Local port running: {LOCAL_PORT}")

    server = await websockets.serve(receive_stream, "0.0.0.0", LOCAL_PORT)
    print(f"WebSocket server is running on port {LOCAL_PORT}. Waiting for connections...")
    try:
        await server.wait_closed()
    finally:
        if ngrok_process is not None and ngrok_process.poll() is None:
            ngrok_process.terminate()
            ngrok_process.wait(timeout=10)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nRemote Eye closed by user.")
    except Exception as exc:
        print(f"Unexpected error: {exc}")
