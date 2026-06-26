#!/usr/bin/env python3
"""Save miner params locally and rsync to remote miner servers."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import sys
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MINER_PARAMS_DIR = os.path.join(PROJECT_ROOT, "79Miners", "params")
MINER_PARAMS_REMOTE_DIR = os.getenv("MINER_PARAMS_REMOTE_DIR", "/root/79/sn-79")
MINER_PARAMS_RSYNC_USER = os.getenv("MINER_PARAMS_RSYNC_USER", "root")
MINER_PARAMS_RSYNC_SSH_OPTS = os.getenv(
    "MINER_PARAMS_RSYNC_SSH_OPTS",
    "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15",
)
DEFAULT_NETUID = 79


def params_path(uid: int) -> str:
    return os.path.join(MINER_PARAMS_DIR, f"run_miner_{uid}.json")


def load_params(uid: int) -> Optional[dict]:
    path = params_path(uid)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read params for uid=%s: %s", uid, exc)
        return None


def save_params(uid: int, data: dict) -> str:
    os.makedirs(MINER_PARAMS_DIR, exist_ok=True)
    path = params_path(uid)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)
    return path


def fetch_uid_to_ip(netuid: int = DEFAULT_NETUID) -> Dict[str, str]:
    """Load {uid: axon_ip} from metagraph subprocess."""
    script = os.path.join(PROJECT_ROOT, "load_metagraph.py")
    if not os.path.isfile(script):
        return {}
    try:
        proc = subprocess.run(
            [sys.executable, script, str(netuid)],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Metagraph IP lookup failed: %s", exc)
        return {}
    if proc.returncode != 0:
        logger.warning("Metagraph IP lookup exit %s: %s", proc.returncode, proc.stderr.strip())
        return {}
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    uid_to_ip = payload.get("uidToIp") or {}
    return {str(k): v for k, v in uid_to_ip.items() if v}


def resolve_miner_ip(uid: int, data: dict, uid_to_ip: Optional[Dict[str, str]] = None) -> str:
    """Prefer on-chain axon IP; update data['miner_ip'] when found."""
    ip = ""
    if uid_to_ip:
        ip = (uid_to_ip.get(str(uid)) or uid_to_ip.get(str(int(uid))) or "").strip()
    if not ip:
        stored = data.get("miner_ip")
        if isinstance(stored, str):
            ip = stored.strip()
    if ip:
        data["miner_ip"] = ip
    return ip


def rsync_params(uid: int, local_path: str, miner_ip: str) -> tuple[bool, str]:
    if not miner_ip:
        return False, "miner_ip missing"
    remote = f"{MINER_PARAMS_RSYNC_USER}@{miner_ip}:{MINER_PARAMS_REMOTE_DIR}/run_miner_{uid}.json"
    ssh_cmd = f"ssh {MINER_PARAMS_RSYNC_SSH_OPTS}"
    cmd = ["rsync", "-avz", "-e", ssh_cmd, local_path, remote]
    logger.info("Rsync miner params: %s", " ".join(shlex.quote(part) for part in cmd))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError:
        return False, "rsync command not found"
    except subprocess.TimeoutExpired:
        return False, "rsync timed out after 60 seconds"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return False, err or f"rsync failed with exit code {proc.returncode}"
    return True, (proc.stdout or "").strip() or f"Synced to {remote}"


def save_and_sync_params(
    uid: int,
    data: dict,
    uid_to_ip: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Write local JSON and rsync to the miner's server."""
    miner_ip = resolve_miner_ip(uid, data, uid_to_ip)
    local_path = save_params(uid, data)
    rsync_ok, rsync_message = rsync_params(uid, local_path, miner_ip)
    return {
        "uid": uid,
        "saved_locally": True,
        "local_path": local_path,
        "miner_ip": miner_ip,
        "rsynced": rsync_ok,
        "rsync_message": rsync_message,
        "remote_path": f"{MINER_PARAMS_REMOTE_DIR}/run_miner_{uid}.json",
    }
