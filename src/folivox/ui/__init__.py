"""Local web interface (``folivox ui``). Needs the optional extra ``folivox[ui]``."""

from __future__ import annotations


def port_in_use(host: str, port: int) -> bool:
    """True if anything already answers on host:port, including a server bound to all interfaces: macOS lets a
    second server bind 127.0.0.1 next to it, and the browser would then talk to the wrong one."""
    import socket

    for target in (host, "localhost"):
        try:
            with socket.create_connection((target, port), timeout=0.5):
                return True
        except OSError:
            continue
    return False


WILDCARD_HOSTS = ("0.0.0.0", "::", "")


def local_host(host: str) -> str:
    """Where this machine reaches a studio bound to ``host`` (a wildcard bind answers on the loopback)."""
    return "127.0.0.1" if host in WILDCARD_HOSTS else host


def env_token() -> str | None:
    """A token set by the deployment: ``FOLIVOX_UI_TOKEN``, or the file named by ``FOLIVOX_UI_TOKEN_FILE`` (a
    Docker secret). At least 16 characters; shorter values are refused rather than silently weakened."""
    import os
    from pathlib import Path

    value = os.environ.get("FOLIVOX_UI_TOKEN")
    if not value and os.environ.get("FOLIVOX_UI_TOKEN_FILE"):
        value = Path(os.environ["FOLIVOX_UI_TOKEN_FILE"]).read_text(encoding="utf-8")
    if not value:
        return None
    value = value.strip()
    if len(value) < 16:
        raise SystemExit("FOLIVOX_UI_TOKEN must be at least 16 characters (try: openssl rand -base64 24)")
    return value


def ui_token() -> str:
    """The access token: the deployment's (:func:`env_token`), else created once and kept in a private file in the
    user data dir so every run (and every bookmark or old tab) uses the same one."""
    import os
    import secrets
    from pathlib import Path

    from platformdirs import user_data_dir

    if (value := env_token()) is not None:
        return value
    path = Path(user_data_dir("folivox", appauthor=False)) / "ui_token"
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 16:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(24)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(value)
    os.chmod(path, 0o600)
    return value


def read_token() -> str | None:
    """The saved access token, or None if the studio never ran (unlike :func:`ui_token`, creates nothing)."""
    from pathlib import Path

    from platformdirs import user_data_dir

    try:
        if (value := env_token()) is not None:
            return value
    except (OSError, SystemExit):
        return None
    try:
        value = (Path(user_data_dir("folivox", appauthor=False)) / "ui_token").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value if len(value) >= 16 else None


def _call(host: str, port: int, path: str, body: dict | None = None, timeout: float = 2.0) -> dict | None:
    """One request to a studio on host:port with the saved token. The JSON answer (an error answer carries its
    ``status``), or None if nothing studio-like answered."""
    import json
    import urllib.error
    import urllib.request

    token = read_token()
    if token is None:
        return None
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=data, method="GET" if body is None else "POST",
                                 headers={"X-Folivox-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return {**json.loads(e.read()), "status": e.code}
        except ValueError:
            return None
    except (OSError, ValueError):
        return None


def studio_on(host: str, port: int) -> dict | None:
    """What /api/ping says if a Folivox studio (with our token) is on host:port, else None."""
    info = _call(host, port, "/api/ping")
    return info if info and info.get("app") == "folivox-studio" else None


def older_studio(host: str, port: int) -> str | None:
    """A studio that predates /api/ping (it cannot be asked to stop): a description with its pid if we can find
    it, else None. Recognised by answering our token on an old route."""
    if not isinstance(_call(host, port, "/api/settings"), dict):
        return None
    import shutil
    import subprocess

    pid = ""
    if shutil.which("lsof"):
        out = subprocess.run(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True,
                             check=False).stdout.split()  # noqa: S603, S607 — fixed argv
        pid = out[0] if out else ""
    return (f"An older Folivox studio{f' (pid {pid})' if pid else ''} is running on port {port}; it cannot be "
            "stopped remotely. Stop it once with Ctrl+C in its terminal" + (f" or: kill {pid}" if pid else "")
            + ". Newer ones can be replaced with: folivox ui --restart")


def wait_free(host: str, port: int, seconds: float = 15.0) -> bool:
    import time

    end = time.time() + seconds
    while time.time() < end:
        if not port_in_use(host, port):
            return True
        time.sleep(0.2)
    return not port_in_use(host, port)


def stop_running(host: str, port: int, force: bool = False) -> dict:
    """Ask the studio on this port to exit and wait until the port is free. Exits with a message if there is no
    studio, if it is rendering (without ``force``) or if it does not stop."""
    host = local_host(host)
    info = studio_on(host, port)
    if info is None:
        if not port_in_use(host, port):
            raise SystemExit(f"No Folivox studio is running on port {port}.")
        raise SystemExit(older_studio(host, port) or f"Port {port} is used by another program, not by the studio.")
    answer = _call(host, port, "/api/shutdown", {"force": force}) or {}
    if answer.get("status") == 409:
        raise SystemExit(f"The studio on port {port} is rendering now. Wait for it to finish, or stop it anyway "
                         "(the work is saved and continues next time): folivox ui --restart --force")
    if not answer.get("stopping"):
        raise SystemExit(f"The studio on port {port} did not accept the stop request "
                         f"(version {info.get('version', '?')}, pid {info.get('pid', '?')}): stop it with Ctrl+C "
                         "in its terminal or kill that process.")
    if not wait_free(host, port):
        raise SystemExit(f"The studio on port {port} did not stop in time (pid {info.get('pid', '?')}).")
    return info


def run(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True, restart: bool = False,
        force: bool = False, allow_hosts: tuple[str, ...] | list[str] = ()) -> None:
    """``host``: the address to bind; anything but the loopback serves the studio to the network (the token is
    still required). ``allow_hosts``: the names browsers use to reach it, for the DNS-rebinding guard."""
    import threading
    import webbrowser

    import uvicorn

    from folivox.ui.app import create_app

    bind, host = host, local_host(host)
    if port_in_use(host, port):
        info = studio_on(host, port)
        if info is None:
            raise SystemExit(older_studio(host, port) or f"Port {port} is used by another program. "
                             f"Choose another one: folivox ui --port {port + 1}")
        if not restart:
            state = ", rendering now" if info.get("busy") else ""
            raise SystemExit(f"The Folivox studio is already running at http://{host}:{port}/ "
                             f"(version {info.get('version', '?')}{state}). Open it, or replace it with this "
                             "version: folivox ui --restart")
        stop_running(host, port, force)
        print(f"Stopped the previous studio (pid {info.get('pid', '?')}).", flush=True)

    from_env = env_token() is not None
    token = ui_token()
    app = create_app(token=token, port=port, allow_hosts=allow_hosts)
    server = uvicorn.Server(uvicorn.Config(app, host=bind, port=port, log_level="warning"))
    app.state.request_exit = lambda: setattr(server, "should_exit", True)
    url = f"http://{host}:{port}/?token={token}"
    if bind != host or bind not in ("127.0.0.1", "localhost", "::1"):
        names = ", ".join(allow_hosts) or "none: only localhost works, add --allow-host"
        print(f"Folivox UI listening on {bind or '*'}:{port} for the network. Names it answers to: {names}.",
              flush=True)
    if from_env:  # the deployment knows its token: keep it out of the (shared) logs
        url = f"http://{host}:{port}/?token=<FOLIVOX_UI_TOKEN>"
    print(f"Folivox UI: {url}\nOpen this link once: the browser then keeps a cookie and you can use "
          f"http://{host}:{port}/ (bookmark it). Press Ctrl+C to stop.", flush=True)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    server.run()
