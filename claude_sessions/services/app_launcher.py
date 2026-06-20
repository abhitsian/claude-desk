"""App launcher — reads .claude-app.json manifests, starts servers, manages processes.

Supports three app types:
- python: runs `pip install -r requirements.txt && python3 {entry}`
- node: runs `npm install && node {entry}`
- static: runs a simple HTTP server on the app directory
"""

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path
from typing import Optional

APPS_DIR = Path.home() / "claude-apps"
PINNED_APPS_FILE = Path.home() / ".claude" / "pinned-apps.json"

# Track running app processes: {app_name: {"process": Popen, "port": int, "pid": int}}
_running_apps: dict = {}


def get_pinned_apps() -> list:
    """Read pinned apps from ~/.claude/pinned-apps.json."""
    if not PINNED_APPS_FILE.exists():
        return []
    try:
        apps = json.loads(PINNED_APPS_FILE.read_text())
        for app in apps:
            slug = app["name"].lower().replace(" ", "-")
            app["dir_name"] = slug
            # Check if running by port
            app["running"] = _is_port_in_use(app.get("port", 0))
        return apps
    except Exception:
        return []


def scan_apps() -> list:
    """Scan ~/claude-apps/ for directories with .claude-app.json manifests."""
    if not APPS_DIR.exists():
        return []

    apps = []
    for d in sorted(APPS_DIR.iterdir()):
        if not d.is_dir():
            continue
        manifest = d / ".claude-app.json"
        if not manifest.exists():
            continue
        try:
            data = json.loads(manifest.read_text())
            data["path"] = str(d)
            data["dir_name"] = d.name
            data["running"] = d.name in _running_apps
            if data["running"]:
                data["pid"] = _running_apps[d.name].get("pid")
                data["port"] = _running_apps[d.name].get("port")
            apps.append(data)
        except (json.JSONDecodeError, KeyError):
            continue

    return apps


def get_app(app_name: str) -> Optional[dict]:
    """Get a single app's manifest."""
    app_dir = APPS_DIR / app_name
    manifest = app_dir / ".claude-app.json"
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text())
        data["path"] = str(app_dir)
        data["dir_name"] = app_name
        data["running"] = app_name in _running_apps
        if data["running"]:
            data["pid"] = _running_apps[app_name].get("pid")
            data["port"] = _running_apps[app_name].get("port")

        # List files in the app directory
        files = []
        for f in sorted(app_dir.rglob("*")):
            if f.is_file() and not any(p in str(f) for p in ["__pycache__", "node_modules", ".git", ".venv"]):
                files.append(str(f.relative_to(app_dir)))
        data["files"] = files

        return data
    except Exception:
        return None


def launch_app(app_name: str) -> dict:
    """Launch an app by name. Checks ~/claude-apps/ and pinned apps. Returns status dict."""
    if app_name in _running_apps:
        info = _running_apps[app_name]
        return {"ok": True, "status": "already_running", "port": info["port"], "url": info.get("url", "")}

    # Check ~/claude-apps/ first
    app_dir = APPS_DIR / app_name
    manifest = app_dir / ".claude-app.json"
    config = None

    if manifest.exists():
        try:
            config = json.loads(manifest.read_text())
        except Exception as e:
            return {"ok": False, "error": f"Bad manifest: {e}"}
    else:
        # Check pinned apps
        pinned = get_pinned_apps()
        slug = app_name
        for p in pinned:
            if p["dir_name"] == slug:
                config = p
                app_dir = Path(p["path"])
                break

    if not config:
        return {"ok": False, "error": f"App '{app_name}' not found in ~/claude-apps/ or pinned apps"}

    # Check if already running by port
    port = config.get("port", 0)
    if port and _is_port_in_use(port):
        url = config.get("url", config.get("open", f"http://localhost:{port}"))
        return {"ok": True, "status": "already_running", "port": port, "url": url}

    app_type = config.get("type", "static")
    entry = config.get("entry", "index.html")
    port = config.get("port") or _find_free_port()
    install_cmd = config.get("install", "")
    url = config.get("open", f"http://localhost:{port}")

    # Replace port placeholder in url
    url = url.replace(str(config.get("port", "")), str(port))
    if "localhost" not in url:
        url = f"http://localhost:{port}"

    # Install dependencies if specified
    if install_cmd:
        try:
            subprocess.run(
                install_cmd, shell=True, cwd=str(app_dir),
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            return {"ok": False, "error": f"Install failed: {e}"}

    # Start the server
    try:
        if app_type == "python":
            process = subprocess.Popen(
                ["python3", entry],
                cwd=str(app_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "PORT": str(port)},
            )
        elif app_type == "node":
            process = subprocess.Popen(
                ["node", entry],
                cwd=str(app_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={**os.environ, "PORT": str(port)},
            )
        elif app_type == "static":
            process = subprocess.Popen(
                ["python3", "-m", "http.server", str(port)],
                cwd=str(app_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:
            return {"ok": False, "error": f"Unknown app type: {app_type}"}

    except Exception as e:
        return {"ok": False, "error": f"Failed to start: {e}"}

    # Wait for the port to be available
    if not _wait_for_port(port, timeout=15):
        # Check if process died
        if process.poll() is not None:
            stderr = process.stderr.read().decode()[:500] if process.stderr else ""
            return {"ok": False, "error": f"Process exited immediately. stderr: {stderr}"}
        return {"ok": False, "error": f"Server started but port {port} not responding after 15s"}

    _running_apps[app_name] = {
        "process": process,
        "port": port,
        "pid": process.pid,
        "url": url,
    }

    return {"ok": True, "status": "launched", "port": port, "url": url, "pid": process.pid}


def stop_app(app_name: str) -> dict:
    """Stop a running app."""
    if app_name not in _running_apps:
        return {"ok": False, "error": "Not running"}

    info = _running_apps[app_name]
    process = info["process"]

    try:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
    except Exception:
        # Force kill by PID
        try:
            os.kill(info["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass

    del _running_apps[app_name]
    return {"ok": True, "status": "stopped"}


def get_running_apps() -> list:
    """Get list of currently running apps."""
    # Clean up dead processes
    dead = []
    for name, info in _running_apps.items():
        if info["process"].poll() is not None:
            dead.append(name)
    for name in dead:
        del _running_apps[name]

    return [
        {"name": name, "port": info["port"], "pid": info["pid"], "url": info.get("url", "")}
        for name, info in _running_apps.items()
    ]


def rebuild_as_app(artifact_path: str, app_name: str) -> dict:
    """Use Claude to restructure an existing artifact into a proper app.

    Launches claude -p to read the artifact and recreate it in ~/claude-apps/{name}/
    with a .claude-app.json manifest.
    """
    artifact = Path(artifact_path)
    if not artifact.exists():
        return {"ok": False, "error": f"File not found: {artifact_path}"}

    content = artifact.read_text()[:10000]  # Cap content size
    app_dir = APPS_DIR / app_name

    prompt = f"""I have an existing artifact that needs to be restructured as a self-contained app.

The original file is at: {artifact_path}
Its content:
---
{content}
---

Restructure this into a proper app at ~/claude-apps/{app_name}/ with:
1. All necessary files (server, frontend, assets)
2. A .claude-app.json manifest
3. requirements.txt or package.json if needed
4. Make it fully functional — if it needs a server, add one

Create all the files now."""

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            return {"ok": False, "error": "Claude failed to rebuild"}

        # Check if the app was created
        if app_dir.exists() and (app_dir / ".claude-app.json").exists():
            return {"ok": True, "app_name": app_name, "path": str(app_dir)}
        else:
            return {"ok": False, "error": "Claude ran but didn't create the app structure. Check ~/claude-apps/"}

    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timed out (180s)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- Helpers ---


def _is_port_in_use(port: int) -> bool:
    """Check if a port is currently in use."""
    if not port:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(('localhost', port))
            return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


def _find_free_port() -> int:
    """Find a free port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]


def _wait_for_port(port: int, timeout: int = 15) -> bool:
    """Wait for a port to be accepting connections."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(('localhost', port))
                return True
        except (ConnectionRefusedError, socket.timeout, OSError):
            time.sleep(0.5)
    return False
