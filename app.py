#!/usr/bin/env python3
"""Blockyard local Minecraft server manager. Python standard library only."""
from __future__ import annotations

import hashlib
import concurrent.futures
import errno
import gzip
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("APPDATA", Path.home() / ".config")) / "Blockyard"
CONFIG_FILE = DATA_DIR / "settings.json"
SERVERS_FILE = DATA_DIR / "servers.json"
CLIENT_ONLY_MANIFEST_FILE = DATA_DIR / "client-only-mods.json"
CLIENT_ONLY_LIST_CACHE_FILE = DATA_DIR / "client-only-list-cache.json"
CLIENT_ONLY_LIST_URL = "https://cloudflare-api.bytebuilders.co.za/cache/client-mods/v1/list.json"
CLIENT_ONLY_LIST_SOURCE_URL = "https://github.com/LianJordaan/client-side-mods-list"
CLIENT_ONLY_LIST_SCHEMA_VERSION = 1
CLIENT_ONLY_LIST_REFRESH_SECONDS = 6 * 60 * 60
MODRINTH_BASE = "https://api.modrinth.com/v2"
CURSEFORGE_BASE = "https://api.curseforge.com/v1"
CURSEFORGE_DOWNLOAD_ALLOWED_HOSTS = {
    "edge.forgecdn.net", "media.forgecdn.net", "mediafilez.forgecdn.net",
    "mediafiles.forgecdn.net", "files.forgecdn.net",
}
UA = "BlockyardServerManager/1.0 (local desktop app)"
MRPACK_ALLOWED_HOSTS = {"cdn.modrinth.com", "github.com", "raw.githubusercontent.com", "gitlab.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com"}
# This e4mc release is labeled singleplayer-only by Modrinth, but the
# Fabulously Optimized 26.3 pack index currently marks it server-required.
SERVER_UNSUPPORTED_MODRINTH_VERSIONS = {"RNDvrMAd"}
KNOWN_CLIENT_ONLY_MOD_IDS = {
    "shouldersurfing": "third-person camera mod is client-only",
    "tp_shooting": "TAC: Zero third-person camera integration is client-only",
    "sodium": "rendering and frame-rate optimization mod is client-only",
    "cubes_without_borders": "borderless fullscreen window mod is client-only",
    "immersivedamageindicators": "damage indicator HUD uses client-only Minecraft classes",
    "shadowdrop": "GUI item drop-shadow effects mod is client-only",
    "jei_mekanism_multiblocks": "JEI add-on for Mekanism multiblock pages; CurseForge marks it client-only",
    "jeresources": "JEI add-on for mob drops and world-generation pages; CurseForge marks it client-only",
    "boxlib": "installed version loads Minecraft client Screen on a dedicated server",
    "fancytoasts": "toast UI mod loads Minecraft client Screen on a dedicated server",
    "smart_particles": "particle rendering mod loads Minecraft client Screen on a dedicated server",
    "compactf3plus": "debug HUD mod loads Minecraft client Screen on a dedicated server",
    "coordinatesdisplay": "coordinates HUD mod loads Minecraft client Screen on a dedicated server",
    "shut_up_gl_error": "graphics error UI mod loads Minecraft client KeyMapping on a dedicated server",
    "distraction_free_recipes": "EMI recipe UI add-on loads Minecraft client Screen on a dedicated server",
    "retrodamageindicators": "damage indicator HUD references Minecraft client renderer classes on a dedicated server",
    "accurateblockplacement": "block placement input mod loads Minecraft client KeyMapping on a dedicated server",
}
MAX_PACK_BYTES = 2 * 1024 * 1024 * 1024
MAX_EXPANDED_BYTES = 8 * 1024 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
MAX_SERVER_FILE_UPLOAD_BYTES = 512 * 1024 * 1024
PORT = 8765
_JAVA_INVENTORY = {"checked_at": 0.0, "runtimes": []}

DATA_DIR.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def validate_upstream_client_only_list(data: dict) -> dict | None:
    """Accept only the supported, non-empty schema; ignore unknown confidence/side values."""
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("schemaVersion"), bool):
        return None
    try:
        schema_version = int(data.get("schemaVersion"))
    except (TypeError, ValueError):
        return None
    if schema_version < 1 or schema_version > CLIENT_ONLY_LIST_SCHEMA_VERSION:
        return None
    list_version = str(data.get("listVersion") or "").strip()[:100]
    rules = data.get("rules")
    protected = data.get("neverRemove")
    if (not list_version or not isinstance(rules, list) or not rules or
            not isinstance(protected, list) or not protected):
        return None

    def clean_match(match):
        if not isinstance(match, dict):
            return None
        result = {}
        for platform in ("modrinth", "curseforge"):
            values = match.get(platform)
            if not isinstance(values, dict):
                continue
            part = {}
            for key in ("projectId", "slug"):
                value = values.get(key)
                if isinstance(value, str) and value.strip():
                    part[key] = value.strip()[:200]
            if part:
                result[platform] = part
        filename = match.get("filename")
        if isinstance(filename, dict):
            # Prefix and exact-name matchers are sufficient for the curated
            # dataset's useful coverage and avoid executing remote regexes.
            file_part = {}
            for key in ("equals", "prefix"):
                values = filename.get(key)
                if isinstance(values, list):
                    cleaned = [value.strip().casefold()[:255] for value in values
                               if isinstance(value, str) and value.strip()]
                    if cleaned:
                        file_part[key] = cleaned[:200]
            if file_part:
                result["filename"] = file_part
        return result or None

    clean_rules = []
    for rule in rules[:5000]:
        if not isinstance(rule, dict):
            continue
        side = str(rule.get("sideness") or "").casefold()
        confidence = str(rule.get("confidence") or "").casefold()
        loaders = rule.get("loaders")
        matcher = clean_match(rule.get("match"))
        if (side not in ("client-only", "client-optional") or
                confidence not in ("verified", "high", "heuristic") or
                not isinstance(loaders, list) or not matcher):
            continue
        clean_rules.append({
            "id": str(rule.get("id") or "")[:200],
            "name": str(rule.get("name") or "Unknown mod")[:200],
            "sideness": side,
            "confidence": confidence,
            "loaders": [str(value).casefold() for value in loaders
                        if str(value).casefold() in ("fabric", "forge", "neoforge", "quilt")],
            "reason": str(rule.get("reason") or "Curated client-only mod list")[:500],
            "match": matcher,
        })
    clean_protected = []
    for entry in protected[:2000]:
        if not isinstance(entry, dict):
            continue
        matcher = clean_match(entry.get("match"))
        if matcher:
            clean_protected.append({"name": str(entry.get("name") or "Protected mod")[:200], "match": matcher})
    if not clean_rules or not clean_protected:
        return None
    overrides = data.get("overrides") if isinstance(data.get("overrides"), dict) else {}
    return {
        "schemaVersion": schema_version,
        "listVersion": list_version,
        "updatedAt": str(data.get("updatedAt") or "")[:100],
        "rules": clean_rules,
        "neverRemove": clean_protected,
        "overrides": overrides,
    }


_client_list_cache = load_json(CLIENT_ONLY_LIST_CACHE_FILE, {})
upstream_client_only_list = validate_upstream_client_only_list(
    _client_list_cache.get("data") if isinstance(_client_list_cache, dict) else None
) or {"listVersion": "", "updatedAt": "", "rules": [], "neverRemove": [], "overrides": {}}
try:
    _upstream_client_list_checked_at = float(_client_list_cache.get("checked_at", 0))
except (TypeError, ValueError, AttributeError):
    _upstream_client_list_checked_at = 0.0
if not upstream_client_only_list.get("listVersion"):
    _upstream_client_list_checked_at = 0.0
_upstream_client_list_attempted_at = _upstream_client_list_checked_at
_upstream_client_list_refresh_lock = threading.Lock()


loaded_config = load_json(CONFIG_FILE, {})
config = loaded_config if isinstance(loaded_config, dict) else {}
config.setdefault("default_root", str(Path.home() / "Minecraft Servers"))
config.setdefault("theme_color", "#54a9ff")
if not re.fullmatch(r"#[0-9a-fA-F]{6}", str(config.get("theme_color") or "")):
    config["theme_color"] = "#54a9ff"
_legacy_key_found = "curseforge_key" in config
config.pop("curseforge_key", None)
loaded_servers = load_json(SERVERS_FILE, {})
servers = ({str(key): value for key, value in loaded_servers.items() if isinstance(value, dict)}
           if isinstance(loaded_servers, dict) else {})
client_only_manifest = load_json(CLIENT_ONLY_MANIFEST_FILE, {"version": 1, "mods": []})
if not isinstance(client_only_manifest, dict) or not isinstance(client_only_manifest.get("mods"), list):
    client_only_manifest = {"version": 1, "mods": []}
client_only_manifest["version"] = 1
runtime: dict[str, dict] = {}
state_lock = threading.RLock()
_legacy_server_data_found = False

for _sid, _server in servers.items():
    if not isinstance(_server, dict):
        continue
    _modpack = _server.get("modpack") if isinstance(_server.get("modpack"), dict) else {}
    if _server.get("source") == "curseforge" or _modpack.get("source") == "curseforge":
        _server["source"] = "local"
        _modpack["source"] = "local"
        if _server.get("modpack") is not _modpack:
            _server["modpack"] = _modpack
        for _key in ("curseforge_page_url", "docker_container_name", "container_runtime", "docker_java_version", "docker_image"):
            _server.pop(_key, None)
        _legacy_server_data_found = True
    if _server.pop("curseforge_file_id", None) is not None:
        _legacy_server_data_found = True
    if _server.get("setup_status") == "installing":
        _server["setup_status"] = "error"
        _server["setup_error"] = "Setup was interrupted when Blockyard closed. Review the install folder before starting again."


def persist_config():
    with state_lock:
        saved = dict(config)
        temp = CONFIG_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
        temp.replace(CONFIG_FILE)


def public_config():
    runtimes = detect_java_runtimes()
    java_runtime = runtimes[0] if runtimes else {"path": "", "version": "", "source": ""}
    return {
        "default_root": config.get("default_root"),
        "theme_color": config.get("theme_color", "#54a9ff"),
        "curseforge_api_key_set": bool(str(config.get("curseforge_api_key") or "").strip()),
        "data_folder": str(DATA_DIR),
        "java_runtime": java_runtime,
        "java_runtimes": runtimes,
    }


if _legacy_key_found:
    persist_config()


def _java_candidates(preferred: str = "") -> list[tuple[str, str]]:
    """Return plausible Java executables installed on this host, in preference order."""
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(value, source):
        if not value:
            return
        raw = os.path.expandvars(os.path.expanduser(str(value).strip().strip('"')))
        if not raw:
            return
        located = shutil.which(raw) if not Path(raw).is_absolute() and ("/" not in raw and "\\" not in raw) else raw
        if not located:
            return
        path = Path(located)
        if path.is_dir():
            path = path / ("java.exe" if os.name == "nt" else "java")
        try:
            key = os.path.normcase(str(path.resolve()))
        except OSError:
            key = os.path.normcase(str(path))
        if key not in seen and path.is_file():
            seen.add(key)
            candidates.append((str(path), source))

    add(preferred, "Saved Java executable")
    add(shutil.which("java"), "PATH")
    for variable in ("JAVA_HOME", "JDK_HOME"):
        home = os.environ.get(variable)
        if home:
            add(Path(home) / "bin" / ("java.exe" if os.name == "nt" else "java"), variable)

    if os.name == "nt":
        # The Minecraft Launcher keeps its own runtime under the user's .minecraft folder.
        appdata = os.environ.get("APPDATA")
        if appdata:
            launcher = Path(appdata) / ".minecraft" / "runtime"
            for pattern in ("*/windows-x64/*/bin/java.exe", "*/windows-x64/*/bin/java"):
                try:
                    for match in sorted(launcher.glob(pattern)):
                        add(match, "Minecraft Launcher runtime")
                except OSError:
                    pass

        roots = []
        base_folders = [Path(os.environ[key]) for key in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA") if os.environ.get(key)]
        # Some launchers and packaged Python runtimes start with a reduced
        # environment that omits ProgramFiles; retain standard host fallbacks.
        system_drive = os.environ.get("SystemDrive", "C:")
        base_folders.extend([Path(system_drive) / "Program Files", Path(system_drive) / "Program Files (x86)"])
        local_app_data = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        base_folders.append(Path(local_app_data))
        unique_bases = {os.path.normcase(str(base)): base for base in base_folders}
        for base in unique_bases.values():
            roots.extend([
                (base / "Eclipse Adoptium", "Eclipse Adoptium"),
                (base / "Java", "Java install folder"),
                (base / "Microsoft", "Microsoft Build of OpenJDK"),
                (base / "Amazon Corretto", "Amazon Corretto"),
                (base / "BellSoft", "BellSoft Liberica"),
                (base / "Azul Systems", "Azul Zulu"),
                (base / "Zulu", "Azul Zulu"),
                (base / "OpenJDK", "OpenJDK"),
                (base / "Programs" / "Eclipse Adoptium", "Eclipse Adoptium"),
            ])
        for root, label in roots:
            if not root.is_dir():
                continue
            try:
                for match in sorted(root.glob("*/bin/java.exe")):
                    add(match, label)
                add(root / "bin" / "java.exe", label)
            except OSError:
                pass

        # Per-user IDE/toolchain and package-manager installs are common when Java
        # was installed without adding it to the machine-wide PATH.
        home = Path.home()
        user_patterns = [
            (home / ".jdks", "JetBrains runtime folder"),
            (home / ".gradle" / "jdks", "Gradle Java toolchain"),
            (home / "scoop" / "apps", "Scoop package install"),
        ]
        for root, label in user_patterns:
            if not root.is_dir():
                continue
            try:
                patterns = ("*/bin/java.exe", "*/*/bin/java.exe", "*/current/bin/java.exe")
                for pattern in patterns:
                    for match in sorted(root.glob(pattern)):
                        add(match, label)
            except OSError:
                pass

        program_data = os.environ.get("ProgramData")
        if program_data:
            chocolatey = Path(program_data) / "chocolatey" / "lib"
            if chocolatey.is_dir():
                try:
                    for match in sorted(chocolatey.glob("*/tools/*/bin/java.exe")):
                        add(match, "Chocolatey package install")
                except OSError:
                    pass

        oracle_javapath = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Common Files" / "Oracle" / "Java" / "javapath" / "java.exe"
        add(oracle_javapath, "Oracle Java path")

        # JavaSoft registry entries cover manually selected installation folders.
        try:
            import winreg
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
                    for family in ("JavaSoft\\JDK", "JavaSoft\\Java Development Kit", "JavaSoft\\JRE", "JavaSoft\\Java Runtime Environment"):
                        try:
                            with winreg.OpenKey(hive, "SOFTWARE\\" + family, 0, winreg.KEY_READ | view) as key:
                                version = winreg.QueryValueEx(key, "CurrentVersion")[0]
                                with winreg.OpenKey(key, str(version)) as version_key:
                                    home = winreg.QueryValueEx(version_key, "JavaHome")[0]
                                    add(Path(home) / "bin" / "java.exe", "Windows Java registry")
                        except (OSError, ValueError):
                            continue
        except ImportError:
            pass
    else:
        for pattern in ("/usr/lib/jvm/*/bin/java", "/usr/java/*/bin/java", "/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java"):
            try:
                for match in sorted(Path("/").glob(pattern.lstrip("/"))):
                    add(match, "System Java install")
            except OSError:
                pass
    return candidates


def detect_java_runtimes(preferred: str = "", refresh: bool = False) -> list[dict]:
    """Return working Java runtimes on this host, including their versions."""
    if not preferred and not refresh and time.time() - _JAVA_INVENTORY["checked_at"] < 900:
        return list(_JAVA_INVENTORY["runtimes"])
    runtimes = []
    for path, source in _java_candidates(preferred)[:20]:
        try:
            result = subprocess.run([path, "-version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", timeout=3, check=False)
            output = (result.stdout or "").strip()
            match = re.search(r'version\s+["\']?([0-9]+(?:\.[0-9]+)*(?:_[0-9]+)?)', output, re.IGNORECASE)
            if result.returncode == 0 and match:
                runtimes.append({"path": str(Path(path).resolve()), "version": match.group(1), "source": source})
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    if not preferred:
        _JAVA_INVENTORY["checked_at"] = time.time()
        _JAVA_INVENTORY["runtimes"] = runtimes
    return runtimes


def detect_java(preferred: str = "") -> dict:
    """Find the first working Java executable on this host."""
    runtimes = detect_java_runtimes(preferred)
    if runtimes:
        return runtimes[0]
    return {"path": "", "version": "", "source": ""}


def java_version_major(version: str) -> int | None:
    """Return the Java feature version from strings such as 21.0.2 or 1.8.0_402."""
    match = re.match(r"^\s*(\d+)(?:\.(\d+))?", str(version or ""))
    if not match:
        return None
    major = int(match.group(1))
    return int(match.group(2)) if major == 1 and match.group(2) else major


def java_requirement_for_minecraft(version: str) -> dict | None:
    """Mirror the Java guidance shown in the UI for known Minecraft releases."""
    match = re.match(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?", str(version or ""))
    if not match:
        return None
    game_major, game_minor, game_patch = (int(value or 0) for value in match.groups())
    if game_major == 26 and game_minor >= 1:
        return {"major": 25, "minimum": True}
    if game_major != 1:
        return None
    if game_minor == 21 or (game_minor == 20 and game_patch >= 5):
        return {"major": 21, "minimum": False}
    if 18 <= game_minor <= 20:
        return {"major": 17, "minimum": False}
    if game_minor == 17:
        return {"major": 16, "minimum": False}
    if 1 <= game_minor <= 16:
        return {"major": 8, "minimum": False}
    return None


def java_meets_requirement(runtime: dict, requirement: dict) -> bool:
    major = java_version_major(runtime.get("version", ""))
    return major is not None and (major >= requirement["major"] if requirement["minimum"] else major == requirement["major"])


def select_java_for_minecraft(version: str, configured_java: str = "java") -> dict:
    """Choose a detected Java runtime that matches this Minecraft version."""
    configured_java = str(configured_java or "java").strip() or "java"
    generic_command = configured_java.lower() in ("java", "java.exe")
    runtimes = detect_java_runtimes("" if generic_command else configured_java, refresh=True)
    if not runtimes:
        return {"path": "", "version": "", "source": ""}

    requirement = java_requirement_for_minecraft(version)
    if not requirement:
        return runtimes[0]

    # Keep an explicitly selected runtime when it is compatible. Otherwise pick
    # another installed runtime that matches the game's requirement.
    if not generic_command:
        saved_runtime = next((item for item in runtimes if item.get("source") == "Saved Java executable"), None)
        if saved_runtime and java_meets_requirement(saved_runtime, requirement):
            return saved_runtime
    compatible = next((item for item in runtimes if java_meets_requirement(item, requirement)), None)
    if compatible:
        return compatible

    detected = ", ".join(f"Java {item['version']} ({item['source']})" for item in runtimes[:4])
    needed = f"Java {requirement['major']} or newer" if requirement["minimum"] else f"Java {requirement['major']}"
    raise ValueError(
        f"Minecraft {version} requires {needed}, but Blockyard detected {detected}. "
        f"Install {needed} and use Find Java in Properties to select it."
    )


def persist_servers():
    with state_lock:
        safe = {}
        for sid, item in servers.items():
            safe[sid] = {k: v for k, v in item.items() if k not in ("logs", "status")}
        temp = SERVERS_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(safe, indent=2), encoding="utf-8")
        temp.replace(SERVERS_FILE)


if _legacy_server_data_found:
    persist_servers()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def persist_client_only_manifest():
    """Write the learned client-only mod fingerprints atomically."""
    with state_lock:
        temp = CLIENT_ONLY_MANIFEST_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(client_only_manifest, indent=2), encoding="utf-8")
        temp.replace(CLIENT_ONLY_MANIFEST_FILE)


def validate_client_only_manifest(document):
    """Validate a user-edited learned manifest before replacing the saved copy."""
    if not isinstance(document, dict) or type(document.get("version")) is not int or document.get("version") != 1:
        raise ValueError("Manifest must be a JSON object with version set to 1.")
    entries = document.get("mods")
    if not isinstance(entries, list) or len(entries) > 10000:
        raise ValueError("Manifest must contain a mods array with no more than 10,000 entries.")

    list_fields = ("mod_ids", "filenames", "sha256", "sources", "sizes", "reasons")
    string_fields = {"reason", "first_seen", "last_seen"}
    normalized = {"version": 1, "mods": []}
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Manifest entry {index} must be an object.")
        clean = {}
        for key in list_fields:
            if key not in entry:
                continue
            values = entry[key]
            if not isinstance(values, list) or len(values) > 1000:
                raise ValueError(f"Manifest entry {index}: {key} must be a list with no more than 1,000 values.")
            if key == "sizes":
                if any(not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 2**63 - 1 for value in values):
                    raise ValueError(f"Manifest entry {index}: sizes must contain positive integers.")
                clean[key] = sorted(set(values))
                continue
            if any(not isinstance(value, str) or len(value) > 500 for value in values):
                raise ValueError(f"Manifest entry {index}: {key} must contain strings no longer than 500 characters.")
            if key == "sha256" and any(not re.fullmatch(r"[0-9a-fA-F]{64}", value.strip()) for value in values):
                raise ValueError(f"Manifest entry {index}: sha256 values must be 64 hexadecimal characters.")
            if key == "filenames":
                names = (normalize_mod_filename(value) for value in values if value.strip())
                clean[key] = list(dict.fromkeys(name for name in names if name))
                continue
            clean[key] = list(dict.fromkeys(value.strip().lower() if key == "mod_ids" or key == "sha256" else value.strip()
                                            for value in values if value.strip()))
        for key in string_fields:
            if key not in entry:
                continue
            value = entry[key]
            if not isinstance(value, str) or len(value) > 1000:
                raise ValueError(f"Manifest entry {index}: {key} must be a string no longer than 1,000 characters.")
            clean[key] = value
        fingerprints = any(clean.get(key) for key in ("mod_ids", "filenames", "sha256"))
        if not fingerprints:
            raise ValueError(f"Manifest entry {index} needs at least one mod ID, filename, or SHA-256 fingerprint.")
        normalized["mods"].append(clean)
    return normalized


def replace_client_only_manifest(document):
    """Replace and persist the editable manifest, then refresh saved compatibility views."""
    global client_only_manifest
    with state_lock:
        temp = CLIENT_ONLY_MANIFEST_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(document, indent=2), encoding="utf-8")
        temp.replace(CLIENT_ONLY_MANIFEST_FILE)
        client_only_manifest = document
    try:
        refresh_saved_client_only_classifications()
    except Exception as exc:
        print(f"Could not refresh saved mod compatibility after editing the client-only manifest: {exc}")


def refresh_upstream_client_only_list(force: bool = False) -> bool:
    """Refresh the attributed, conservative client-only rules and keep a local cache."""
    global upstream_client_only_list, _upstream_client_list_checked_at, _upstream_client_list_attempted_at
    now = time.time()
    if not force and now - _upstream_client_list_attempted_at < CLIENT_ONLY_LIST_REFRESH_SECONDS:
        return False
    if not _upstream_client_list_refresh_lock.acquire(timeout=10):
        return False
    try:
        _upstream_client_list_attempted_at = time.time()
        request = urllib.request.Request(
            CLIENT_ONLY_LIST_URL,
            headers={"User-Agent": UA, "Accept": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return False
        parsed = json.loads(raw.decode("utf-8"))
        validated = validate_upstream_client_only_list(parsed)
        if not validated:
            return False
        checked_at = time.time()
        with state_lock:
            upstream_client_only_list = validated
            _upstream_client_list_checked_at = checked_at
            temp = CLIENT_ONLY_LIST_CACHE_FILE.with_suffix(".tmp")
            temp.write_text(json.dumps({"checked_at": checked_at, "data": parsed}, indent=2), encoding="utf-8")
            temp.replace(CLIENT_ONLY_LIST_CACHE_FILE)
        try:
            refresh_saved_client_only_classifications()
        except Exception as exc:
            print(f"Could not recheck saved mods against the curated client-only list: {exc}")
        return True
    except (OSError, urllib.error.URLError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return False
    finally:
        _upstream_client_list_refresh_lock.release()


def normalize_mod_filename(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", Path(name).stem.lower())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _record_client_only_mod_unlocked(jar: Path | None, mod_ids=(), reason: str = "", source: str = "metadata"):
    """Remember identifying data for a client-only mod confirmed by metadata, crash evidence, or the user."""
    ids = sorted({str(value).strip().lower() for value in mod_ids if str(value).strip()})
    filenames = [normalize_mod_filename(jar.name)] if jar else []
    hashes, sizes = [], []
    if jar:
        try:
            if jar.is_file():
                hashes.append(sha256_file(jar))
                sizes.append(jar.stat().st_size)
        except OSError:
            pass
    if not ids and not filenames and not hashes:
        return

    matched = None
    for item in client_only_manifest["mods"]:
        if not isinstance(item, dict):
            continue
        old_ids = item.get("mod_ids") if isinstance(item.get("mod_ids"), list) else []
        old_names = item.get("filenames") if isinstance(item.get("filenames"), list) else []
        old_hashes = item.get("sha256") if isinstance(item.get("sha256"), list) else []
        if (set(ids) & set(old_ids) or set(filenames) & set(old_names) or set(hashes) & set(old_hashes)):
            matched = item
            break
    now = utc_now()
    if matched is None:
        matched = {"mod_ids": [], "filenames": [], "sha256": [], "sources": [],
                   "sizes": [], "reasons": [], "reason": "", "first_seen": now}
        client_only_manifest["mods"].append(matched)
    for key, values in (("mod_ids", ids), ("filenames", filenames), ("sha256", hashes), ("sizes", sizes), ("sources", [source])):
        existing = matched.get(key) if isinstance(matched.get(key), list) else []
        matched[key] = sorted(set(existing) | set(values))
    if reason:
        reasons = matched.get("reasons") if isinstance(matched.get("reasons"), list) else []
        matched["reasons"] = list(dict.fromkeys([*reasons, reason[:500]]))
        if source != "metadata-or-manifest" or not matched.get("reason"):
            matched["reason"] = reason[:500]
    matched["last_seen"] = now
    persist_client_only_manifest()


def record_client_only_mod(jar: Path | None, mod_ids=(), reason: str = "", source: str = "metadata"):
    """Serialize manifest updates so concurrent imports cannot overwrite one another."""
    with state_lock:
        _record_client_only_mod_unlocked(jar, mod_ids, reason, source)


def find_client_only_manifest_match(jar: Path, mod_ids: set[str]) -> dict | None:
    filename = normalize_mod_filename(jar.name)
    needs_hash = False
    try:
        jar_size = jar.stat().st_size
    except OSError:
        return None
    for item in client_only_manifest["mods"]:
        if not isinstance(item, dict):
            continue
        old_ids = item.get("mod_ids") if isinstance(item.get("mod_ids"), list) else []
        old_names = item.get("filenames") if isinstance(item.get("filenames"), list) else []
        old_hashes = item.get("sha256") if isinstance(item.get("sha256"), list) else []
        old_sizes = item.get("sizes") if isinstance(item.get("sizes"), list) else []
        if set(mod_ids) & set(old_ids) or filename in old_names:
            return item
        needs_hash = needs_hash or bool(old_hashes) and (not old_sizes or jar_size in old_sizes)
    if not needs_hash:
        return None
    try:
        digest = sha256_file(jar)
    except OSError:
        return None
    return next((item for item in client_only_manifest["mods"]
                 if isinstance(item, dict) and digest in (item.get("sha256") if isinstance(item.get("sha256"), list) else [])), None)


def _upstream_match_matches(matcher: dict, filename: str, mod_ids=(),
                            modrinth_project_ids=(), modrinth_slugs=(),
                            curseforge_project_ids=(), curseforge_slugs=()) -> bool:
    if not isinstance(matcher, dict):
        return False
    filename = Path(filename).name.casefold()
    ids = {str(value).casefold() for value in mod_ids if str(value)}
    modrinth_ids = {str(value).casefold() for value in modrinth_project_ids if str(value)}
    curseforge_ids = {str(value).casefold() for value in curseforge_project_ids if str(value)}
    # Many loaders use the Modrinth slug itself as the mod id. This fallback
    # gives local CurseForge/Prism archives the same ID matching as .mrpacks.
    modrinth_slug_values = ids | {str(value).casefold() for value in modrinth_slugs if str(value)}
    curseforge_slug_values = ids | {str(value).casefold() for value in curseforge_slugs if str(value)}
    for platform, candidate_ids, candidate_slugs in (
        ("modrinth", modrinth_ids, modrinth_slug_values),
        ("curseforge", curseforge_ids, curseforge_slug_values),
    ):
        expected = matcher.get(platform)
        if not isinstance(expected, dict):
            continue
        project_id = str(expected.get("projectId") or "").casefold()
        slug = str(expected.get("slug") or "").casefold()
        if (project_id and project_id in candidate_ids) or (slug and slug in candidate_slugs):
            return True
    expected_file = matcher.get("filename")
    if isinstance(expected_file, dict):
        equals = expected_file.get("equals") if isinstance(expected_file.get("equals"), list) else []
        prefixes = expected_file.get("prefix") if isinstance(expected_file.get("prefix"), list) else []
        if filename in {str(value).casefold() for value in equals}:
            return True
        if any(filename.startswith(str(value).casefold()) for value in prefixes):
            return True
    return False


def find_upstream_protected_entry(filename: str, mod_ids=(), modrinth_project_ids=(),
                                  modrinth_slugs=(), curseforge_project_ids=(),
                                  curseforge_slugs=()) -> dict | None:
    args = (filename, mod_ids, modrinth_project_ids, modrinth_slugs,
            curseforge_project_ids, curseforge_slugs)
    for entry in upstream_client_only_list.get("neverRemove", []):
        if _upstream_match_matches(entry.get("match", {}), *args):
            return entry
    return None


def find_upstream_client_only_rule(filename: str, loader: str, minecraft: str,
                                   mod_ids=(), modrinth_project_ids=(), modrinth_slugs=(),
                                   curseforge_project_ids=(), curseforge_slugs=()) -> dict | None:
    """Return only high-confidence client-only rules, after hard protections and scoped overrides."""
    loader = str(loader or "").casefold()
    minecraft = str(minecraft or "").casefold()
    if loader not in ("fabric", "forge", "neoforge", "quilt"):
        return None
    args = (filename, mod_ids, modrinth_project_ids, modrinth_slugs,
            curseforge_project_ids, curseforge_slugs)
    if find_upstream_protected_entry(filename, mod_ids, modrinth_project_ids,
                                     modrinth_slugs, curseforge_project_ids, curseforge_slugs):
        return None
    for rule in upstream_client_only_list.get("rules", []):
        if loader not in rule.get("loaders", []):
            continue
        if not _upstream_match_matches(rule.get("match", {}), *args):
            continue
        effective = dict(rule)
        scopes = (
            f"{loader}@{minecraft}", f"{loader}@*", f"*@{minecraft}", "*",
        )
        for scope in scopes:
            scoped_rules = upstream_client_only_list.get("overrides", {}).get(scope)
            patch = scoped_rules.get(str(rule.get("id") or "")) if isinstance(scoped_rules, dict) else None
            if isinstance(patch, dict):
                for key in ("sideness", "confidence", "reason", "loaders"):
                    if key in patch:
                        effective[key] = patch[key]
                break
        if (effective.get("sideness") == "client-only" and
                effective.get("confidence") in ("verified", "high")):
            return effective
    return None


for _known_id, _known_reason in KNOWN_CLIENT_ONLY_MOD_IDS.items():
    if not any(isinstance(_item, dict) and _known_id in _item.get("mod_ids", [])
               for _item in client_only_manifest["mods"]):
        client_only_manifest["mods"].append({"mod_ids": [_known_id], "filenames": [], "sha256": [],
                                              "sizes": [], "sources": ["built-in"], "reasons": [_known_reason], "reason": _known_reason,
                                              "first_seen": utc_now(), "last_seen": utc_now()})
persist_client_only_manifest()


def download_url(url: str, target: Path, expected_sha1: str | None = None, expected_sha512: str | None = None, allowed_hosts: set[str] | None = None):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("A download URL was not a secure HTTPS link.")
    host = parsed.hostname.lower()
    if allowed_hosts and host not in allowed_hosts:
        raise ValueError(f"The modpack links to a download host that Blockyard does not allow: {host}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    digest = hashlib.sha1()
    digest512 = hashlib.sha512() if expected_sha512 else None
    written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    class RestrictedRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, original, fp, code, msg, headers, newurl):
            redirected_url = urllib.parse.urlparse(newurl)
            redirected_host = (redirected_url.hostname or "").lower()
            if allowed_hosts and (redirected_url.scheme != "https" or redirected_host not in allowed_hosts):
                raise ValueError(f"The download redirected to a host that Blockyard does not allow: {redirected_host or 'unknown'}")
            return super().redirect_request(original, fp, code, msg, headers, newurl)

    opener = urllib.request.build_opener(RestrictedRedirects())
    with opener.open(req, timeout=60) as response, target.open("wb") as out:
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            written += len(block)
            if written > MAX_PACK_BYTES:
                raise ValueError("A download exceeded the 2 GB safety limit.")
            digest.update(block)
            if digest512:
                digest512.update(block)
            out.write(block)
    if expected_sha1 and digest.hexdigest().lower() != expected_sha1.lower():
        target.unlink(missing_ok=True)
        raise ValueError(f"Checksum mismatch while downloading {target.name}.")
    if expected_sha512 and digest512.hexdigest().lower() != expected_sha512.lower():
        target.unlink(missing_ok=True)
        raise ValueError(f"Checksum mismatch while downloading {target.name}.")
    return written


def safe_target(root: Path, relative: str) -> Path:
    relative = relative.replace("\\", "/")
    if not relative.strip():
        raise ValueError("The pack contains an empty file path.")
    path = PurePosixPath(relative)
    if path.is_absolute() or any(part in ("..", "") for part in path.parts) or re.match(r"^[A-Za-z]:", relative):
        raise ValueError(f"The pack contains an unsafe file path: {relative}")
    base = root.resolve()
    dest = (base / Path(*path.parts)).resolve()
    if dest != base and base not in dest.parents:
        raise ValueError(f"The pack contains an unsafe file path: {relative}")
    return dest


def extract_zip_safe(archive: zipfile.ZipFile, root: Path, prefix: str | None = None):
    if len(archive.infolist()) > 100_000:
        raise ValueError("The pack archive contains too many entries to install safely.")
    expanded = 0
    for info in archive.infolist():
        name = info.filename.replace("\\", "/")
        if prefix:
            if not name.startswith(prefix + "/"):
                continue
            name = name[len(prefix) + 1:]
        name = name.rstrip("/")
        if not name:
            continue
        mode = (info.external_attr >> 16) & 0xFFFF
        if (mode & 0o170000) == 0o120000:
            raise ValueError(f"Symbolic links are not allowed in pack archives: {info.filename}")
        dest = safe_target(root, name)
        if info.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
            continue
        expanded += info.file_size
        if expanded > MAX_EXPANDED_BYTES:
            raise ValueError("The pack archive expands beyond the 8 GB safety limit.")
        dest.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, dest.open("wb") as out:
            shutil.copyfileobj(source, out)


def log(server_id: str, message: str, level: str = "info"):
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "text": message, "level": level}
    with state_lock:
        slot = runtime.setdefault(server_id, {"logs": [], "status": "offline", "process": None, "log_seq": 0})
        slot["log_seq"] += 1
        entry["seq"] = slot["log_seq"]
        slot["logs"].append(entry)
        slot["logs"] = slot["logs"][-1200:]
        server = servers.get(server_id)
        if server:
            server["updated_at"] = utc_now()


def public_server(server_id: str):
    with state_lock:
        item = servers[server_id]
        live = runtime.setdefault(server_id, {"logs": [], "status": "offline", "process": None, "log_seq": 0})
        process = live.get("process")
        return {
            **item,
            "status": "running" if process and process.poll() is None else live["status"],
            "pid": process.pid if process and process.poll() is None else None,
            "logs": list(live["logs"][-160:]),
            "log_seq": live["log_seq"],
        }


def server_file_path(server_id: str, relative_path: str = "") -> tuple[Path, Path]:
    """Resolve a path inside a registered server folder and reject traversal or escaping symlinks."""
    server = servers.get(server_id)
    if not server:
        raise ValueError("Server not found.")
    raw_root = str(server.get("path", "")).strip()
    if not raw_root:
        raise ValueError("This server does not have a configured folder.")
    root = Path(raw_root).resolve()
    if not root.is_dir():
        raise ValueError("The server folder is missing or unavailable.")
    raw = str(relative_path or "").replace("\\", "/").strip()
    relative = PurePosixPath(raw)
    if relative.is_absolute() or any(part in (".", "..") for part in relative.parts):
        raise ValueError("That path is outside the server folder.")
    candidate = root.joinpath(*relative.parts).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("That path is outside the server folder.") from exc
    return root, candidate


def server_file_listing(server_id: str, relative_path: str = "") -> dict:
    root, folder = server_file_path(server_id, relative_path)
    if not folder.is_dir():
        raise ValueError("That server folder does not exist.")
    entries = []
    for child in folder.iterdir():
        try:
            resolved = child.resolve()
            resolved.relative_to(root)
            info = child.stat()
        except (OSError, ValueError):
            # Ignore broken links and links that point outside the server folder.
            continue
        is_directory = child.is_dir()
        if not is_directory and not child.is_file():
            continue
        relative = child.relative_to(root).as_posix()
        entries.append({
            "name": child.name,
            "path": relative,
            "type": "directory" if is_directory else "file",
            "size": 0 if is_directory else info.st_size,
            "modified": datetime.fromtimestamp(info.st_mtime).astimezone().isoformat(timespec="minutes"),
        })
    entries.sort(key=lambda item: (item["type"] != "directory", item["name"].casefold()))
    relative_folder = folder.relative_to(root).as_posix() if folder != root else ""
    return {"path": relative_folder, "entries": entries}


def safe_server_filename(value: str) -> str:
    name = str(value or "").strip()
    if (not name or name in (".", "..") or "/" in name or "\\" in name
            or len(name) > 255 or name.endswith((".", " "))
            or any(ord(char) < 32 or char in '<>:"|?*' for char in name)):
        raise ValueError("Enter a valid file name without folders or control characters.")
    return name


def validate_root(name: str, custom_root: str):
    display = re.sub(r"\s+", " ", str(name or "").strip())
    if not display or len(display) > 70:
        raise ValueError("Enter a server name between 1 and 70 characters.")
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", display).strip(".-_") or "minecraft-server"
    root_text = str(custom_root or config.get("default_root", "")).strip()
    root = Path(os.path.expandvars(os.path.expanduser(root_text))).resolve()
    target = (root / slug).resolve()
    if target.exists() and any(target.iterdir()):
        target_key = os.path.normcase(str(target))
        previous = next((item for item in servers.values()
                         if os.path.normcase(str(Path(item.get("path", "")).resolve())) == target_key
                         and item.get("setup_status") == "error"), None)
        if previous:
            error = str(previous.get("setup_error") or "The previous setup did not finish.")
            missing_mods = re.search(r"lists (\d+) mod files but contains only (\d+) mod JARs", error, re.IGNORECASE)
            if missing_mods and "CurseForge client export" in error:
                reason = (f"The previous import failed because this CurseForge profile lists {missing_mods.group(1)} mods "
                          f"but includes only {missing_mods.group(2)} mod JARs. The profile export omitted the mod downloads, "
                          "so Blockyard cannot build a runnable server from that archive without CurseForge API access. "
                          "Use a dedicated server pack or a profile archive that includes the mod JARs.")
            else:
                reason = f"The previous import failed: {error}"
            raise ValueError(f"{reason} Choose a different server name or install location to retry.")
        raise ValueError(f"The install folder already contains files: {target}. Choose a different server name or install location.")
    return display, target


def remember_install_root(target: Path):
    """Keep the parent folder of the latest server install as the next default."""
    root = str(target.parent.resolve())
    with state_lock:
        if config.get("default_root") != root:
            config["default_root"] = root
            persist_config()


SERVER_LAUNCH_SCRIPTS = (
    "run.bat", "run.cmd", "run.sh", "startserver.bat", "startserver.cmd", "startserver.sh",
    "serverstart.bat", "serverstart.cmd", "serverstart.sh", "start.bat", "start.cmd", "start.sh",
)
SERVER_LAUNCH_JARS = ("server.jar", "fabric-server-launch.jar", "quilt-server-launch.jar")


def _read_server_properties(root: Path) -> dict[str, str]:
    values = {}
    try:
        lines = (root / "server.properties").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return values
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def _server_folder_candidate(folder: Path) -> dict | None:
    """Describe a recognizable existing server folder without changing its contents."""
    try:
        if folder.is_symlink() or not folder.is_dir():
            return None
        root = folder.resolve(strict=True)
        if root.is_symlink():
            return None
        files = {item.name.casefold(): item.name for item in root.iterdir() if item.is_file()}
        launch_file = next((files[name] for name in SERVER_LAUNCH_SCRIPTS + SERVER_LAUNCH_JARS if name in files), None)
        if not launch_file:
            return None
        has_server_data = any((root / name).exists() for name in (
            "server.properties", "eula.txt", "mods", "libraries", "world", "logs",
        ))
        if not has_server_data:
            return None

        eula_accepted = False
        try:
            eula_text = (root / "eula.txt").read_text(encoding="utf-8", errors="replace")
            eula_accepted = bool(re.search(r"(?im)^\s*eula\s*=\s*true\s*$", eula_text))
        except OSError:
            pass

        loader_text = launch_file.casefold()
        for script in SERVER_LAUNCH_SCRIPTS:
            script_path = root / files.get(script, "")
            if script_path.is_file():
                try:
                    loader_text += "\n" + script_path.read_bytes()[:256 * 1024].decode("utf-8", "ignore").casefold()
                except OSError:
                    pass
        if (root / "libraries" / "net" / "neoforged" / "neoforge").is_dir() or "neoforge" in loader_text:
            loader = "NeoForge"
        elif (root / "libraries" / "net" / "minecraftforge" / "forge").is_dir() or "forge" in loader_text:
            loader = "Forge"
        elif "fabric" in loader_text:
            loader = "Fabric"
        elif "quilt" in loader_text:
            loader = "Quilt"
        else:
            loader = "Vanilla" if launch_file.casefold() == "server.jar" else "Unknown"

        version = "Unknown"
        log_paths = (root / "logs" / "latest.log", root / "latest.log")
        for log_path in log_paths:
            try:
                log_text = log_path.read_bytes()[-1024 * 1024:].decode("utf-8", "replace")
            except OSError:
                continue
            version_match = re.search(
                r"(?i)(?:starting\s+minecraft\s+server\s+version|minecraft(?:\s+server)?\s+version|minecraft\s+)(?:\s*[:=]\s*|\s+)?v?(\d+\.\d+(?:\.\d+)?)",
                log_text,
            )
            if version_match:
                version = version_match.group(1)
                break

        mod_count = 0
        try:
            mods_dir = root / "mods"
            if mods_dir.is_dir():
                mod_count = sum(1 for item in mods_dir.iterdir() if item.is_file() and item.suffix.casefold() == ".jar")
        except OSError:
            pass
        return {
            "name": root.name or "Existing Minecraft server",
            "path": str(root),
            "minecraft_version": version,
            "loader": loader,
            "launch_file": launch_file,
            "mod_count": mod_count,
            "eula_accepted": eula_accepted,
        }
    except (OSError, RuntimeError):
        return None


def detect_local_server_folders() -> dict:
    """Find server folders directly inside the configured location, plus the location itself."""
    configured = str(config.get("default_root", "")).strip()
    try:
        scan_root = Path(os.path.expandvars(os.path.expanduser(configured))).resolve() if configured else Path()
    except (OSError, RuntimeError):
        return {"scan_root": configured, "directory_exists": False, "servers": []}
    if not configured:
        return {"scan_root": "", "directory_exists": False, "servers": []}
    if not scan_root.is_dir():
        return {"scan_root": str(scan_root), "directory_exists": False, "servers": []}

    registered = set()
    for item in servers.values():
        try:
            registered.add(os.path.normcase(str(Path(item.get("path", "")).resolve())))
        except (OSError, RuntimeError):
            continue
    folders = [scan_root]
    try:
        folders.extend(item for item in scan_root.iterdir() if item.is_dir() and not item.is_symlink())
    except OSError:
        pass
    candidates = []
    seen = set()
    for folder in folders:
        candidate = _server_folder_candidate(folder)
        if not candidate:
            continue
        path_key = os.path.normcase(candidate["path"])
        if path_key in registered or path_key in seen:
            continue
        seen.add(path_key)
        candidates.append(candidate)
    candidates.sort(key=lambda item: item["name"].casefold())
    return {"scan_root": str(scan_root), "directory_exists": True, "servers": candidates}


def http_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=35) as response:
        return json.loads(response.read().decode("utf-8"))


def modrinth_request(path: str, query: dict | None = None):
    url = MODRINTH_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    try:
        return http_json(url)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", "replace")
        raise ValueError(f"Modrinth API returned HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach Modrinth: {exc.reason}") from exc


def curseforge_request(path: str, query: dict | None = None, method: str = "GET", body=None):
    api_key = str(config.get("curseforge_api_key") or "").strip()
    if not api_key:
        raise ValueError("Add your CurseForge API key in Settings before searching or installing CurseForge packs.")
    url = CURSEFORGE_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    payload = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"User-Agent": UA, "Accept": "application/json", "x-api-key": api_key}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(1200).decode("utf-8", "replace")
        if exc.code in (401, 403):
            raise ValueError("CurseForge rejected the API key. Check that it is an active Minecraft API key and save it again in Settings.") from exc
        if exc.code == 429:
            raise ValueError("CurseForge is rate-limiting requests. Wait a moment, then try again.") from exc
        raise ValueError(f"CurseForge API returned HTTP {exc.code}: {detail[:300]}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Could not reach CurseForge: {exc.reason}") from exc


def curseforge_download_link(mod_id: int, file_id: int, file_info: dict | None = None) -> str:
    url = str((file_info or {}).get("downloadUrl") or "").strip()
    if not url:
        result = curseforge_request(f"/mods/{mod_id}/files/{file_id}/download-url")
        data = result.get("data") if isinstance(result, dict) else None
        url = str(data or "").strip()
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or not (host == "forgecdn.net" or host.endswith(".forgecdn.net")):
        raise ValueError("CurseForge did not return a secure, official Forge CDN download link for this file.")
    return url


def curseforge_download(url: str, target: Path, expected_sha1: str | None = None) -> int:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "forgecdn.net" or host.endswith(".forgecdn.net")):
        raise ValueError(f"CurseForge returned an unrecognized download host: {host or 'unknown'}")
    return download_url(url, target, expected_sha1=expected_sha1,
                        allowed_hosts=CURSEFORGE_DOWNLOAD_ALLOWED_HOSTS | {host})


def modrinth_file_ids(entries: list[dict]) -> list[str]:
    ids = set()
    for entry in entries:
        for url in entry.get("downloads") or []:
            match = re.search(r"/versions/([A-Za-z0-9]+)/", urllib.parse.urlparse(str(url)).path)
            if match:
                ids.add(match.group(1))
    return sorted(ids)


def fetch_modrinth_versions(version_ids: list[str]) -> dict[str, dict]:
    """Resolve original Modrinth file environments/project IDs in API batches."""
    result = {}
    for offset in range(0, len(version_ids), 50):
        batch = version_ids[offset:offset + 50]
        try:
            versions = modrinth_request("/versions", {"ids": json.dumps(batch, separators=(",", ":"))})
            result.update({str(item.get("id")): item for item in versions if item.get("id")})
        except (ValueError, urllib.error.URLError) as exc:
            # Keep installation moving if Modrinth's metadata endpoint is unavailable.
            continue
    return result


def entry_environment(entry: dict, version_metadata: dict | None = None) -> str:
    env = entry.get("env") or {}
    server_flag = str(env.get("server", "")).lower()
    if server_flag == "unsupported":
        return "client_only"
    if server_flag in ("required", "optional"):
        return "server_compatible"
    version_metadata = version_metadata or {}
    environment = str(version_metadata.get("environment") or "").lower()
    if environment:
        return environment
    return "server_compatible"


def environment_supports_server(environment: str) -> bool:
    return environment in {
        "server_compatible", "client_and_server", "client_only_server_optional",
        "server_only", "server_only_client_optional", "dedicated_server_only",
        "client_or_server", "client_or_server_prefers_both",
    }


def select_server_pack_files(entries: list[dict], server_id: str, version_metadata: dict | None = None) -> list[dict]:
    """Honor server-side metadata and filter a known crashing e4mc artifact."""
    version_metadata = version_metadata or {}
    selected = []
    for entry in entries:
        ids = modrinth_file_ids([entry])
        metadata = version_metadata.get(ids[0], {}) if ids else {}
        version_ids = set(ids)
        unsupported = version_ids & SERVER_UNSUPPORTED_MODRINTH_VERSIONS
        if unsupported:
            log(server_id, "Skipping e4mc 6.2.3-fabric-modern: Modrinth lists this release as singleplayer-only, and it crashes dedicated servers when players join.", "warning")
            continue
        if entry_environment(entry, metadata) in ("client_only", "singleplayer_only"):
            if str(entry.get("path") or "").lower().endswith(".jar"):
                log(server_id, f"Skipping client-only mod for dedicated server: {Path(str(entry.get('path') or '')).name}", "warning")
            continue
        selected.append(entry)
    return selected


def quarantine_client_only_pack_entries(entries: list[dict], version_metadata: dict[str, dict],
                                         root: Path, server_id: str) -> int:
    """Catch client-only JARs that arrived through shared Modrinth overrides."""
    moved = 0
    for entry in entries:
        relative = str(entry.get("path") or "").replace("\\", "/")
        path = PurePosixPath(relative)
        if path.suffix.lower() != ".jar" or "mods" not in [part.lower() for part in path.parts]:
            continue
        ids = modrinth_file_ids([entry])
        metadata = version_metadata.get(ids[0], {}) if ids else {}
        project_id = str(metadata.get("project_id") or "")
        protected = find_upstream_protected_entry(
            path.name, modrinth_project_ids=[project_id] if project_id else (),
        )
        if protected:
            log(server_id, f"Keeping protected library in the pack overrides: {path.name} ({protected.get('name')}).", "warning")
            continue
        environment = entry_environment(entry, metadata)
        if environment not in ("client_only", "singleplayer_only"):
            continue
        target = safe_target(root, relative)
        if not target.is_file():
            continue
        try:
            backup_path = quarantine_client_mod(root, target)
        except (OSError, ValueError) as exc:
            raise ValueError(f"Blockyard found client-only mod {target.name} in the pack overrides but could not move it: {exc}") from exc
        moved += 1
        log(server_id, f"Moved client-only mod out of the server mods folder: {target.name} (Modrinth marks it client-only). Backup: {backup_path}")
    return moved


def modrinth_server_alternative(project_id: str, minecraft: str, loader: str) -> dict | None:
    query = {"game_versions": json.dumps([minecraft]), "include_changelog": "false"}
    if loader and loader != "minecraft":
        query["loaders"] = json.dumps([loader])
    versions = modrinth_request(f"/project/{urllib.parse.quote(project_id, safe='')}/version", query)
    for version in versions:
        if not environment_supports_server(str(version.get("environment") or "unknown").lower()):
            continue
        if minecraft not in (version.get("game_versions") or []):
            continue
        if loader and loader != "minecraft" and loader not in (version.get("loaders") or []):
            continue
        files = version.get("files") or []
        jar = next((item for item in files if str(item.get("filename") or "").lower().endswith(".jar") and item.get("url")), None)
        if jar:
            version_id = str(version.get("id") or "")
            return {
                "version": str(version.get("version_number") or version.get("name") or "Compatible release"),
                "filename": str(jar.get("filename") or "server mod JAR"),
                "url": f"https://modrinth.com/mod/{urllib.parse.quote(project_id, safe='')}/version/{urllib.parse.quote(version_id, safe='')}",
            }
    return None


def make_mod_compatibility(entries: list[dict], version_metadata: dict[str, dict], root: Path,
                           minecraft: str, loader: str, server_id: str) -> dict:
    """Summarize pack mod JARs and look for server-compatible versions of client-only mods."""
    mods = []
    seen_paths = set()
    alternatives = []
    alternative_projects = {}
    for entry in entries:
        relative = str(entry.get("path") or "").replace("\\", "/")
        path = PurePosixPath(relative)
        if path.suffix.lower() != ".jar" or "mods" not in [part.lower() for part in path.parts]:
            continue
        seen_paths.add(path.as_posix().casefold())
        version_ids = modrinth_file_ids([entry])
        metadata = version_metadata.get(version_ids[0], {}) if version_ids else {}
        environment = entry_environment(entry, metadata)
        project_id = str(metadata.get("project_id") or "")
        protected_entry = find_upstream_protected_entry(
            path.name, modrinth_project_ids=[project_id] if project_id else (),
        )
        curated_rule = find_upstream_client_only_rule(
            path.name, loader, minecraft,
            modrinth_project_ids=[project_id] if project_id else (),
        )
        env_flag = str((entry.get("env") or {}).get("server") or "").lower()
        known_bad = bool(set(version_ids) & SERVER_UNSUPPORTED_MODRINTH_VERSIONS)
        reason = ""
        classification_source = ""
        if known_bad:
            status = "Skipped: known server crash"
            installed = False
        elif protected_entry:
            status = "Protected library"
            installed = (root / relative).is_file()
            reason = f"{protected_entry.get('name', 'This library')} is protected by the curated list and will not be suggested for removal."
            classification_source = "curated-list-protection"
        elif curated_rule:
            status = "Client only"
            installed = (root / relative).is_file()
            reason = (f"{curated_rule.get('name')}: {curated_rule.get('reason')} "
                      f"(curated list {upstream_client_only_list.get('listVersion')})")
            classification_source = "curated-list"
            if installed:
                record_client_only_mod(root / relative, (), reason, "curated-list")
            if project_id:
                indexes = alternative_projects.setdefault(project_id, [])
                if len(mods) not in indexes:
                    indexes.append(len(mods))
        elif environment in ("client_only", "singleplayer_only") or env_flag == "unsupported":
            status = "Client only"
            installed = False
            reason = "Modrinth marks this file as client-only."
            classification_source = "Modrinth"
            if project_id:
                indexes = alternative_projects.setdefault(project_id, [])
                if len(mods) not in indexes:
                    indexes.append(len(mods))
        elif environment == "unknown":
            status = "Unverified"
            installed = (root / relative).is_file()
        else:
            status = "Server compatible"
            installed = (root / relative).is_file()
        backup_path = (PurePosixPath("blockyard-client-only-mods") / path).as_posix()
        quarantined = (root / backup_path).is_file()
        if quarantined:
            status = "Protected library" if protected_entry else "Client only"
            installed = False
            if project_id:
                alternative_projects.setdefault(project_id, []).append(len(mods))
        mods.append({"name": path.name, "path": path.as_posix(), "status": status,
                     "installed": installed, "quarantined": quarantined,
                     "backup_path": backup_path if quarantined else "", "reason": reason,
                     "classification_source": classification_source,
                     "protected": bool(protected_entry),
                     "protected_name": protected_entry.get("name", "") if protected_entry else "",
                     "modrinth_project_id": project_id, "alternative": None})

    # A small bounded lookup keeps setup responsive while trying the exact same
    # Modrinth project against the selected Minecraft version and loader.
    project_items = list(alternative_projects.items())[:48]
    if project_items:
        log(server_id, f"Checking {len(project_items)} client-only mods for server-compatible {minecraft} / {loader} releases…")
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(modrinth_server_alternative, project, minecraft, loader): indexes
                       for project, indexes in project_items}
            for future in concurrent.futures.as_completed(futures):
                indexes = futures[future]
                try:
                    alternative = future.result()
                except Exception:
                    alternative = None
                if alternative:
                    for index in indexes:
                        mods[index]["alternative"] = alternative
                        alternatives.append(alternative)

    mods_root = root / "mods"
    if mods_root.is_dir():
        for jar in mods_root.rglob("*.jar"):
            try:
                relative = jar.relative_to(root).as_posix()
            except ValueError:
                continue
            if relative.casefold() not in seen_paths:
                mods.append({"name": jar.name, "path": relative, "status": "Unverified (not listed in pack index)",
                             "installed": True, "alternative": None})

    counts = {
        "total": len(mods),
        "server_compatible": sum(item["status"] == "Server compatible" for item in mods),
        "client_only": sum(item["status"] == "Client only" for item in mods),
        "removed_client_only": sum(item.get("quarantined", False) for item in mods),
        "unverified": sum(item["status"].startswith("Unverified") for item in mods),
        "alternatives_found": len(alternatives),
    }
    return {"minecraft": minecraft, "loader": loader, "mods": mods, "counts": counts,
            "checked_at": utc_now(), "alternatives_checked": len(project_items), "lookup_limit": 48,
            "client_mod_list_version": upstream_client_only_list.get("listVersion", ""),
            "client_mod_list_source": CLIENT_ONLY_LIST_SOURCE_URL}


def inspect_mod_jar(jar: Path, loader: str = "", minecraft: str = "",
                   modrinth_project_ids=(), curseforge_project_ids=()) -> dict:
    """Classify a JAR only when its metadata or a narrowly known client-only ID is conclusive."""
    environment = "unknown"
    reason = ""
    mod_ids = set()
    display_names = []
    try:
        with zipfile.ZipFile(jar) as archive:
            entries = {name.lower(): name for name in archive.namelist()}
            for metadata_name in ("fabric.mod.json", "quilt.mod.json"):
                entry_name = entries.get(metadata_name)
                if not entry_name:
                    continue
                metadata = json.loads(archive.read(entry_name).decode("utf-8"))
                if metadata.get("id"):
                    mod_ids.add(str(metadata["id"]).lower())
                if metadata.get("name"):
                    display_names.append(str(metadata["name"]))
                value = str(metadata.get("environment") or "").lower()
                if value == "client":
                    environment = "client_only"
                    reason = f"{metadata_name} marks this mod client-only"
                elif value in ("*", "server"):
                    environment = "server_compatible" if value == "*" else "server_only"
                break

            # NeoForge renamed the metadata file; both formats use TOML sections.
            for forge_metadata in (entries.get("meta-inf/neoforge.mods.toml"),
                                   entries.get("meta-inf/mods.toml")):
                if not forge_metadata:
                    continue
                text = archive.read(forge_metadata).decode("utf-8", "replace")
                metadata_ids = set()
                for block in re.split(r"(?im)^\s*\[\[mods\]\]\s*", text)[1:]:
                    mod_id = re.search(r"(?im)^\s*modId\s*=\s*['\"]([^'\"]+)", block)
                    display = re.search(r"(?im)^\s*displayName\s*=\s*['\"]([^'\"]+)", block)
                    if mod_id:
                        metadata_ids.add(mod_id.group(1).lower())
                        mod_ids.add(mod_id.group(1).lower())
                    if display:
                        display_names.append(display.group(1))

                # A NeoForge mod whose every declared dependency is explicitly
                # client-sided cannot load on a dedicated server. Require an
                # explicit side on every dependency to avoid removing common mods.
                for mod_id in metadata_ids:
                    dependency_blocks = re.findall(
                        rf"(?ims)^\s*\[\[dependencies\.{re.escape(mod_id)}\]\]\s*(.*?)(?=^\s*\[\[|\Z)",
                        text,
                    )
                    dependency_sides = [
                        match.group(1).lower()
                        for block in dependency_blocks
                        if (match := re.search(r"(?im)^\s*side\s*=\s*['\"]([^'\"]+)", block))
                    ]
                    if dependency_blocks and len(dependency_sides) == len(dependency_blocks) \
                            and all(side == "client" for side in dependency_sides):
                        environment = "client_only"
                        reason = f"{forge_metadata.rsplit('/', 1)[-1]} marks all dependencies for {mod_id} as client-only"
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError):
        pass

    manifest_match = find_client_only_manifest_match(jar, mod_ids)
    known_id = next((item for item in sorted(mod_ids) if item in KNOWN_CLIENT_ONLY_MOD_IDS), None)
    filename = jar.name.lower()
    protected_entry = find_upstream_protected_entry(
        jar.name, mod_ids,
        modrinth_project_ids=modrinth_project_ids,
        curseforge_project_ids=curseforge_project_ids,
    )
    upstream_rule = None if protected_entry else find_upstream_client_only_rule(
        jar.name, loader, minecraft, mod_ids,
        modrinth_project_ids=modrinth_project_ids,
        curseforge_project_ids=curseforge_project_ids,
    )
    if manifest_match:
        learned_sources_value = manifest_match.get("sources")
        learned_sources = {str(value) for value in learned_sources_value} if isinstance(learned_sources_value, list) else set()
        if (protected_entry or
                (learned_sources and learned_sources <= {"curated-list"} and not upstream_rule)):
            # Curated classifications track the upstream list. Do not let an
            # old cached fingerprint keep a rule alive after it is corrected.
            manifest_match = None
    classification_source = "mod-metadata" if environment == "client_only" else ""
    if protected_entry:
        environment = "unknown"
        reason = f"{protected_entry.get('name', 'This library')} is protected by the curated list and will not be suggested for removal."
        classification_source = "curated-list-protection"
    elif known_id or filename.startswith("tp_shooting-"):
        environment = "client_only"
        reason = KNOWN_CLIENT_ONLY_MOD_IDS.get(known_id or "tp_shooting", "known client-only mod")
        classification_source = "built-in"
    elif environment != "client_only" and upstream_rule:
        environment = "client_only"
        reason = (f"{upstream_rule.get('name')}: {upstream_rule.get('reason')} "
                  f"(curated list {upstream_client_only_list.get('listVersion')})")
        classification_source = "curated-list"
    elif environment == "unknown" and manifest_match:
        environment = "client_only"
        prior_reason = str(manifest_match.get("reason") or "").strip()
        reason = "Previously identified client-only mod" + (f": {prior_reason}" if prior_reason else "")
        classification_source = "Blockyard manifest"
    return {"environment": environment, "reason": reason, "mod_ids": sorted(mod_ids),
            "display_names": display_names, "classification_source": classification_source,
            "upstream_rule": upstream_rule, "protected": bool(protected_entry),
            "protected_name": protected_entry.get("name", "") if protected_entry else ""}


# Learn fingerprints from client-only JARs that Blockyard had already moved
# into its recovery folder before the manifest feature was added.
for _saved_server in servers.values():
    if not _saved_server.get("path"):
        continue
    _root = Path(str(_saved_server["path"]))
    _report = _saved_server.get("mod_compatibility") or {}
    for _mod in _report.get("mods", []):
        if not _mod.get("quarantined") or not _mod.get("backup_path"):
            continue
        _backup_relative = PurePosixPath(str(_mod["backup_path"]).replace("\\", "/"))
        if _backup_relative.is_absolute() or any(part in ("", ".", "..") for part in _backup_relative.parts):
            continue
        try:
            _backup = _root.resolve().joinpath(*_backup_relative.parts).resolve(strict=True)
            _backup.relative_to(_root.resolve())
            if _backup.is_file():
                _metadata = inspect_mod_jar(_backup)
                record_client_only_mod(_backup, _metadata["mod_ids"], _mod.get("reason") or _metadata["reason"], "previously-confirmed")
        except (OSError, ValueError):
            continue


def quarantine_client_mod(root: Path, jar: Path) -> str:
    """Move a confirmed client-only mod outside the loader's mods folder, keeping a backup."""
    root = root.resolve()
    if jar.is_symlink():
        raise ValueError("Refusing to move a mod JAR that is a symbolic link.")
    jar = jar.resolve(strict=True)
    relative = jar.relative_to(root)
    backup_root = root / "blockyard-client-only-mods"
    backup = backup_root / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        stem, suffix = backup.stem, backup.suffix
        number = 2
        while backup.exists():
            backup = backup.with_name(f"{stem}-{number}{suffix}")
            number += 1
    shutil.move(str(jar), str(backup))
    return backup.relative_to(root).as_posix()


def scan_imported_mods(root: Path, minecraft: str, loader: str, server_id: str | None = None) -> dict:
    """Inspect imported JAR metadata and list client-only mods for user confirmation."""
    mods = []
    mods_root = root / "mods"
    seen_paths = set()
    if mods_root.is_dir():
        for jar in sorted(mods_root.rglob("*.jar")):
            try:
                jar.resolve().relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            metadata = inspect_mod_jar(jar, loader, minecraft)
            environment = metadata["environment"]
            relative = jar.relative_to(root).as_posix()
            seen_paths.add(relative.casefold())
            if environment == "client_only":
                record_client_only_mod(jar, metadata["mod_ids"], metadata["reason"],
                                       metadata.get("classification_source") or "metadata-or-manifest")
                if server_id:
                    log(server_id, f"Found a likely client-only mod for review: {jar.name} ({metadata['reason']}). Confirm its removal in Mod compatibility.", "warning")
            status = ("Protected library" if metadata.get("protected") else
                      "Client only" if environment == "client_only" else
                      "Server compatible" if environment_supports_server(environment) else "Unverified")
            mods.append({"name": jar.name, "path": relative, "status": status,
                         "installed": True, "quarantined": False,
                         "backup_path": "", "reason": metadata["reason"],
                         "classification_source": metadata.get("classification_source", ""),
                         "protected": bool(metadata.get("protected")),
                         "protected_name": metadata.get("protected_name", ""), "alternative": None})

    # Keep previously confirmed removals visible after a rescan, as long as the
    # recoverable copy is still present.
    previous = servers.get(server_id, {}).get("mod_compatibility") if server_id else None
    for item in (previous or {}).get("mods", []):
        if not item.get("quarantined") or not item.get("backup_path"):
            continue
        relative = str(item.get("path") or "")
        backup_relative = PurePosixPath(str(item["backup_path"]).replace("\\", "/"))
        if not relative or relative.casefold() in seen_paths or backup_relative.is_absolute() or any(part in (".", "..") for part in backup_relative.parts):
            continue
        backup = root.joinpath(*backup_relative.parts).resolve()
        try:
            backup.relative_to(root.resolve())
        except ValueError:
            continue
        if backup.is_file():
            try:
                backup_metadata = inspect_mod_jar(backup)
                if backup_metadata.get("protected"):
                    item = dict(item)
                    item.update({"status": "Protected library", "protected": True,
                                 "protected_name": backup_metadata.get("protected_name", ""),
                                 "reason": backup_metadata.get("reason", "")})
                else:
                    record_client_only_mod(backup, backup_metadata["mod_ids"], item.get("reason") or backup_metadata["reason"], "previously-confirmed")
            except OSError:
                pass
            mods.append(dict(item))
            seen_paths.add(relative.casefold())
    counts = {
        "total": len(mods),
        "server_compatible": sum(item["status"] == "Server compatible" for item in mods),
        "client_only": sum(item["status"] == "Client only" for item in mods),
        "removed_client_only": sum(item.get("quarantined", False) for item in mods),
        "unverified": sum(item["status"] == "Unverified" for item in mods),
        "alternatives_found": 0,
    }
    return {"minecraft": minecraft, "loader": loader, "mods": mods, "counts": counts,
            "checked_at": utc_now(), "client_mod_list_version": upstream_client_only_list.get("listVersion", ""),
            "client_mod_list_source": CLIENT_ONLY_LIST_SOURCE_URL}


def install_loader(server: dict, minecraft: str, loader_id: str | None):
    root = Path(server["path"])
    if not loader_id:
        log(server["id"], f"Downloading vanilla Minecraft {minecraft} server files…")
        manifest = http_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        ver = next((v for v in manifest.get("versions", []) if v.get("id") == minecraft), None)
        if not ver:
            raise ValueError(f"Minecraft version {minecraft} was not found in Mojang's version manifest.")
        data = http_json(ver["url"])
        url = (data.get("downloads", {}).get("server") or {}).get("url")
        if not url:
            raise ValueError(f"Mojang does not provide a vanilla server jar for {minecraft}.")
        download_url(url, root / "server.jar")
        server["loader"] = "Vanilla"
        return

    pieces = loader_id.split("-", 1)
    loader = pieces[0].lower()
    loader_version = pieces[1] if len(pieces) == 2 else ""
    if loader in ("fabric", "quilt"):
        log(server["id"], f"Installing {loader.title()} server loader {loader_version or '(latest)'}…")
        if loader == "fabric":
            base = "https://meta.fabricmc.net/v2"
        else:
            base = "https://meta.quiltmc.org/v3"
        loaders = http_json(f"{base}/versions/loader/{urllib.parse.quote(minecraft)}")
        if loader_version:
            match = next((x for x in loaders if str(x.get("loader", {}).get("version")) == loader_version), None)
        else:
            match = next((x for x in loaders if x.get("loader", {}).get("stable", True)), loaders[0] if loaders else None)
        if not match:
            raise ValueError(f"No {loader.title()} loader is available for Minecraft {minecraft} ({loader_version}).")
        loader_version = str(match.get("loader", {}).get("version"))
        if loader == "fabric":
            installers = http_json(f"{base}/versions/installer")
            installer = next((x for x in installers if x.get("stable", True)), installers[0] if installers else None)
            if not installer:
                raise ValueError("Fabric did not return an installer version.")
            installer_version = str(installer["version"])
            url = f"{base}/versions/loader/{minecraft}/{loader_version}/{installer_version}/server/jar"
            download_url(url, root / "server.jar")
        else:
            installers = http_json(f"{base}/versions/installer")
            installer = next((x for x in installers if x.get("stable", True)), installers[0] if installers else None)
            if not installer:
                raise ValueError("Quilt did not return an installer version.")
            installer_version = str(installer["version"])
            url = f"https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer/{installer_version}/quilt-installer-{installer_version}.jar"
            installer_jar = root / "quilt-installer.jar"
            download_url(url, installer_jar)
            run_installer(server, [java, "-jar", str(installer_jar), "install", "server", minecraft, "--download-server"])
        server["loader"] = f"{loader.title()} {loader_version}"
        return

    if loader in ("forge", "neoforge"):
        if not loader_version:
            raise ValueError(f"The modpack did not specify a {loader.title()} version.")
        if loader == "forge":
            coordinate = f"{minecraft}-{loader_version}"
            url = f"https://maven.minecraftforge.net/net/minecraftforge/forge/{coordinate}/forge-{coordinate}-installer.jar"
        else:
            url = f"https://maven.neoforged.net/releases/net/neoforged/neoforge/{loader_version}/neoforge-{loader_version}-installer.jar"
        installer_jar = root / f"{loader}-installer.jar"
        log(server["id"], f"Downloading {loader.title()} installer {loader_version}…")
        download_url(url, installer_jar)
        configured_java = str(server.get("settings", {}).get("java_path", "java")).strip() or "java"
        java_info = select_java_for_minecraft(minecraft, configured_java)
        if not java_info["path"]:
            raise ValueError(f"No Java executable was found on this computer to install the {loader.title()} server files. Install a compatible Java runtime or set its full path in Properties → Java executable, then retry setup.")
        java = java_info["path"]
        server.setdefault("settings", {})["java_path"] = java
        log(server["id"], f"Using Java {java_info['version']} from {java_info['source']} to install the Minecraft {minecraft} server loader.")
        log(server["id"], f"Running {loader.title()} server installer…")
        run_installer(server, [java, "-jar", str(installer_jar), "--installServer"])
        installer_jar.unlink(missing_ok=True)
        server["loader"] = f"{loader.title()} {loader_version}"
        return
    raise ValueError(f"This modpack uses an unsupported server loader: {loader_id}")


def run_installer(server: dict, command: list[str]):
    try:
        completed = subprocess.run(command, cwd=server["path"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", timeout=1800, check=False)
    except FileNotFoundError as exc:
        raise ValueError("Java was not found. Install a Java runtime that matches this Minecraft version, add it to PATH, then try again.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("The loader installer timed out after 30 minutes.") from exc
    output = completed.stdout or ""
    for line in output.splitlines()[-55:]:
        log(server["id"], line[:500])
    if completed.returncode != 0:
        raise ValueError(f"The server loader installer exited with code {completed.returncode}. Check the Console for its output.")


def apply_properties(server: dict):
    root = Path(server["path"])
    path = root / "server.properties"
    existing = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                existing[key.strip()] = value.strip()
    settings = server.get("settings", {})
    mapping = {
        "server-port": str(settings.get("port", 25565)),
        "motd": str(settings.get("motd", server.get("name", "Minecraft Server"))).replace("\n", " "),
        "max-players": str(settings.get("max_players", 20)),
        "difficulty": str(settings.get("difficulty", "normal")),
        "gamemode": str(settings.get("gamemode", "survival")),
        "view-distance": str(settings.get("view_distance", 10)),
        "simulation-distance": str(settings.get("simulation_distance", 10)),
        "online-mode": "true" if settings.get("online_mode", True) else "false",
        "white-list": "true" if settings.get("whitelist", False) else "false",
        "pvp": "true" if settings.get("pvp", True) else "false",
        "allow-flight": "true" if settings.get("allow_flight", False) else "false",
    }
    existing.update(mapping)
    path.write_text("#Minecraft server properties\n" + "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n", encoding="utf-8")
    (root / "eula.txt").write_text("# By changing the setting below to TRUE you are indicating your agreement to the Minecraft EULA.\neula=" + ("true" if settings.get("eula_accepted") else "false") + "\n", encoding="utf-8")


def install_modrinth_server(server_id: str, version_id: str, modpack: dict,
                            imported_archive: Path | None = None, original_filename: str = ""):
    server = servers[server_id]
    root = Path(server["path"])
    pack_path = imported_archive or (root / "_modrinth-pack.mrpack")
    try:
        refresh_upstream_client_only_list(force=not bool(upstream_client_only_list.get("listVersion")))
        root.mkdir(parents=True, exist_ok=True)
        if imported_archive:
            log(server_id, f"Converting uploaded Modrinth client pack {original_filename or imported_archive.name} into a dedicated server…")
            version = {}
        else:
            log(server_id, f"Preparing {server['name']} from Modrinth…")
            version = modrinth_request(f"/version/{urllib.parse.quote(str(version_id), safe='')}")
            pack_file = next((item for item in version.get("files", []) if str(item.get("filename", "")).lower().endswith(".mrpack")), None)
            if not pack_file or not pack_file.get("url"):
                raise ValueError("This Modrinth release has no downloadable .mrpack file.")
            hashes = pack_file.get("hashes") or {}
            if not hashes.get("sha1") or not hashes.get("sha512"):
                raise ValueError("The Modrinth release is missing required file checksums.")
            log(server_id, f"Downloading {pack_file.get('filename') or 'modpack archive'}…")
            download_url(pack_file["url"], pack_path, expected_sha1=hashes.get("sha1"), expected_sha512=hashes.get("sha512"), allowed_hosts=MRPACK_ALLOWED_HOSTS)

        with zipfile.ZipFile(pack_path) as archive:
            names = set(archive.namelist())
            if "modrinth.index.json" not in names:
                raise ValueError("The Modrinth archive is missing modrinth.index.json.")
            index = json.loads(archive.read("modrinth.index.json").decode("utf-8"))
            if index.get("formatVersion") != 1 or index.get("game") != "minecraft":
                raise ValueError("This Modrinth modpack uses an unsupported archive format.")
            dependencies = index.get("dependencies") or {}
            minecraft = str(dependencies.get("minecraft") or "")
            if not minecraft:
                raise ValueError("The Modrinth pack does not specify a Minecraft version.")
            loader_keys = ("fabric-loader", "quilt-loader", "neoforge", "forge")
            loader_key = next((key for key in loader_keys if dependencies.get(key)), None)
            loader_id = None
            if loader_key:
                loader_name = "fabric" if loader_key == "fabric-loader" else "quilt" if loader_key == "quilt-loader" else loader_key
                loader_id = f"{loader_name}-{dependencies[loader_key]}"
                server["loader"] = loader_name.title()
            else:
                loader_name = "minecraft"
                server["loader"] = "Vanilla"
            server["minecraft_version"] = minecraft
            server["pack_version"] = (Path(original_filename).stem if imported_archive and original_filename
                                       else str(version.get("version_number") or version.get("name") or "Latest"))
            log(server_id, f"Pack: Minecraft {minecraft} · {server['loader']}")

            # Shared overrides and server-only overrides belong in the server
            # folder. Client-only overrides are intentionally not copied.
            extract_zip_safe(archive, root, "overrides")
            extract_zip_safe(archive, root, "server-overrides")
            pack_entries = index.get("files", [])
            version_metadata = fetch_modrinth_versions(modrinth_file_ids(pack_entries))
            files = select_server_pack_files(pack_entries, server_id, version_metadata)
            total_bytes = sum(int(entry.get("fileSize") or 0) for entry in files)
            if total_bytes > MAX_EXPANDED_BYTES:
                raise ValueError("The Modrinth pack’s server files exceed the 8 GB safety limit.")
            for number, entry in enumerate(files, 1):
                relative = str(entry.get("path") or "")
                target = safe_target(root, relative)
                urls = entry.get("downloads") or []
                file_url = next((candidate for candidate in urls if urllib.parse.urlparse(str(candidate)).scheme == "https"), None)
                if not file_url:
                    raise ValueError(f"Modrinth did not provide a secure download link for {relative or 'a required file'}.")
                file_hashes = entry.get("hashes") or {}
                if not file_hashes.get("sha1") or not file_hashes.get("sha512"):
                    raise ValueError(f"The Modrinth pack is missing required checksums for {Path(relative).name}.")
                log(server_id, f"Downloading pack file {number}/{len(files)}: {Path(relative).name}")
                written = download_url(file_url, target, expected_sha1=file_hashes.get("sha1"), expected_sha512=file_hashes.get("sha512"), allowed_hosts=MRPACK_ALLOWED_HOSTS)
                expected_size = int(entry.get("fileSize") or 0)
                if expected_size and written != expected_size:
                    target.unlink(missing_ok=True)
                    raise ValueError(f"File size mismatch while downloading {Path(relative).name}.")
            quarantine_client_only_pack_entries(pack_entries, version_metadata, root, server_id)
            scan_imported_mods(root, minecraft, loader_name, server_id)
            server["mod_compatibility"] = make_mod_compatibility(
                pack_entries, version_metadata, root, minecraft, loader_name, server_id
            )
            summary = server["mod_compatibility"]["counts"]
            log(server_id, f"Mod check: {summary['server_compatible']} server-compatible, {summary['client_only']} client-only, {summary['unverified']} unverified; found {summary['alternatives_found']} server-compatible alternatives.")
        pack_path.unlink(missing_ok=True)

        start_files = {p.name.lower() for p in root.iterdir() if p.is_file()}
        has_script = bool(start_files & {"run.bat", "run.sh", "startserver.bat", "serverstart.bat", "start.bat"})
        if not (root / "server.jar").exists() and not (root / "fabric-server-launch.jar").exists() and not (root / "quilt-server-launch.jar").exists() and not has_script:
            install_loader(server, minecraft, loader_id)
        server["setup_status"] = "ready"
        apply_properties(server)
        log(server_id, "Server setup is complete. Review the EULA setting, then press Start.")
    except Exception as exc:
        server["setup_status"] = "error"
        server["setup_error"] = str(exc)
        log(server_id, f"Setup failed: {exc}", "error")
    finally:
        pack_path.unlink(missing_ok=True)
        persist_servers()


def has_server_launcher(directory: Path):
    if not directory.is_dir():
        return False
    files = {item.name.lower() for item in directory.iterdir() if item.is_file()}
    if files & {"run.bat", "run.cmd", "run.sh", "startserver.bat", "startserver.cmd", "startserver.sh", "serverstart.bat", "serverstart.cmd", "serverstart.sh", "start.bat", "start.cmd", "start.sh"}:
        return True
    if files & {"server.jar", "fabric-server-launch.jar", "quilt-server-launch.jar"}:
        return True
    jars = [item for item in directory.glob("*.jar") if "installer" not in item.name.lower()]
    return len(jars) == 1


def copy_pack_contents(source: Path, root: Path):
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source).as_posix()
        target = safe_target(root, relative)
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise ValueError(f"The client pack contains duplicate files at {relative}.")
        shutil.copy2(item, target)


def normalize_pack_metadata_overrides(values: dict | None = None):
    values = values or {}
    minecraft = str(values.get("minecraft_version") or "").strip()
    loader_type = str(values.get("loader_type") or "auto").strip().lower() or "auto"
    loader_version = str(values.get("loader_version") or "").strip()
    if minecraft and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,39}", minecraft):
        raise ValueError("Enter a valid Minecraft version, such as 1.21.1 or 26.3.")
    if loader_type not in {"auto", "vanilla", "fabric", "quilt", "forge", "neoforge"}:
        raise ValueError("Choose Auto detect, Vanilla, Fabric, Quilt, Forge, or NeoForge for the loader.")
    if loader_version and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]{0,79}", loader_version):
        raise ValueError("Enter a valid loader version without spaces or slashes.")
    return {"minecraft_version": minecraft, "loader_type": loader_type, "loader_version": loader_version}


def manual_pack_loader_id(overrides: dict):
    loader_type = overrides.get("loader_type", "auto")
    if loader_type == "auto":
        return None
    if loader_type == "vanilla":
        return ""
    loader_version = str(overrides.get("loader_version") or "").strip()
    if loader_type in {"forge", "neoforge"} and not loader_version:
        raise ValueError(f"Enter the {loader_type.title()} version shown in the pack launcher or profile details.")
    return f"{loader_type}-{loader_version}" if loader_version else loader_type


def copy_curseforge_instance(pack_root: Path, root: Path):
    """Copy server-relevant files from a full CurseForge installation export."""
    client_only = {
        "resourcepacks", "shaderpacks", "screenshots", "saves", "logs", "crash-reports",
        "downloads", "cache", "versions", "assets", "libraries", "webcache", "local",
    }
    launcher_metadata = {
        "manifest.json", "minecraftinstance.json", "instance.json", "instance.cfg",
        "mmc-pack.json", "launcher_profiles.json", "options.txt", "optionsof.txt",
        "servers.dat", "usercache.json", "modlist.html", "profileimage.png",
    }
    for item in sorted(pack_root.iterdir(), key=lambda path: path.name.casefold()):
        name = item.name.casefold()
        if name in client_only or name in launcher_metadata or name == "overrides":
            continue
        if item.is_dir():
            copy_pack_contents(item, root / item.name)
        elif item.is_file():
            destination = safe_target(root, item.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise ValueError(f"The client pack contains duplicate files at {item.name}.")
            shutil.copy2(item, destination)


def curseforge_enabled_mod_paths(manifest: dict, pack_root: Path) -> list[str] | None:
    """Return required, enabled mod JAR paths from full CurseForge instance metadata."""
    instance_path = pack_root / "minecraftinstance.json"
    if not instance_path.is_file():
        return None
    try:
        instance = json.loads(instance_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    addons = instance.get("installedAddons") if isinstance(instance, dict) else None
    if not isinstance(addons, list):
        return None

    by_file = {}
    for addon in addons:
        if not isinstance(addon, dict):
            continue
        installed_file = addon.get("installedFile") or {}
        if not isinstance(installed_file, dict):
            continue
        key = (str(addon.get("addonID") or installed_file.get("projectId") or ""),
               str(installed_file.get("id") or ""))
        if all(key):
            by_file[key] = addon

    expected_paths = []
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict) or entry.get("required", True) is False:
            continue
        addon = by_file.get((str(entry.get("projectID") or ""), str(entry.get("fileID") or "")))
        if addon is None:
            # Partial launcher metadata cannot safely distinguish mods from
            # shaders or disabled add-ons, so leave the legacy validation in place.
            return None
        installed_file = addon.get("installedFile") or {}
        category_section = addon.get("categorySection") or {}
        if not isinstance(category_section, dict):
            return None
        category = str(category_section.get("path") or "mods").replace("\\", "/")
        category_path = PurePosixPath(category)
        if not category_path.parts or category_path.parts[0].casefold() != "mods":
            continue
        file_name = str(addon.get("fileNameOnDisk") or installed_file.get("fileNameOnDisk")
                        or installed_file.get("fileName") or "").replace("\\", "/")
        file_path = PurePosixPath(file_name)
        if not file_name or file_path.is_absolute() or any(part in ("", ".", "..") for part in file_path.parts):
            return None
        relative = (category_path / file_path).as_posix()
        if relative.casefold().endswith(".jar.disabled") or not relative.casefold().endswith(".jar"):
            continue
        if addon.get("isEnabled") is False:
            continue
        expected_paths.append(relative)
    return sorted(set(expected_paths), key=str.casefold)


def transform_curseforge_client_pack(server_id: str, archive_path: Path, original_filename: str,
                                     manifest: dict, staging: Path, pack_root: Path,
                                     metadata_overrides: dict | None = None):
    """Build a server folder from a CurseForge profile only when its mod JARs are present locally."""
    server = servers[server_id]
    root = Path(server["path"])
    metadata_overrides = normalize_pack_metadata_overrides(metadata_overrides)
    minecraft_data = manifest.get("minecraft") or {}
    minecraft = str(minecraft_data.get("version") or metadata_overrides.get("minecraft_version") or "")
    if not minecraft:
        raise ValueError("The pack does not include a Minecraft version. Enter it in the manual version field and try again.")
    loader_entry = next((item for item in minecraft_data.get("modLoaders", []) if item.get("primary")), None)
    if not loader_entry and minecraft_data.get("modLoaders"):
        loader_entry = minecraft_data["modLoaders"][0]
    loader_id = str((loader_entry or {}).get("id") or "")
    if not loader_id and metadata_overrides.get("loader_type") != "auto":
        loader_id = manual_pack_loader_id(metadata_overrides) or ""
    loader_key = loader_id.split("-", 1)[0].lower() if loader_id else ""
    if not loader_key and manifest.get("files"):
        if metadata_overrides.get("loader_type") == "vanilla":
            raise ValueError("This pack lists mods, so a Vanilla server cannot load them. Choose the mod loader used by the client pack.")
        raise ValueError("This client pack lists mods but does not identify its loader. Choose Fabric, Quilt, Forge, or NeoForge manually.")

    is_full_instance = (pack_root / "minecraftinstance.json").is_file() or (pack_root / "instance.json").is_file()
    expected_mod_paths = curseforge_enabled_mod_paths(manifest, pack_root) if is_full_instance else None
    if expected_mod_paths is not None:
        missing_mods = [relative for relative in expected_mod_paths if not safe_target(pack_root, relative).is_file()]
        if missing_mods:
            names = ", ".join(PurePosixPath(item).name for item in missing_mods[:12])
            more = f" and {len(missing_mods) - 12} more" if len(missing_mods) > 12 else ""
            raise ValueError(
                f"This CurseForge instance is missing {len(missing_mods)} enabled mod JARs: {names}{more}. "
                "Add those JARs to the ZIP or export a complete instance before importing."
            )
        log(server_id, f"CurseForge metadata confirms all {len(expected_mod_paths)} enabled mod JARs are present; disabled add-ons and non-mod files are ignored for this check.")

    override_roots = [pack_root / "overrides"] if (pack_root / "overrides").is_dir() else []
    copied_roots = set()
    if override_roots:
        copy_pack_contents(override_roots[0], root)
        copied_roots.add(override_roots[0].resolve())

    if is_full_instance:
        copy_curseforge_instance(pack_root, root)
    else:
        # Some profile exports include their local mod JARs even though the
        # standard CurseForge manifest usually stores only project/file IDs.
        for mods_dir in sorted((item for item in pack_root.rglob("mods") if item.is_dir()), key=lambda item: len(item.parts)):
            if any(parent in mods_dir.resolve().parents for parent in copied_roots):
                continue
            copy_pack_contents(mods_dir, root / "mods")

    jar_count = sum(1 for item in (root / "mods").rglob("*.jar") if item.is_file()) if (root / "mods").is_dir() else 0
    expected_count = len([entry for entry in manifest.get("files") or []
                          if isinstance(entry, dict) and entry.get("required", True) is not False])
    if expected_mod_paths is None and expected_count and jar_count < expected_count:
        raise ValueError(
            f"This CurseForge client export lists {expected_count} mod files but contains only {jar_count} mod JARs. "
            "Some profile exports omit mod downloads. Add the missing JARs to the ZIP or import a complete CurseForge instance."
        )

    server["source"] = "local"
    server["minecraft_version"] = minecraft
    server["pack_version"] = Path(original_filename).stem or "Imported client pack"
    loader_labels = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric", "quilt": "Quilt"}
    server["loader"] = loader_labels.get(loader_key, loader_key.title() if loader_key else "Vanilla")
    log(server_id, f"Converting CurseForge client pack to a server folder: Minecraft {minecraft} · {server['loader']}.")
    install_loader(server, minecraft, loader_id or None)
    server["mod_compatibility"] = scan_imported_mods(root, minecraft, loader_key or "unknown", server_id)
    server["setup_status"] = "ready"
    apply_properties(server)
    shutil.rmtree(staging, ignore_errors=True)
    archive_path.unlink(missing_ok=True)
    log(server_id, "Client pack converted to a server folder. Review suggested removals in Mod compatibility, accept the EULA, then press Start.")


def install_curseforge_server_pack(server_id: str, mod_id: int, file_id: int):
    """Download an official CurseForge pack release and pass it through Blockyard's importer."""
    server = servers[server_id]
    root = Path(server["path"])
    work = Path(tempfile.mkdtemp(prefix="curseforge-", dir=DATA_DIR))
    profile_path = work / "profile.zip"
    prepared_path = work / "prepared-pack.zip"
    mod_downloads: list[tuple[Path, str]] = []
    try:
        refresh_upstream_client_only_list(force=not bool(upstream_client_only_list.get("listVersion")))
        log(server_id, "Looking up the selected CurseForge release…")
        response = curseforge_request(f"/mods/{mod_id}/files/{file_id}")
        selected = response.get("data") if isinstance(response, dict) else None
        if not isinstance(selected, dict):
            raise ValueError("CurseForge did not return details for the selected release.")

        server_pack_id = selected.get("serverPackFileId")
        server_pack = selected
        if server_pack_id:
            try:
                related = curseforge_request(f"/mods/{mod_id}/files/{int(server_pack_id)}")
                if isinstance(related.get("data"), dict):
                    server_pack = related["data"]
            except (ValueError, TypeError):
                server_pack = selected
        if server_pack.get("isServerPack"):
            server_pack_id = int(server_pack.get("id") or server_pack_id or file_id)
            server_pack_name = safe_server_filename(server_pack.get("fileName") or f"{server['name']}-server-pack.zip")
            link = curseforge_download_link(mod_id, server_pack_id, server_pack)
            server_pack_path = work / server_pack_name
            log(server_id, f"Downloading author-provided server pack {server_pack_name}…")
            curseforge_download(link, server_pack_path)
            if not zipfile.is_zipfile(server_pack_path):
                raise ValueError("The CurseForge server-pack download was not a valid ZIP archive.")
            install_imported_server_pack(server_id, server_pack_path, server_pack_name)
            return

        profile_name = safe_server_filename(selected.get("fileName") or f"{server['name']}-profile.zip")
        profile_link = curseforge_download_link(mod_id, file_id, selected)
        log(server_id, f"Downloading CurseForge profile {profile_name}…")
        curseforge_download(profile_link, profile_path)
        if not zipfile.is_zipfile(profile_path):
            raise ValueError("CurseForge returned a file that is not a ZIP profile export.")

        with zipfile.ZipFile(profile_path) as archive:
            manifest_name = next((name for name in archive.namelist()
                                  if PurePosixPath(name.replace("\\", "/")).name.casefold() == "manifest.json"), None)
            if not manifest_name:
                raise ValueError("The selected CurseForge file is not a profile export and has no server-pack metadata.")
            try:
                manifest = json.loads(archive.read(manifest_name).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("CurseForge's profile manifest could not be read.") from exc
            if not isinstance(manifest, dict) or not isinstance(manifest.get("minecraft"), dict):
                raise ValueError("The CurseForge profile does not include Minecraft version metadata.")
            required = [item for item in manifest.get("files", [])
                        if isinstance(item, dict) and item.get("required", True) is not False]
            if len(required) > 1000:
                raise ValueError("This CurseForge profile lists too many mod files to prepare safely.")
            refs = []
            for item in required:
                try:
                    mod_ref, file_ref = int(item.get("projectID")), int(item.get("fileID"))
                except (TypeError, ValueError):
                    raise ValueError("A required mod entry in the CurseForge manifest has invalid project or file IDs.")
                refs.append({"modId": mod_ref, "fileId": file_ref})
            metadata_by_ref: dict[tuple[int, int], dict] = {}
            for offset in range(0, len(refs), 50):
                batch = refs[offset:offset + 50]
                if not batch:
                    continue
                batch_result = curseforge_request("/mods/files", method="POST", body=batch)
                files = batch_result.get("data", []) if isinstance(batch_result, dict) else []
                by_file_id = {int(item.get("id")): item for item in files
                              if isinstance(item, dict) and str(item.get("id", "")).isdigit()}
                for ref in batch:
                    details = by_file_id.get(ref["fileId"])
                    if details:
                        metadata_by_ref[(ref["modId"], ref["fileId"])] = details

            archive_jar_names = {PurePosixPath(name.replace("\\", "/")).name.casefold()
                                 for name in archive.namelist()
                                 if name.lower().endswith(".jar")}
            downloads = []
            download_bytes = sum(info.file_size for info in archive.infolist())
            if download_bytes > MAX_EXPANDED_BYTES:
                raise ValueError("The CurseForge profile expands beyond the 8 GB safety limit.")
            for ref in refs:
                details = metadata_by_ref.get((ref["modId"], ref["fileId"]))
                if not details:
                    raise ValueError(f"CurseForge could not find required mod file {ref['fileId']} (project {ref['modId']}).")
                filename = safe_server_filename(PurePosixPath(str(details.get("fileName") or f"mod-{ref['fileId']}.jar").replace("\\", "/")).name)
                if filename.casefold() in archive_jar_names:
                    continue
                downloads.append((ref, details, filename))

            for index, (ref, details, filename) in enumerate(downloads, 1):
                mod_link = curseforge_download_link(ref["modId"], ref["fileId"], details)
                target = work / f"mod-{ref['fileId']}.jar"
                hashes = details.get("hashes") or []
                sha1 = next((str(item.get("value")) for item in hashes
                             if isinstance(item, dict) and str(item.get("algo", "")).casefold() in ("1", "sha1")), None)
                log(server_id, f"Downloading required mod {index}/{len(downloads)}: {filename}")
                written = curseforge_download(mod_link, target, expected_sha1=sha1)
                expected_size = int(details.get("fileLength") or 0)
                if expected_size and written != expected_size:
                    target.unlink(missing_ok=True)
                    raise ValueError(f"File size mismatch while downloading {filename}.")
                download_bytes += written
                if download_bytes > MAX_EXPANDED_BYTES:
                    raise ValueError("The CurseForge profile and mods exceed the 8 GB safety limit.")
                mod_downloads.append((target, filename))

            manifest_parent = PurePosixPath(manifest_name.replace("\\", "/")).parent
            prefix = "" if str(manifest_parent) == "." else manifest_parent.as_posix().rstrip("/") + "/"
            existing_members = {name.casefold() for name in archive.namelist()}
            reserved_names = set(archive_jar_names)
            with zipfile.ZipFile(prepared_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
                for info in archive.infolist():
                    with archive.open(info) as source, output.open(info.filename, "w") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                for mod_path, filename in mod_downloads:
                    output_name = filename
                    if output_name.casefold() in reserved_names:
                        output_name = f"{Path(filename).stem}-{mod_path.stem}{Path(filename).suffix}"
                    reserved_names.add(output_name.casefold())
                    member_name = f"{prefix}overrides/mods/{output_name}"
                    if member_name.casefold() in existing_members:
                        raise ValueError(f"The profile contains a conflicting mod path: {output_name}.")
                    existing_members.add(member_name.casefold())
                    with mod_path.open("rb") as source, output.open(member_name, "w") as destination:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)

        log(server_id, f"CurseForge profile is complete; prepared {len(mod_downloads)} missing mod download(s). Converting it for a dedicated server…")
        server["modpack"] = {"name": server.get("modpack", {}).get("name", server["name"]),
                             "source": "curseforge", "project_id": str(mod_id)}
        install_imported_server_pack(server_id, prepared_path, profile_name)
    except Exception as exc:
        server["setup_status"] = "error"
        server["setup_error"] = str(exc)
        log(server_id, f"CurseForge setup failed: {exc}", "error")
        persist_servers()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def install_imported_server_pack(server_id: str, archive_path: Path, original_filename: str,
                                 metadata_overrides: dict | None = None):
    server = servers[server_id]
    root = Path(server["path"])
    staging = root / "_server-pack-staging"
    metadata_overrides = normalize_pack_metadata_overrides(metadata_overrides)
    try:
        log(server_id, f"Preparing imported server pack {original_filename}…")
        refresh_upstream_client_only_list(force=not bool(upstream_client_only_list.get("listVersion")))
        if not zipfile.is_zipfile(archive_path):
            raise ValueError("That file is not a valid ZIP archive.")
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            if "modrinth.index.json" in names:
                server["modpack"] = {"name": Path(original_filename).stem or "Imported Modrinth pack", "source": "modrinth"}
                install_modrinth_server(server_id, "", server["modpack"], archive_path, original_filename)
                return
            manifest = None
            manifest_path = None
            for archive_name in archive.namelist():
                normalized_name = archive_name.replace("\\", "/")
                if PurePosixPath(normalized_name).name.casefold() != "manifest.json":
                    continue
                try:
                    candidate = json.loads(archive.read(archive_name).decode("utf-8"))
                    if isinstance(candidate, dict) and isinstance(candidate.get("minecraft"), dict):
                        manifest = candidate
                        manifest_path = normalized_name
                        break
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
            extract_zip_safe(archive, staging)
            manifest_parent = PurePosixPath(manifest_path).parent if manifest_path else PurePosixPath(".")
            pack_root = staging.joinpath(*manifest_parent.parts) if manifest_parent.parts != (".",) else staging

        layouts = [staging] + [item for item in staging.iterdir() if item.is_dir()]
        layout = next((item for item in layouts if has_server_launcher(item)), None)
        if layout is None:
            if manifest and manifest.get("minecraft"):
                transform_curseforge_client_pack(server_id, archive_path, original_filename, manifest, staging,
                                                 pack_root, metadata_overrides)
                return
            # Generic client profile exports often contain mods/ and config/ but
            # omit the CurseForge manifest. Prism/MultiMC packs carry component
            # metadata that lets Blockyard install the matching dedicated loader.
            mods_source = next((item for item in staging.rglob("mods") if item.is_dir()), None)
            if mods_source:
                pack_root = mods_source.parent
                pack_metadata = None
                for metadata_path in staging.rglob("mmc-pack.json"):
                    try:
                        pack_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                        break
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        continue
                components = (pack_metadata or {}).get("components") or []
                component_versions = {str(item.get("uid") or "").lower(): str(item.get("version") or "")
                                      for item in components if isinstance(item, dict)}
                minecraft = component_versions.get("net.minecraft") or metadata_overrides.get("minecraft_version") or ""
                inferred = re.search(r"(?<!\d)(1\.\d+(?:\.\d+)?)(?!\d)", original_filename)
                minecraft = minecraft or (inferred.group(1) if inferred else "")
                loader_id = None
                for component, loader in (("net.fabricmc.fabric-loader", "fabric"),
                                          ("org.quiltmc.quilt-loader", "quilt"),
                                          ("net.minecraftforge", "forge"),
                                          ("net.neoforged", "neoforge")):
                    version = component_versions.get(component)
                    if version:
                        loader_id = f"{loader}-{version}"
                        break
                if not loader_id and metadata_overrides.get("loader_type") != "auto":
                    loader_id = manual_pack_loader_id(metadata_overrides)
                if not minecraft:
                    raise ValueError("This client pack has no Minecraft version metadata. Enter the version in the manual field and try again.")
                if not loader_id:
                    if metadata_overrides.get("loader_type") == "vanilla":
                        raise ValueError("This pack contains mod JARs, so a Vanilla server cannot load them. Choose the mod loader used by the client pack.")
                    raise ValueError("Blockyard found client mods but could not identify the server loader. Choose Fabric, Quilt, Forge, or NeoForge manually.")
                jar_count = sum(1 for item in mods_source.rglob("*.jar") if item.is_file())
                if not jar_count:
                    raise ValueError("This client pack does not contain any mod JARs to convert.")
                copy_curseforge_instance(pack_root, root)
                server["minecraft_version"] = minecraft
                server["pack_version"] = Path(original_filename).stem or "Imported client pack"
                loader_key = loader_id.split("-", 1)[0].lower()
                loader_labels = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric", "quilt": "Quilt"}
                server["loader"] = loader_labels.get(loader_key, loader_key.title())
                server["source"] = "local"
                log(server_id, f"Converting client profile files for Minecraft {minecraft} · {server['loader']}…")
                install_loader(server, minecraft, loader_id)
                server["mod_compatibility"] = scan_imported_mods(root, server["minecraft_version"], "unknown", server_id)
                server["setup_status"] = "ready"
                apply_properties(server)
                archive_path.unlink(missing_ok=True)
                log(server_id, "Client profile converted to a dedicated server folder. Review suggested removals in Mod compatibility, accept the EULA, then press Start.")
                return
            raise ValueError("No server launch script or server JAR was found. Import a complete author-provided server pack ZIP.")

        for item in list(layout.iterdir()):
            destination = root / item.name
            if destination.exists():
                raise ValueError(f"The server pack contains a conflicting path: {item.name}")
            shutil.move(str(item), str(destination))
        shutil.rmtree(staging, ignore_errors=True)

        minecraft = str((manifest or {}).get("minecraft", {}).get("version") or metadata_overrides.get("minecraft_version") or "")
        loader_entry = next((item for item in (manifest or {}).get("minecraft", {}).get("modLoaders", []) if item.get("primary")), None)
        if not loader_entry and (manifest or {}).get("minecraft", {}).get("modLoaders"):
            loader_entry = manifest["minecraft"]["modLoaders"][0]
        loader_id = str(loader_entry.get("id") or "") if loader_entry else ""
        if not loader_id and metadata_overrides.get("loader_type") != "auto":
            loader_id = manual_pack_loader_id(metadata_overrides) or ""
        inferred = re.search(r"(?<!\d)(1\.\d+(?:\.\d+)?)(?!\d)", original_filename)
        server["minecraft_version"] = minecraft or (inferred.group(1) if inferred else "Imported pack")
        server["pack_version"] = Path(original_filename).stem or "Imported server pack"
        loader_key = loader_id.split("-", 1)[0].lower() if loader_id else ""
        loader_labels = {"forge": "Forge", "neoforge": "NeoForge", "fabric": "Fabric", "quilt": "Quilt"}
        server["loader"] = loader_labels.get(
            loader_key, loader_key.title() if loader_key else
            ("Vanilla" if metadata_overrides.get("loader_type") == "vanilla" else "Included server files"))
        server["mod_compatibility"] = scan_imported_mods(root, server["minecraft_version"], loader_key or "unknown", server_id)
        server["setup_status"] = "ready"
        apply_properties(server)
        archive_path.unlink(missing_ok=True)
        log(server_id, "Server pack imported and ready. Review suggested removals in Mod compatibility, accept the EULA, then press Start.")
    except Exception as exc:
        server["setup_status"] = "error"
        server["setup_error"] = str(exc)
        log(server_id, f"Import failed: {exc}", "error")
        shutil.rmtree(staging, ignore_errors=True)
    finally:
        archive_path.unlink(missing_ok=True)
        persist_servers()


def process_reader(server_id: str, process: subprocess.Popen):
    recent_lines = []
    try:
        if process.stdout:
            for line in iter(process.stdout.readline, ""):
                text = line.rstrip()
                if text:
                    recent_lines.append(text[:1200])
                    recent_lines = recent_lines[-300:]
                    log(server_id, text[:1200], "error" if "ERROR" in text.upper() or "EXCEPTION" in text.upper() else "info")
    except Exception as exc:
        log(server_id, f"Console reader stopped: {exc}", "warning")
    finally:
        code = process.poll()
        launch_script = None
        with state_lock:
            slot = runtime.setdefault(server_id, {"logs": [], "status": "offline", "process": None, "log_seq": 0})
            if slot.get("process") is process:
                slot["status"] = "offline"
                slot["process"] = None
                launch_script = slot.pop("managed_launch_script", None)
        if launch_script:
            try:
                Path(launch_script).unlink(missing_ok=True)
            except OSError:
                pass
        if code is not None:
            learn_client_mod_from_crash(server_id, recent_lines)
            log(server_id, f"Server process exited (code {code}).", "warning" if code else "info")
            persist_servers()


def learn_client_mod_from_crash(server_id: str, recent_lines: list[str]):
    """Use clear client-class or known client-only loader failures as review suggestions."""
    text = "\n".join(recent_lines)
    missing_client_class = re.search(
        r"(?:ClassNotFoundException|NoClassDefFoundError)\s*:?\s*(net\.minecraft\.client\.[\w.$/]+)",
        text,
        re.IGNORECASE,
    )
    mod_id = ""
    if missing_client_class:
        mod_mentions = re.findall(r"\bfrom mod\s+([a-zA-Z0-9_.-]+)", text, re.IGNORECASE)
        if mod_mentions:
            mod_id = mod_mentions[-1].lower()
    else:
        failed_ids = re.findall(
            r"\b[A-Za-z0-9_.-]+\s+\(([a-zA-Z0-9_.-]+)\)\s+has failed to load correctly",
            text,
            re.IGNORECASE,
        )
        known_ids = set(KNOWN_CLIENT_ONLY_MOD_IDS)
        known_ids.update(
            str(value).lower()
            for item in client_only_manifest.get("mods", [])
            if isinstance(item, dict)
            for value in (item.get("mod_ids") if isinstance(item.get("mod_ids"), list) else [])
        )
        mod_id = next((value.lower() for value in reversed(failed_ids) if value.lower() in known_ids), "")
        if not mod_id:
            return
    if not mod_id:
        return
    if missing_client_class:
        reason = f"Startup crash referenced {missing_client_class.group(1)} from mod {mod_id}."
    else:
        known_reason = KNOWN_CLIENT_ONLY_MOD_IDS.get(mod_id, "previously identified as client-only")
        reason = f"Startup loader failure identified client-only mod {mod_id}: {known_reason}."
    record_client_only_mod(None, [mod_id], reason, "startup-crash")
    server = servers.get(server_id)
    if not server:
        return
    root = Path(server.get("path", ""))
    matches = []
    mods_root = root / "mods"
    if not mods_root.is_dir():
        return
    for jar in mods_root.rglob("*.jar"):
        try:
            jar.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        metadata = inspect_mod_jar(jar)
        file_id = re.sub(r"[^a-z0-9]", "", normalize_mod_filename(jar.name))
        normalized_id = re.sub(r"[^a-z0-9]", "", mod_id)
        if mod_id in metadata["mod_ids"] or file_id.startswith(normalized_id):
            matches.append((jar, metadata))
    if len(matches) != 1:
        return
    jar, metadata = matches[0]
    relative = jar.relative_to(root).as_posix()
    record_client_only_mod(jar, metadata["mod_ids"] or [mod_id], reason, "startup-crash")
    report = server.get("mod_compatibility") or scan_imported_mods(root, str(server.get("minecraft_version") or ""), "unknown", server_id)
    mods = report.setdefault("mods", [])
    entry = next((item for item in mods if str(item.get("path", "")).casefold() == relative.casefold()), None)
    if entry is None:
        entry = {"name": jar.name, "path": relative, "installed": True, "quarantined": False,
                 "backup_path": "", "alternative": None}
        mods.append(entry)
    entry.update({"status": "Client only", "installed": True, "quarantined": False, "reason": reason})
    report["counts"] = {
        "total": len(mods),
        "server_compatible": sum(item.get("status") == "Server compatible" for item in mods),
        "client_only": sum(item.get("status") == "Client only" for item in mods),
        "removed_client_only": sum(bool(item.get("quarantined")) for item in mods),
        "unverified": sum(item.get("status") == "Unverified" for item in mods),
        "alternatives_found": sum(bool(item.get("alternative")) for item in mods),
    }
    report["checked_at"] = utc_now()
    server["mod_compatibility"] = report
    log(server_id, f"Crash log points to client-only mod {jar.name}. Review Mod compatibility and confirm removal if appropriate.", "warning")
    persist_servers()


def refresh_saved_client_only_classifications():
    """Reclassify saved reports when client-only evidence or curated rules change."""
    changed = False
    for server_id, server in servers.items():
        report = server.get("mod_compatibility") or {}
        mods = report.get("mods") if isinstance(report, dict) else None
        if not isinstance(mods, list) or not server.get("path"):
            continue
        root = Path(str(server["path"]))
        try:
            root_resolved = root.resolve(strict=True)
        except OSError:
            continue
        server_changed = False
        for item in mods:
            if not isinstance(item, dict) or not item.get("installed") or item.get("quarantined"):
                continue
            was_client_only = item.get("status") == "Client only"
            was_curated_candidate = (was_client_only and item.get("classification_source") == "curated-list")
            was_manifest_candidate = (was_client_only and item.get("classification_source") == "Blockyard manifest")
            was_protected_candidate = item.get("status") == "Protected library"
            relative = PurePosixPath(str(item.get("path") or "").replace("\\", "/"))
            if relative.is_absolute() or not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
                continue
            try:
                jar = root.joinpath(*relative.parts).resolve(strict=True)
                jar.relative_to(root_resolved)
            except (OSError, ValueError):
                continue
            if not jar.is_file() or jar.suffix.lower() != ".jar":
                continue
            loader_key = str(server.get("loader") or report.get("loader") or "").casefold().split(" ", 1)[0]
            minecraft = str(report.get("minecraft") or server.get("minecraft_version") or "")
            project_id = str(item.get("modrinth_project_id") or "")
            metadata = inspect_mod_jar(
                jar, loader_key, minecraft,
                modrinth_project_ids=[project_id] if project_id else (),
            )
            if metadata.get("protected"):
                item.update({"status": "Protected library", "reason": metadata["reason"],
                             "classification_source": "curated-list-protection",
                             "protected": True, "protected_name": metadata.get("protected_name", ""),
                             "installed": True, "quarantined": False, "backup_path": ""})
                server_changed = True
                continue
            if was_protected_candidate:
                status = ("Client only" if metadata["environment"] == "client_only" else
                          "Server compatible" if environment_supports_server(metadata["environment"]) else "Unverified")
                item.update({"status": status, "reason": metadata["reason"],
                             "classification_source": metadata.get("classification_source", ""),
                             "protected": False, "protected_name": "",
                             "installed": True, "quarantined": False, "backup_path": ""})
                if status == "Client only":
                    record_client_only_mod(jar, metadata["mod_ids"], metadata["reason"],
                                           metadata.get("classification_source") or "metadata-or-manifest")
                server_changed = True
                continue
            if was_client_only and not was_curated_candidate and not was_manifest_candidate:
                continue
            if metadata["environment"] != "client_only":
                if was_curated_candidate or was_manifest_candidate:
                    status = "Server compatible" if environment_supports_server(metadata["environment"]) else "Unverified"
                    item.update({"status": status, "reason": metadata["reason"],
                                 "classification_source": metadata.get("classification_source", ""),
                                 "installed": True, "quarantined": False, "backup_path": ""})
                    server_changed = True
                continue
            reason = metadata["reason"] or "Client-only mod recorded in Blockyard's manifest"
            item.update({"status": "Client only", "reason": reason, "installed": True,
                         "quarantined": False, "backup_path": "",
                         "classification_source": metadata.get("classification_source", "")})
            record_client_only_mod(jar, metadata["mod_ids"], reason,
                                   metadata.get("classification_source") or "metadata-or-manifest")
            log(server_id, f"Found a likely client-only mod for review: {jar.name} ({reason}). Confirm its removal in Mod compatibility.", "warning")
            server_changed = True
        list_version = upstream_client_only_list.get("listVersion", "")
        if report.get("client_mod_list_version", "") != list_version:
            report["client_mod_list_version"] = list_version
            server_changed = True
        if report.get("client_mod_list_source") != CLIENT_ONLY_LIST_SOURCE_URL:
            report["client_mod_list_source"] = CLIENT_ONLY_LIST_SOURCE_URL
            server_changed = True
        if not server_changed:
            continue
        report["counts"] = {
            "total": len(mods),
            "server_compatible": sum(item.get("status") == "Server compatible" for item in mods),
            "client_only": sum(item.get("status") == "Client only" for item in mods),
            "removed_client_only": sum(bool(item.get("quarantined")) for item in mods),
            "unverified": sum(str(item.get("status", "")).startswith("Unverified") for item in mods),
            "alternatives_found": sum(bool(item.get("alternative")) for item in mods),
        }
        report["checked_at"] = utc_now()
        report["client_mod_list_version"] = list_version
        server["mod_compatibility"] = report
        changed = True
    if changed:
        persist_servers()


def start_process(server_id: str):
    with state_lock:
        server = servers.get(server_id)
        if not server:
            raise ValueError("Server not found.")
        slot = runtime.setdefault(server_id, {"logs": [], "status": "offline", "process": None, "log_seq": 0})
        process = slot.get("process")
        if process and process.poll() is None:
            raise ValueError("This server is already running.")
        if server.get("setup_status") != "ready":
            raise ValueError("Wait for server setup to finish before starting it.")
        if not server.get("settings", {}).get("eula_accepted"):
            raise ValueError("Accept the Minecraft EULA in Server settings before starting this server.")
        root = Path(server["path"])
        modpack = server.get("modpack") if isinstance(server.get("modpack"), dict) else {}
        if server.get("source") == "local" and modpack.get("source") != "modrinth":
            loader_name = str(server.get("loader") or "").lower()
            loader_key = next((key for key in ("neoforge", "forge", "fabric", "quilt") if key in loader_name), "unknown")
            report = scan_imported_mods(root, str(server.get("minecraft_version") or ""), loader_key, server_id)
            remaining_client_only = [item["name"] for item in report["mods"]
                                     if item["status"] == "Client only" and item["installed"]]
            server["mod_compatibility"] = report
            persist_servers()
            if remaining_client_only:
                raise ValueError("Review Mod compatibility and confirm removal of the flagged client-only mods before starting: " + ", ".join(remaining_client_only))
        apply_properties(server)
        configured_java = str(server.get("settings", {}).get("java_path", "java")).strip() or "java"
        java_info = select_java_for_minecraft(str(server.get("minecraft_version") or ""), configured_java)
        if not java_info["path"]:
            raise ValueError(f"No Java executable was found on this computer. Blockyard checked PATH, JAVA_HOME, common install folders, Windows Java registry entries, and Minecraft Launcher runtimes. Install a Java runtime compatible with Minecraft {server.get('minecraft_version', '')}, or set its full path in Properties → Java executable.")
        java = java_info["path"]
        server.setdefault("settings", {})["java_path"] = java
        memory = max(1024, min(131072, int(server.get("settings", {}).get("ram_mb", 4096))))
        args_file = root / "user_jvm_args.txt"
        if args_file.exists():
            args_file.write_text(f"-Xms{min(2048, memory)}M\n-Xmx{memory}M\n", encoding="utf-8")
        files = {p.name.lower(): p.name for p in root.iterdir() if p.is_file()}
        bat = next((files[x] for x in ("run.bat", "startserver.bat", "serverstart.bat", "start.bat") if x in files), None)
        cmd = next((files[x] for x in ("run.cmd", "startserver.cmd", "serverstart.cmd", "start.cmd") if x in files), None)
        sh = next((files[x] for x in ("run.sh", "startserver.sh", "serverstart.sh", "start.sh") if x in files), None)
        managed_launch_script = None
        if bat or cmd:
            script_name = bat or cmd
            source_script = root / script_name
            try:
                raw_script = source_script.read_bytes()
                encoding = "utf-8-sig"
                try:
                    script_text = raw_script.decode(encoding)
                except UnicodeDecodeError:
                    encoding = "mbcs" if os.name == "nt" else "cp1252"
                    script_text = raw_script.decode(encoding, "replace")
                # Many Forge and NeoForge launchers end with `pause`. That leaves
                # cmd.exe alive after Java crashes, making the server look running.
                no_pause = re.sub(r"(?im)^[ \t]*@?[ \t]*pause\b[^\r\n]*(?:\r?\n|$)", "", script_text)
                has_utf8_bom = raw_script.startswith(b"\xef\xbb\xbf")
                if no_pause != script_text or has_utf8_bom:
                    managed_launch_script = root / f".blockyard-launch-{server_id}-{uuid.uuid4().hex[:8]}.bat"
                    # Decoding as utf-8-sig already removed the BOM. Writing
                    # with utf-8-sig would add it back, which cmd.exe then
                    # mistakes for part of the first command (for example,
                    # treating ﻿@echo as an unknown command).
                    write_encoding = "utf-8" if encoding == "utf-8-sig" else encoding
                    managed_launch_script.write_text(no_pause, encoding=write_encoding, newline="")
                    script_name = managed_launch_script.name
            except OSError:
                managed_launch_script = None
            command = ["cmd.exe", "/d", "/c", script_name, "nogui"]
        elif sh:
            command = ["bash", sh, "nogui"]
        else:
            candidates = ["server.jar", "fabric-server-launch.jar", "quilt-server-launch.jar"]
            jar = next((x for x in candidates if (root / x).exists()), None)
            if not jar:
                jars = [x for x in root.glob("*.jar") if "installer" not in x.name.lower()]
                if len(jars) == 1:
                    jar = jars[0].name
            if not jar:
                raise ValueError("No server launch file was found in this install folder.")
            command = [java, f"-Xms{min(2048, memory)}M", f"-Xmx{memory}M", "-jar", jar, "nogui"]
        try:
            child_env = os.environ.copy()
            java_file = Path(java)
            child_env["PATH"] = str(java_file.parent) + os.pathsep + child_env.get("PATH", "")
            child_env["JAVA_HOME"] = str(java_file.parent.parent)
            proc = subprocess.Popen(command, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env)
        except FileNotFoundError as exc:
            if managed_launch_script:
                managed_launch_script.unlink(missing_ok=True)
            executable = str(command[0])
            if Path(executable).name.lower() in ("java", "java.exe"):
                raise ValueError(f"Java executable '{java}' was not found. Install a Java runtime compatible with Minecraft {server.get('minecraft_version', '')}, then set its full java.exe path in Properties → Java executable.") from exc
            if executable.lower() in ("cmd.exe", "bash"):
                raise ValueError(f"The server launch script could not run because '{executable}' is unavailable on this computer.") from exc
            raise ValueError(f"Could not launch '{executable}'. Check that the server files are complete and the launcher exists.") from exc
        slot["process"] = proc
        slot["status"] = "running"
        if managed_launch_script:
            slot["managed_launch_script"] = str(managed_launch_script)
        server["last_started_at"] = utc_now()
        persist_servers()
    log(server_id, f"Server process started (PID {proc.pid}).")
    log(server_id, f"Using Java {java_info['version']} from {java_info['source']} for Minecraft {server.get('minecraft_version', 'unknown')}: {java}")
    threading.Thread(target=process_reader, args=(server_id, proc), daemon=True).start()


def stop_process(server_id: str, force: bool = False):
    with state_lock:
        slot = runtime.get(server_id)
        proc = slot.get("process") if slot else None
        process_running = bool(proc and proc.poll() is None)
    if not process_running:
        with state_lock:
            slot = runtime.get(server_id)
            if slot:
                slot["status"] = "offline"
                slot["process"] = None
        return
    if force:
        log(server_id, "Force stopping server process…", "warning")
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.kill()
    else:
        log(server_id, "Sending stop command…")
        try:
            proc.stdin.write("stop\n")
            proc.stdin.flush()
        except (AttributeError, OSError):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                proc.terminate()
    try:
        proc.wait(timeout=30 if not force else 10)
    except subprocess.TimeoutExpired:
        log(server_id, "Graceful stop timed out; force stopping process tree.", "warning")
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.kill()
    with state_lock:
        slot = runtime.setdefault(server_id, {"logs": [], "status": "offline", "process": None, "log_seq": 0})
        if slot.get("process") is proc:
            slot["process"] = None
            slot["status"] = "offline"
    log(server_id, "Server stopped.")
    persist_servers()


class BlockyardHTTPServer(ThreadingHTTPServer):
    # ThreadingHTTPServer enables SO_REUSEADDR by default. On Windows that can
    # allow multiple Blockyard copies to bind the same loopback port, causing
    # requests to alternate between stale in-memory settings and server data.
    allow_reuse_address = False


class Handler(BaseHTTPRequestHandler):
    server_version = "Blockyard/1.0"
    protocol_version = "HTTP/1.1"

    def _valid_local_host(self):
        host = str(self.headers.get("Host", "")).lower()
        return host in {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    def _valid_local_origin(self):
        origin = self.headers.get("Origin")
        return origin is None or origin.rstrip("/").lower() in {
            f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"
        }

    def log_message(self, fmt, *args):
        sys.stdout.write("[Blockyard] " + (fmt % args) + "\n")

    def _write_all(self, data):
        """Write a complete response body even when the unbuffered socket writes partially."""
        remaining = memoryview(data)
        while remaining:
            written = self.wfile.write(remaining)
            if written is None or written <= 0:
                raise ConnectionError("The browser connection closed during the response.")
            remaining = remaining[written:]

    def send_json(self, data, status=200, extra_headers=None):
        raw = json.dumps(data).encode("utf-8")
        accept_encoding = self.headers.get("Accept-Encoding", "")
        accepts_gzip = any(
            item.strip().split(";", 1)[0].lower() == "gzip"
            and not re.search(r"(?:^|;)\s*q=0(?:\.0*)?(?:;|$)", item, re.IGNORECASE)
            for item in accept_encoding.split(",")
        )
        compressed = accepts_gzip and len(raw) >= 1024
        if compressed:
            raw = gzip.compress(raw, compresslevel=6, mtime=0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Vary", "Accept-Encoding")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "no-store")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self._write_all(raw)

    def read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_TEXT_FILE_BYTES * 3:
            raise ValueError("Request body is too large.")
        return json.loads(self.rfile.read(length) or b"{}")

    def import_server_pack_upload(self, parsed_url):
        query = urllib.parse.parse_qs(parsed_url.query)
        metadata_overrides = normalize_pack_metadata_overrides({
            "minecraft_version": (query.get("minecraft_version") or [""])[0],
            "loader_type": (query.get("loader_type") or ["auto"])[0],
            "loader_version": (query.get("loader_version") or [""])[0],
        })
        length_text = self.headers.get("Content-Length")
        transfer_encoding = str(self.headers.get("Transfer-Encoding", "")).lower()
        try:
            declared_size = int(length_text) if length_text else int((query.get("file_size") or ["0"])[0])
        except ValueError as exc:
            raise ValueError("Blockyard could not read the uploaded archive size.") from exc
        if declared_size <= 0 or declared_size > MAX_PACK_BYTES:
            raise ValueError("Choose a non-empty server-pack ZIP smaller than 2 GB.")
        name, target = validate_root((query.get("name") or [""])[0], (query.get("root") or [""])[0])
        remember_install_root(target)
        filename = (query.get("filename") or ["server-pack.zip"])[0].replace("\\", "/").rsplit("/", 1)[-1][:255]
        if not filename.lower().endswith((".zip", ".mrpack")):
            raise ValueError("Choose a .zip or Modrinth .mrpack archive.")
        target.mkdir(parents=True, exist_ok=True)
        archive_path = target / "_blockyard-server-pack-upload.zip"
        try:
            with archive_path.open("xb") as output:
                if "chunked" in transfer_encoding:
                    written = 0
                    while True:
                        size_line = self.rfile.readline(65536)
                        if not size_line:
                            raise ValueError("The server-pack upload ended before the full file arrived.")
                        try:
                            chunk_size = int(size_line.split(b";", 1)[0].strip(), 16)
                        except ValueError as exc:
                            raise ValueError("The browser sent an invalid chunked archive upload.") from exc
                        if chunk_size == 0:
                            while self.rfile.readline(65536) not in (b"\r\n", b"\n", b""):
                                pass
                            break
                        written += chunk_size
                        if written > MAX_PACK_BYTES:
                            raise ValueError("Choose a modpack archive smaller than 2 GB.")
                        remaining = chunk_size
                        while remaining:
                            block = self.rfile.read(min(1024 * 1024, remaining))
                            if not block:
                                raise ValueError("The server-pack upload ended before the full file arrived.")
                            output.write(block)
                            remaining -= len(block)
                        if self.rfile.read(2) != b"\r\n":
                            raise ValueError("The browser sent an invalid chunked archive upload.")
                    if written != declared_size:
                        raise ValueError("The uploaded archive size did not match the selected file.")
                else:
                    remaining = declared_size
                    while remaining:
                        block = self.rfile.read(min(1024 * 1024, remaining))
                        if not block:
                            raise ValueError("The server-pack upload ended before the full file arrived.")
                        output.write(block)
                        remaining -= len(block)
            if not zipfile.is_zipfile(archive_path):
                raise ValueError("That file is not a valid ZIP archive.")

            sid = str(uuid.uuid4())[:8]
            pack_name = Path(filename).stem or "Imported server pack"
            item = {
                "id": sid, "name": name, "path": str(target), "source": "local",
                "modpack": {"name": pack_name, "source": "local"},
                "minecraft_version": "Detecting…", "pack_version": "Imported server pack", "loader": "Resolving…",
                "setup_status": "installing", "setup_error": "", "created_at": utc_now(), "updated_at": utc_now(),
                "settings": {"port": 25565, "motd": name, "max_players": 20, "difficulty": "normal", "gamemode": "survival", "view_distance": 10,
                             "simulation_distance": 10, "online_mode": True, "whitelist": False, "pvp": True, "allow_flight": False,
                             "ram_mb": 4096, "java_path": "java", "eula_accepted": False},
            }
            with state_lock:
                servers[sid] = item
                runtime[sid] = {"logs": [], "status": "installing", "process": None, "log_seq": 0}
            log(sid, f"Uploaded {filename}; checking for a complete server layout.")
            persist_servers()
            threading.Thread(target=install_imported_server_pack,
                             args=(sid, archive_path, filename, metadata_overrides), daemon=True).start()
            return self.send_json({"server": public_server(sid)}, 201)
        except Exception:
            archive_path.unlink(missing_ok=True)
            raise

    def check_server_pack_upload(self, body):
        normalize_pack_metadata_overrides(body)
        filename = str(body.get("filename") or "").replace("\\", "/").rsplit("/", 1)[-1][:255]
        if not filename.lower().endswith((".zip", ".mrpack")):
            raise ValueError("Choose a .zip or Modrinth .mrpack archive.")
        try:
            length = int(body.get("file_size") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Blockyard could not read the selected archive size.") from exc
        if length <= 0 or length > MAX_PACK_BYTES:
            raise ValueError("Choose a non-empty modpack archive smaller than 2 GB.")
        name, target = validate_root(body.get("name", ""), body.get("root", ""))
        return {"ok": True, "name": name, "target": str(target)}

    def upload_server_file(self, server_id: str, parsed_url):
        query = urllib.parse.parse_qs(parsed_url.query)
        relative_folder = (query.get("path") or [""])[0]
        filename = safe_server_filename((query.get("filename") or [""])[0])
        length_text = self.headers.get("Content-Length")
        if not length_text:
            raise ValueError("The upload did not include a file size.")
        length = int(length_text)
        if length <= 0 or length > MAX_SERVER_FILE_UPLOAD_BYTES:
            raise ValueError("Choose a non-empty file no larger than 512 MB.")
        _, folder = server_file_path(server_id, relative_folder)
        if not folder.is_dir():
            raise ValueError("Choose an existing server folder for the upload.")
        target = folder / filename
        # Resolve before writing so an existing symlink cannot redirect writes out of the server folder.
        _, target = server_file_path(server_id, (PurePosixPath(relative_folder) / filename).as_posix())
        if target.exists():
            raise ValueError("A file with that name already exists. Delete or rename it first.")
        target.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        try:
            with target.open("xb") as output:
                while written < length:
                    block = self.rfile.read(min(1024 * 1024, length - written))
                    if not block:
                        raise ValueError("The upload ended before the full file arrived.")
                    output.write(block)
                    written += len(block)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        log(server_id, f"Uploaded file {target.name}.")
        return {"ok": True, "name": target.name, "size": written}

    def do_GET(self):
        global client_only_manifest
        if not self._valid_local_host():
            self.close_connection = True
            return self.send_json({"error": "Blockyard only accepts local browser requests."}, 421,
                                  {"Connection": "close"})
        url = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(url.path)
        query = urllib.parse.parse_qs(url.query)
        if path == "/api/health":
            return self.send_json({"ok": True, "app": "Blockyard", "port": PORT})
        if path == "/api/config":
            return self.send_json(public_config())
        if path == "/api/client-only-manifest":
            try:
                with state_lock:
                    parsed = json.loads(CLIENT_ONLY_MANIFEST_FILE.read_text(encoding="utf-8"))
                    client_only_manifest = validate_client_only_manifest(parsed)
                    content = json.dumps(client_only_manifest, indent=2)
                return self.send_json({"path": str(CLIENT_ONLY_MANIFEST_FILE.resolve()), "content": content})
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                return self.send_json({"error": f"Could not load a valid client-only manifest: {exc}"}, 400)
        if path == "/api/java/detect":
            return self.send_json(detect_java())
        if path == "/api/java/list":
            return self.send_json({"runtimes": detect_java_runtimes(refresh=(query.get("refresh") or [""])[0] == "1")})
        if path == "/api/servers":
            return self.send_json({"servers": [public_server(x) for x in list(servers.keys())]})
        if path == "/api/curseforge/search":
            q = (query.get("q") or [""])[0].strip()
            if len(q) > 100:
                return self.send_json({"error": "Search query is too long."}, 400)
            try:
                result = curseforge_request("/mods/search", {
                    "gameId": 432, "classId": 4471, "searchFilter": q,
                    "pageSize": 24, "index": 0,
                })
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
            data = result.get("data", []) if isinstance(result, dict) else []
            hits = []
            for project in data:
                if not isinstance(project, dict):
                    continue
                logo = project.get("logo") or {}
                hits.append({
                    "id": project.get("id"), "provider": "curseforge",
                    "name": project.get("name") or "Unnamed modpack",
                    "summary": project.get("summary") or "CurseForge Minecraft modpack",
                    "downloads": project.get("downloadCount", 0),
                    "icon": logo.get("thumbnailUrl") if isinstance(logo, dict) else "",
                    "url": project.get("links", {}).get("websiteUrl", "") if isinstance(project.get("links"), dict) else "",
                })
            return self.send_json({"results": hits, "total": len(hits)})
        match = re.fullmatch(r"/api/curseforge/mods/(\d+)/files", path)
        if match:
            mod_id = int(match.group(1))
            try:
                result = curseforge_request(f"/mods/{mod_id}/files", {"pageSize": 50, "index": 0})
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
            files = []
            for item in result.get("data", []) if isinstance(result, dict) else []:
                if not isinstance(item, dict):
                    continue
                game_versions = item.get("gameVersions") or []
                files.append({
                    "id": item.get("id"), "name": item.get("displayName") or item.get("fileName") or "Release",
                    "filename": item.get("fileName") or "", "date": item.get("fileDate") or "",
                    "game_versions": game_versions[:4], "download_count": item.get("downloadCount", 0),
                    "is_server_pack": bool(item.get("isServerPack")),
                    "server_pack_file_id": item.get("serverPackFileId"),
                    "release_type": item.get("releaseType"),
                })
            return self.send_json({"files": files})
        if path == "/api/modrinth/search":
            q = (query.get("q") or [""])[0].strip()
            if len(q) > 100:
                return self.send_json({"error": "Search query is too long."}, 400)
            facets = json.dumps([["project_type:modpack"]], separators=(",", ":"))
            try:
                data = modrinth_request("/search", {"query": q, "facets": facets, "limit": 24, "index": "relevance"})
            except (ValueError, urllib.error.URLError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            hits = []
            for project in data.get("hits", []):
                slug = project.get("slug") or project.get("project_id")
                hits.append({"id": project.get("project_id"), "provider": "modrinth", "name": project.get("title"), "slug": slug, "summary": project.get("description"), "downloads": project.get("downloads", 0), "icon": project.get("icon_url"), "url": f"https://modrinth.com/modpack/{slug}"})
            return self.send_json({"results": hits, "total": data.get("total_hits", len(hits))})
        match = re.fullmatch(r"/api/modrinth/projects/([\w-]+)/versions", path)
        if match:
            project_id = match.group(1)
            try:
                result = modrinth_request(f"/project/{urllib.parse.quote(project_id, safe='')}/version", {"include_changelog": "false"})
            except (ValueError, urllib.error.URLError) as exc:
                return self.send_json({"error": str(exc)}, 400)
            files = []
            for version in result[:50]:
                modpack_file = next((item for item in version.get("files", []) if str(item.get("filename", "")).lower().endswith(".mrpack")), None)
                if not modpack_file:
                    continue
                files.append({"id": version.get("id"), "name": version.get("name") or version.get("version_number"), "filename": modpack_file.get("filename"), "date": version.get("date_published"), "game_versions": version.get("game_versions", []), "loaders": version.get("loaders", []), "release_type": version.get("version_type"), "primary": bool(modpack_file.get("primary")), "download_count": version.get("downloads", 0)})
            return self.send_json({"files": files})
        match = re.fullmatch(r"/api/servers/([\w-]+)", path)
        if match and match.group(1) in servers:
            return self.send_json({"server": public_server(match.group(1))})
        match = re.fullmatch(r"/api/servers/([\w-]+)/logs", path)
        if match and match.group(1) in servers:
            sid = match.group(1)
            after = int((query.get("after") or ["0"])[0])
            with state_lock:
                slot = runtime.setdefault(sid, {"logs": [], "status": "offline", "process": None, "log_seq": 0})
                new_logs = [x for x in slot["logs"] if x["seq"] > after]
                return self.send_json({"logs": new_logs, "seq": slot["log_seq"], "status": slot["status"]})
        match = re.fullmatch(r"/api/servers/([\w-]+)/files", path)
        if match:
            try:
                return self.send_json(server_file_listing(match.group(1), (query.get("path") or [""])[0]))
            except (ValueError, OSError) as exc:
                return self.send_json({"error": str(exc)}, 400)
        match = re.fullmatch(r"/api/servers/([\w-]+)/files/read", path)
        if match:
            try:
                _, file_path = server_file_path(match.group(1), (query.get("path") or [""])[0])
                if not file_path.is_file():
                    raise ValueError("That file does not exist.")
                if file_path.stat().st_size > MAX_TEXT_FILE_BYTES:
                    raise ValueError("That file is too large to edit in Blockyard (maximum 2 MB).")
                content_bytes = file_path.read_bytes()
                if b"\0" in content_bytes:
                    raise ValueError("That file appears to be binary and cannot be edited here.")
                try:
                    content = content_bytes.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError("That file is not UTF-8 text and cannot be edited here.") from exc
                return self.send_json({"path": file_path.name, "content": content})
            except (ValueError, OSError) as exc:
                return self.send_json({"error": str(exc)}, 400)
        match = re.fullmatch(r"/api/servers/([\w-]+)/files/download", path)
        if match:
            try:
                _, file_path = server_file_path(match.group(1), (query.get("path") or [""])[0])
                if not file_path.is_file():
                    raise ValueError("That file does not exist.")
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(file_path.stat().st_size))
                self.send_header("Content-Disposition", "attachment; filename=\"download\"; filename*=UTF-8''" + urllib.parse.quote(file_path.name))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with file_path.open("rb") as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        self._write_all(block)
                return
            except (ValueError, OSError) as exc:
                return self.send_json({"error": str(exc)}, 400)
        asset = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}.get(path)
        if asset:
            file_path = APP_DIR / asset
            raw = file_path.read_bytes()
            mime = "text/html; charset=utf-8" if asset.endswith(".html") else "text/javascript; charset=utf-8" if asset.endswith(".js") else "text/css; charset=utf-8"
            accept_encoding = self.headers.get("Accept-Encoding", "")
            accepts_gzip = any(
                item.strip().split(";", 1)[0].lower() == "gzip"
                and not re.search(r"(?:^|;)\s*q=0(?:\.0*)?(?:;|$)", item, re.IGNORECASE)
                for item in accept_encoding.split(",")
            )
            compressed = accepts_gzip and len(raw) >= 1024
            if compressed:
                raw = gzip.compress(raw, compresslevel=6, mtime=0)
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Vary", "Accept-Encoding")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self._write_all(raw)
            return
        self.send_json({"error": "Not found."}, 404)

    def do_POST(self):
        if not self._valid_local_host() or not self._valid_local_origin():
            # Drain small rejected request bodies before closing the HTTP/1.0
            # connection; otherwise Windows may reset it before the 403 lands.
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= 1024 * 1024:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
            self.close_connection = True
            return self.send_json({"error": "Blockyard only accepts local browser requests."}, 403,
                                  {"Connection": "close"})
        parsed_url = urllib.parse.urlparse(self.path)
        request_path = urllib.parse.unquote(parsed_url.path)
        if request_path == "/api/shutdown":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if 0 < length <= 1024 * 1024:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
            self.close_connection = True
            self.send_json({"ok": True, "message": "Blockyard is shutting down."},
                           extra_headers={"Connection": "close"})
            # BaseServer.shutdown() must run from a thread other than serve_forever.
            threading.Thread(target=self.server.shutdown, name="blockyard-shutdown", daemon=True).start()
            return
        if request_path == "/api/servers/import-pack/check":
            try:
                return self.send_json(self.check_server_pack_upload(self.read_body()))
            except (ValueError, OSError) as exc:
                return self.send_json({"error": str(exc)}, 400)
        if request_path == "/api/client-only-manifest":
            try:
                body = self.read_body()
                content = body.get("content") if isinstance(body, dict) else None
                if not isinstance(content, str):
                    raise ValueError("Manifest content must be provided as JSON text.")
                try:
                    content_size = len(content.encode("utf-8"))
                except UnicodeEncodeError as exc:
                    raise ValueError("Manifest contains invalid Unicode text.") from exc
                if content_size > MAX_TEXT_FILE_BYTES:
                    raise ValueError("Manifest content is too large (maximum 2 MB).")
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON on line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
                normalized = validate_client_only_manifest(parsed)
                replace_client_only_manifest(normalized)
                return self.send_json({"ok": True, "path": str(CLIENT_ONLY_MANIFEST_FILE.resolve()),
                                       "content": json.dumps(normalized, indent=2)})
            except (ValueError, OSError) as exc:
                return self.send_json({"error": str(exc)}, 400)
        if urllib.parse.unquote(parsed_url.path) == "/api/servers/import-pack":
            try:
                return self.import_server_pack_upload(parsed_url)
            except (ValueError, OSError) as exc:
                return self.send_json({"error": str(exc)}, 400)
        match = re.fullmatch(r"/api/servers/([\w-]+)/files/upload", request_path)
        if match:
            try:
                return self.send_json(self.upload_server_file(match.group(1), parsed_url), 201)
            except (ValueError, OSError) as exc:
                return self.send_json({"error": str(exc)}, 400)
        try:
            body = self.read_body()
            path = request_path
            match = re.fullmatch(r"/api/servers/([\w-]+)/mods/confirm-removal", path)
            if match:
                sid = match.group(1)
                if sid not in servers:
                    return self.send_json({"error": "Server not found."}, 404)
                refresh_upstream_client_only_list(force=not bool(upstream_client_only_list.get("listVersion")))
                server = servers[sid]
                if server.get("setup_status") != "ready":
                    raise ValueError("Wait for server preparation to finish before changing its mods.")
                with state_lock:
                    process = runtime.setdefault(sid, {"logs": [], "status": "offline", "process": None, "log_seq": 0}).get("process")
                    if process and process.poll() is None:
                        raise ValueError("Stop the server before removing mods.")
                paths = body.get("paths")
                if not isinstance(paths, list) or not paths or len(paths) > 500 or any(not isinstance(value, str) for value in paths):
                    raise ValueError("Choose one or more flagged mods to confirm.")
                report = server.get("mod_compatibility") or {}
                mods = report.get("mods", [])
                pending = {str(item.get("path", "")): item for item in mods
                           if item.get("status") == "Client only" and item.get("installed") and not item.get("quarantined")}
                requested = list(dict.fromkeys(paths))
                if any(value not in pending for value in requested):
                    raise ValueError("One or more selected mods are no longer awaiting confirmation. Refresh the page and review the current list.")
                root, _ = server_file_path(sid)
                targets = []
                loader_key = str(server.get("loader") or report.get("loader") or "").casefold().split(" ", 1)[0]
                minecraft = str(report.get("minecraft") or server.get("minecraft_version") or "")
                for relative in requested:
                    parts = PurePosixPath(relative.replace("\\", "/"))
                    if parts.is_absolute() or any(part in ("", ".", "..") for part in parts.parts) or parts.suffix.lower() != ".jar" or not parts.parts or parts.parts[0].lower() != "mods":
                        raise ValueError("A selected mod path is invalid.")
                    raw_target = root.joinpath(*parts.parts)
                    cursor = root
                    for part in parts.parts:
                        cursor = cursor / part
                        if cursor.is_symlink():
                            raise ValueError("Blockyard will not move mod JARs that are symbolic links.")
                    _, target = server_file_path(sid, relative)
                    try:
                        target.relative_to((root / "mods").resolve())
                    except ValueError as exc:
                        raise ValueError("A selected file is outside the server mods folder.") from exc
                    if not target.is_file() or target.suffix.lower() != ".jar":
                        raise ValueError(f"The selected mod file is missing: {relative}")
                    item = pending[relative]
                    project_id = str(item.get("modrinth_project_id") or "")
                    metadata = inspect_mod_jar(
                        target, loader_key, minecraft,
                        modrinth_project_ids=[project_id] if project_id else (),
                    )
                    if metadata.get("protected") or item.get("protected"):
                        name = metadata.get("protected_name") or item.get("protected_name") or "this protected library"
                        raise ValueError(f"Blockyard will not remove {name}; it is protected as a required library.")
                    targets.append((relative, target, item, metadata))

                removed, failed = [], []
                for relative, target, item, metadata in targets:
                    try:
                        record_client_only_mod(target, metadata["mod_ids"], item.get("reason") or metadata["reason"], "user-confirmed")
                        backup_path = quarantine_client_mod(root, target)
                        item.update({"installed": False, "quarantined": True, "backup_path": backup_path})
                        removed.append(relative)
                        log(sid, f"Moved confirmed client-only mod {Path(relative).name} to {backup_path}.", "warning")
                    except (OSError, ValueError) as exc:
                        failed.append({"path": relative, "error": str(exc)})
                report["counts"] = {
                    "total": len(mods),
                    "server_compatible": sum(item.get("status") == "Server compatible" for item in mods),
                    "client_only": sum(item.get("status") == "Client only" for item in mods),
                    "removed_client_only": sum(bool(item.get("quarantined")) for item in mods),
                    "unverified": sum(item.get("status") == "Unverified" for item in mods),
                    "alternatives_found": sum(bool(item.get("alternative")) for item in mods),
                }
                report["checked_at"] = utc_now()
                server["mod_compatibility"] = report
                persist_servers()
                return self.send_json({"ok": bool(removed), "removed": removed, "failed": failed, "server": public_server(sid)})
            match = re.fullmatch(r"/api/servers/([\w-]+)/mods/restore", path)
            if match:
                sid = match.group(1)
                if sid not in servers:
                    return self.send_json({"error": "Server not found."}, 404)
                server = servers[sid]
                if server.get("setup_status") != "ready":
                    raise ValueError("Wait for server preparation to finish before restoring mods.")
                with state_lock:
                    process = runtime.setdefault(sid, {"logs": [], "status": "offline", "process": None, "log_seq": 0}).get("process")
                    if process and process.poll() is None:
                        raise ValueError("Stop the server before restoring mods.")
                paths = body.get("paths")
                if not isinstance(paths, list) or not paths or len(paths) > 500 or any(not isinstance(value, str) for value in paths):
                    raise ValueError("Choose one or more moved-aside mods to restore.")
                report = server.get("mod_compatibility") or {}
                mods = report.get("mods", [])
                recoverable = {str(item.get("path", "")): item for item in mods
                               if item.get("status") == "Client only" and item.get("quarantined") and not item.get("installed")}
                requested = list(dict.fromkeys(paths))
                if any(value not in recoverable for value in requested):
                    raise ValueError("One or more selected mods are not available to restore. Refresh the page and review the current list.")
                root, _ = server_file_path(sid)
                targets = []
                for relative in requested:
                    item = recoverable[relative]
                    parts = PurePosixPath(relative.replace("\\", "/"))
                    backup_parts = PurePosixPath(str(item.get("backup_path") or "").replace("\\", "/"))
                    if (parts.is_absolute() or any(part in ("", ".", "..") for part in parts.parts)
                            or parts.suffix.lower() != ".jar" or not parts.parts or parts.parts[0].lower() != "mods"):
                        raise ValueError("A selected mod path is invalid.")
                    if (backup_parts.is_absolute() or any(part in ("", ".", "..") for part in backup_parts.parts)
                            or backup_parts.suffix.lower() != ".jar" or not backup_parts.parts
                            or backup_parts.parts[0].casefold() != "blockyard-client-only-mods"):
                        raise ValueError("A selected recovery path is invalid.")
                    for checked_parts in (parts.parts, backup_parts.parts):
                        cursor = root
                        for part in checked_parts:
                            cursor = cursor / part
                            if cursor.is_symlink():
                                raise ValueError("Blockyard will not restore mod JARs through symbolic links.")
                    _, target = server_file_path(sid, relative)
                    _, backup = server_file_path(sid, backup_parts.as_posix())
                    try:
                        target.relative_to((root / "mods").resolve())
                        backup.relative_to((root / "blockyard-client-only-mods").resolve())
                    except ValueError as exc:
                        raise ValueError("A selected mod or recovery file is outside its expected folder.") from exc
                    if target.exists():
                        raise ValueError(f"A file already exists at {relative}; Blockyard will not overwrite it.")
                    if not backup.is_file() or backup.suffix.lower() != ".jar":
                        raise ValueError(f"The recoverable mod file is missing: {backup_parts.as_posix()}")
                    targets.append((relative, target, backup, item))

                restored, failed = [], []
                for relative, target, backup, item in targets:
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(backup), str(target))
                        item.update({"installed": True, "quarantined": False, "backup_path": ""})
                        restored.append(relative)
                        log(sid, f"Restored client-only mod {Path(relative).name} from the recovery folder.", "warning")
                    except (OSError, ValueError) as exc:
                        failed.append({"path": relative, "error": str(exc)})
                report["counts"] = {
                    "total": len(mods),
                    "server_compatible": sum(item.get("status") == "Server compatible" for item in mods),
                    "client_only": sum(item.get("status") == "Client only" for item in mods),
                    "removed_client_only": sum(bool(item.get("quarantined")) for item in mods),
                    "unverified": sum(item.get("status") == "Unverified" for item in mods),
                    "alternatives_found": sum(bool(item.get("alternative")) for item in mods),
                }
                report["checked_at"] = utc_now()
                server["mod_compatibility"] = report
                persist_servers()
                return self.send_json({"ok": bool(restored), "restored": restored, "failed": failed, "server": public_server(sid)})
            if path == "/api/config":
                root = str(body.get("default_root", config.get("default_root", ""))).strip()
                if root:
                    config["default_root"] = str(Path(os.path.expandvars(os.path.expanduser(root))).resolve())
                theme_color = str(body.get("theme_color", config.get("theme_color", "#54a9ff"))).strip()
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", theme_color):
                    raise ValueError("Choose a valid six-digit theme color.")
                config["theme_color"] = theme_color.lower()
                if body.get("clear_curseforge_api_key"):
                    config.pop("curseforge_api_key", None)
                submitted_api_key = str(body.get("curseforge_api_key") or "").strip()
                if submitted_api_key:
                    if len(submitted_api_key) > 512 or any(char in submitted_api_key for char in "\r\n"):
                        raise ValueError("The CurseForge API key is invalid or too long.")
                    config["curseforge_api_key"] = submitted_api_key
                persist_config()
                return self.send_json({"ok": True, **public_config()})
            if path == "/api/curseforge/install":
                if not str(config.get("curseforge_api_key") or "").strip():
                    raise ValueError("Add your CurseForge API key in Settings before installing a pack.")
                try:
                    mod_id = int(body.get("mod_id"))
                    file_id = int(body.get("file_id"))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Choose a CurseForge modpack and release first.") from exc
                if mod_id <= 0 or file_id <= 0:
                    raise ValueError("Choose a valid CurseForge modpack and release first.")
                name, target = validate_root(body.get("name"), body.get("root", ""))
                with state_lock:
                    target_key = os.path.normcase(str(target))
                    if any(os.path.normcase(str(Path(item.get("path", "")).resolve())) == target_key
                           for item in servers.values()):
                        raise ValueError("A server already uses that install location. Choose a different name or folder.")
                    if target.exists():
                        raise ValueError(f"The install folder already exists: {target}. Choose a different server name or install location.")
                    target.mkdir(parents=True, exist_ok=False)
                    remember_install_root(target)
                    pack_name = str(body.get("pack_name") or "CurseForge Modpack")[:200]
                    pack_icon = str(body.get("pack_icon") or "")[:2000]
                    sid = str(uuid.uuid4())[:8]
                    item = {
                        "id": sid, "name": name, "path": str(target), "source": "local",
                        "modpack": {"name": pack_name, "icon": pack_icon, "source": "curseforge", "project_id": str(mod_id)},
                        "curseforge_version_id": str(file_id),
                        "minecraft_version": "Detecting…", "pack_version": "Resolving…", "loader": "Resolving…",
                        "setup_status": "installing", "setup_error": "", "created_at": utc_now(), "updated_at": utc_now(),
                        "settings": {"port": 25565, "motd": name, "max_players": 20, "difficulty": "normal", "gamemode": "survival", "view_distance": 10,
                                     "simulation_distance": 10, "online_mode": True, "whitelist": False, "pvp": True, "allow_flight": False,
                                     "ram_mb": 4096, "java_path": "java", "eula_accepted": False},
                    }
                    servers[sid] = item
                    runtime[sid] = {"logs": [], "status": "installing", "process": None, "log_seq": 0}
                persist_servers()
                threading.Thread(target=install_curseforge_server_pack, args=(sid, mod_id, file_id), daemon=True).start()
                return self.send_json({"server": public_server(sid)}, 201)
            if path == "/api/servers/detect":
                operation = str(body.get("operation") or "scan").lower()
                if operation == "scan":
                    result = detect_local_server_folders()
                    public_candidates = [{key: candidate[key] for key in (
                        "name", "path", "minecraft_version", "loader", "launch_file", "mod_count", "eula_accepted",
                    )} for candidate in result["servers"]]
                    return self.send_json({**result, "servers": public_candidates})
                if operation != "add":
                    raise ValueError("Choose whether to scan or add a detected server folder.")
                submitted_path = str(body.get("path") or "").strip()
                try:
                    requested_key = os.path.normcase(str(Path(submitted_path).resolve(strict=True)))
                except (OSError, RuntimeError):
                    requested_key = ""
                result = detect_local_server_folders()
                candidate = next((item for item in result["servers"]
                                  if os.path.normcase(item["path"]) == requested_key), None)
                if not candidate:
                    raise ValueError("That folder is no longer an unregistered server in the default server location. Scan again and choose a detected folder.")

                props = _read_server_properties(Path(candidate["path"]))

                def property_int(key, default, minimum, maximum):
                    try:
                        return max(minimum, min(maximum, int(props.get(key, default))))
                    except (TypeError, ValueError):
                        return default

                def property_bool(key, default):
                    value = props.get(key)
                    return default if value is None else value.strip().lower() in ("true", "1", "yes")

                with state_lock:
                    path_key = os.path.normcase(candidate["path"])
                    if any(os.path.normcase(str(Path(item.get("path", "")).resolve())) == path_key for item in servers.values()):
                        raise ValueError("This server folder has already been added to Blockyard.")
                    base_name = re.sub(r"\s+", " ", candidate["name"]).strip() or "Existing Minecraft server"
                    base_name = base_name[:70]
                    name = base_name
                    suffix = 2
                    used_names = {str(item.get("name", "")).casefold() for item in servers.values()}
                    while name.casefold() in used_names:
                        ending = f" ({suffix})"
                        name = base_name[:70 - len(ending)] + ending
                        suffix += 1
                    sid = str(uuid.uuid4())[:8]
                    item = {
                        "id": sid, "name": name, "path": candidate["path"], "source": "local",
                        "modpack": {"name": candidate["name"], "source": "local"},
                        "minecraft_version": candidate["minecraft_version"], "pack_version": "Existing installation",
                        "loader": candidate["loader"], "setup_status": "ready", "setup_error": "",
                        "created_at": utc_now(), "updated_at": utc_now(),
                        "settings": {
                            "port": property_int("server-port", 25565, 1, 65535),
                            "motd": props.get("motd", name),
                            "max_players": property_int("max-players", 20, 1, 100000),
                            "difficulty": props.get("difficulty", "normal"),
                            "gamemode": props.get("gamemode", "survival"),
                            "view_distance": property_int("view-distance", 10, 2, 32),
                            "simulation_distance": property_int("simulation-distance", 10, 2, 32),
                            "online_mode": property_bool("online-mode", True),
                            "whitelist": property_bool("white-list", False),
                            "pvp": property_bool("pvp", True),
                            "allow_flight": property_bool("allow-flight", False),
                            "ram_mb": 4096, "java_path": "java", "eula_accepted": candidate["eula_accepted"],
                        },
                    }
                    servers[sid] = item
                    runtime[sid] = {"logs": [], "status": "offline", "process": None, "log_seq": 0}
                persist_servers()
                return self.send_json({"ok": True, "server": public_server(sid)}, 201)
            if path == "/api/servers":
                name, target = validate_root(body.get("name"), body.get("root", ""))
                source = str(body.get("source") or "modrinth").lower()
                if source != "modrinth":
                    raise ValueError("Choose a Modrinth modpack. Local server packs can be imported as ZIP files.")
                project_id, file_id = str(body["project_id"]).strip(), str(body["file_id"]).strip()
                if not project_id or not file_id:
                    raise ValueError("Choose a modpack and release before installing.")
                remember_install_root(target)
                pack_name = str(body.get("pack_name") or "Modrinth Modpack")
                pack_icon = str(body.get("pack_icon") or "")
                sid = str(uuid.uuid4())[:8]
                item = {
                    "id": sid, "name": name, "path": str(target), "source": source, "modpack": {"project_id": project_id, "name": pack_name, "icon": pack_icon, "source": source},
                    "modrinth_version_id": file_id,
                    "minecraft_version": "Detecting…", "pack_version": "Resolving…", "loader": "Resolving…",
                    "setup_status": "installing", "setup_error": "", "created_at": utc_now(), "updated_at": utc_now(),
                    "settings": {"port": 25565, "motd": name, "max_players": 20, "difficulty": "normal", "gamemode": "survival", "view_distance": 10,
                                 "simulation_distance": 10, "online_mode": True, "whitelist": False, "pvp": True, "allow_flight": False,
                                 "ram_mb": 4096, "java_path": "java", "eula_accepted": False},
                }
                with state_lock:
                    servers[sid] = item
                    runtime[sid] = {"logs": [], "status": "installing", "process": None, "log_seq": 0}
                persist_servers()
                threading.Thread(target=install_modrinth_server, args=(sid, file_id, {"project_id": project_id, "name": pack_name, "source": source}), daemon=True).start()
                return self.send_json({"server": public_server(sid)}, 201)

            match = re.fullmatch(r"/api/servers/([\w-]+)/files", path)
            if match:
                sid = match.group(1)
                if sid not in servers:
                    return self.send_json({"error": "Server not found."}, 404)
                operation = str(body.get("operation", ""))
                if operation == "mkdir":
                    relative_folder = str(body.get("path", ""))
                    name = safe_server_filename(body.get("name", ""))
                    _, folder = server_file_path(sid, relative_folder)
                    if not folder.is_dir():
                        raise ValueError("That server folder does not exist.")
                    _, target = server_file_path(sid, (PurePosixPath(relative_folder) / name).as_posix())
                    target.mkdir()
                    log(sid, f"Created folder {name}.")
                elif operation == "write":
                    relative_file = str(body.get("path", ""))
                    content = body.get("content")
                    if not isinstance(content, str):
                        raise ValueError("File contents must be text.")
                    encoded = content.encode("utf-8")
                    if len(encoded) > MAX_TEXT_FILE_BYTES:
                        raise ValueError("Text files are limited to 2 MB.")
                    _, target = server_file_path(sid, relative_file)
                    if not target.parent.is_dir():
                        raise ValueError("That server folder does not exist.")
                    if target.exists() and not target.is_file():
                        raise ValueError("Choose a file, not a folder.")
                    temp = target.with_name(f".{target.name}.blockyard-{uuid.uuid4().hex}.tmp")
                    try:
                        temp.write_bytes(encoded)
                        temp.replace(target)
                    finally:
                        temp.unlink(missing_ok=True)
                    log(sid, f"Saved file {target.name}.")
                elif operation == "delete":
                    _, target = server_file_path(sid, str(body.get("path", "")))
                    if not target.is_file():
                        raise ValueError("Only files can be deleted from Blockyard.")
                    target.unlink()
                    log(sid, f"Deleted file {target.name}.", "warning")
                else:
                    raise ValueError("Choose a valid file operation.")
                return self.send_json({"ok": True})

            match = re.fullmatch(r"/api/servers/([\w-]+)/(start|stop|kill|restart|input|settings|delete)", path)
            if not match:
                return self.send_json({"error": "Not found."}, 404)
            sid, action = match.group(1), match.group(2)
            if sid not in servers:
                return self.send_json({"error": "Server not found."}, 404)
            if action == "start":
                start_process(sid)
            elif action == "stop":
                stop_process(sid, False)
            elif action == "kill":
                stop_process(sid, True)
            elif action == "restart":
                stop_process(sid, False)
                time.sleep(0.6)
                start_process(sid)
            elif action == "input":
                command = str(body.get("command", "")).strip()
                with state_lock:
                    proc = runtime[sid].get("process")
                    if not proc or proc.poll() is not None:
                        raise ValueError("Start the server before sending a console command.")
                    if command.startswith("/"):
                        command = command[1:]
                    proc.stdin.write(command + "\n")
                    proc.stdin.flush()
                log(sid, "> " + command)
            elif action == "settings":
                values = body.get("settings") or {}
                old = servers[sid].get("settings", {})
                if "port" in values:
                    port = int(values["port"])
                    if not 1 <= port <= 65535:
                        raise ValueError("Port must be between 1 and 65535.")
                    values["port"] = port
                if "ram_mb" in values:
                    ram = int(values["ram_mb"])
                    if not 1024 <= ram <= 131072:
                        raise ValueError("Memory must be between 1,024 and 131,072 MB.")
                    values["ram_mb"] = ram
                for field in ("max_players", "view_distance", "simulation_distance"):
                    if field in values:
                        values[field] = int(values[field])
                if "max_players" in values and not 1 <= values["max_players"] <= 1000:
                    raise ValueError("Maximum players must be between 1 and 1000.")
                if any(field in values and not 2 <= values[field] <= 32 for field in ("view_distance", "simulation_distance")):
                    raise ValueError("View and simulation distance must be between 2 and 32 chunks.")
                for field in ("online_mode", "whitelist", "pvp", "allow_flight", "eula_accepted"):
                    if field in values:
                        values[field] = bool(values[field])
                if "motd" in values:
                    values["motd"] = str(values["motd"])[:200].replace("\n", " ")
                if "difficulty" in values and values["difficulty"] not in ("peaceful", "easy", "normal", "hard"):
                    raise ValueError("Choose a valid difficulty.")
                if "gamemode" in values and values["gamemode"] not in ("survival", "creative", "adventure", "spectator"):
                    raise ValueError("Choose a valid game mode.")
                if "java_path" in values:
                    values["java_path"] = str(values["java_path"]).strip()[:400] or "java"
                with state_lock:
                    old.update(values)
                    servers[sid]["settings"] = old
                apply_properties(servers[sid])
                persist_servers()
                log(sid, "Server settings saved.")
            elif action == "delete":
                if servers[sid].get("setup_status") == "installing":
                    raise ValueError("Wait for server setup to finish before deleting this server.")
                delete_files = bool(body.get("delete_files"))
                if delete_files:
                    target = Path(str(servers[sid].get("path") or "")).resolve()
                    home = Path.home().resolve()
                    protected = [APP_DIR.resolve(), DATA_DIR.resolve()]
                    if not target.is_dir():
                        raise ValueError("The server folder is missing or unavailable, so its files could not be deleted.")
                    if target == Path(target.anchor) or target == home or target in home.parents:
                        raise ValueError("Blockyard will not delete a drive root or your user profile folder.")
                    for guard in protected:
                        if target == guard or target in guard.parents or guard in target.parents:
                            raise ValueError("Blockyard will not delete its own application or settings folder.")
                stop_process(sid, False)
                if delete_files:
                    shutil.rmtree(target)
                with state_lock:
                    servers.pop(sid, None)
                    runtime.pop(sid, None)
                persist_servers()
                return self.send_json({"ok": True, "files_deleted": delete_files})
            return self.send_json({"server": public_server(sid)})
        except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, json.JSONDecodeError, urllib.error.URLError) as exc:
            self.send_json({"error": str(exc)}, 400)


def main():
    try:
        server = BlockyardHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1.5) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if health.get("ok") and health.get("app") == "Blockyard":
                    print("Blockyard is already running. Reusing the existing manager; no duplicate was started.")
                    return 0
            except (OSError, urllib.error.URLError, json.JSONDecodeError, AttributeError):
                pass
            print(f"Port {PORT} is already in use by another program. Close that program before starting Blockyard.")
        else:
            print(f"Blockyard could not start its local web server: {exc}")
        return 1
    try:
        refresh_saved_client_only_classifications()
    except Exception as exc:
        print(f"Could not refresh the saved client-only mod manifest: {exc}")
    server.daemon_threads = True
    print(f"Blockyard Server Manager is ready at http://127.0.0.1:{PORT}")
    print(f"Server data: {DATA_DIR}")
    try:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{PORT}")
    except Exception:
        pass
    threading.Thread(target=refresh_upstream_client_only_list, name="blockyard-client-mod-list", daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down Blockyard…")
    finally:
        with state_lock:
            active = [sid for sid, slot in runtime.items() if slot.get("process") and slot["process"].poll() is None]
        for sid in active:
            stop_process(sid, False)
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
