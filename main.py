"""
Decky Smooth Frames backend.

Installs the plugin's own build of the open-source afmf-linux Vulkan layer
(https://github.com/serialexperimentslainnnn/afmf-linux, MIT) into the user's home, keeps the
settings and writes the small wrapper script that Steam's launch options call. No root needed.

The bundled build adds one thing to afmf-linux 1.3.1: AFMF_MAX_BASE_FPS, a limiter for the game's
real frames, plus a control file to switch generation and the limit while a game runs
(patches/afmf-linux-1.3.1-smooth-frames.patch). It is registered under its own layer name and its
own enable variable, so an upstream afmf-linux installed by other tools never loads alongside it.

Layout:
  ~/.local/lib/decky-smooth-frames/libafmf-linux.so
  ~/.local/share/vulkan/implicit_layer.d/decky-smooth-frames.json   (dormant unless SMOOTH_FRAMES_ENABLE=1)
  DECKY_PLUGIN_SETTINGS_DIR/settings.json                  UI state, single source of truth
  DECKY_PLUGIN_SETTINGS_DIR/env/<appid>.env, default.env   generated from settings.json
  ~/.local/share/decky-smooth-frames/smooth-frames-run     wrapper used in launch options
  DECKY_PLUGIN_RUNTIME_DIR/licenses/                       afmf-linux + FidelityFX licence texts
"""

import asyncio
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import time

import decky

LAYER_VERSION = "1.3.1"
LAYER_NAME = "VK_LAYER_DECKY_SMOOTH_FRAMES"
LAYER_ENABLE_VAR = "SMOOTH_FRAMES_ENABLE"
LAYER_DISABLE_VAR = "SMOOTH_FRAMES_DISABLE"
BUNDLE_DIR = os.path.join(decky.DECKY_PLUGIN_DIR, "bin")  # shipped inside the plugin ZIP

HOME = decky.DECKY_USER_HOME
LIB_DIR = os.path.join(HOME, ".local", "lib", "decky-smooth-frames")
LIB_PATH = os.path.join(LIB_DIR, "libafmf-linux.so")
MANIFEST_DIR = os.path.join(HOME, ".local", "share", "vulkan", "implicit_layer.d")
MANIFEST_PATH = os.path.join(MANIFEST_DIR, "decky-smooth-frames.json")

SETTINGS_DIR = decky.DECKY_PLUGIN_SETTINGS_DIR
RUNTIME_DIR = decky.DECKY_PLUGIN_RUNTIME_DIR
LOG_DIR = decky.DECKY_PLUGIN_LOG_DIR
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
ENV_DIR = os.path.join(SETTINGS_DIR, "env")
# Steam splits launch options at spaces, and Decky may name the plugin's folders after the plugin
# ("Decky Smooth Frames"): the wrapper therefore lives at a fixed path without spaces.
WRAPPER_PATH = os.path.join(HOME, ".local", "share", "decky-smooth-frames", "smooth-frames-run")

# Settings of the plugin's earlier name (up to 1.0.0), taken over once on first start.
OLD_SETTINGS_PATHS = [
    os.path.join(decky.DECKY_HOME, "settings", name, "settings.json") for name in ("Decky-AFMF", "decky-afmf")
]
OLD_WRAPPER_NAME = "decky-afmf-run"
LICENSE_DIR = os.path.join(RUNTIME_DIR, "licenses")
CONTROL_DIR = os.path.join(HOME, ".local", "share", "decky-smooth-frames", "control")  # <appid>: "<on> <limit>"
STATUS_DIR = os.path.join(LOG_DIR, "games")  # <appid>.session and <appid>.log per launch
STATUS_LOG_MAX = 4 * 1024 * 1024

def _deck_model():
    """'lcd', 'oled' or None, from the DMI product name (Jupiter / Galileo)."""
    try:
        with open("/sys/devices/virtual/dmi/id/board_vendor", "r") as fh:
            vendor = fh.read().strip()
        with open("/sys/devices/virtual/dmi/id/product_name", "r") as fh:
            product = fh.read().strip()
    except OSError:
        return None
    if vendor != "Valve":
        return None
    return {"Jupiter": "lcd", "Galileo": "oled"}.get(product)


DECK_MODEL = _deck_model()
IS_INTEGRATED_GPU = DECK_MODEL is not None

# afmf-linux's three options (named after AFMF 2.1 in AMD Software), with AMD's defaults, plus the base
# frame rate limit (the Radeon Chill counterpart). 0 = off.
DEFAULT_PROFILE = {
    "search_mode": "auto",        # Auto / Standard / High
    "performance_mode": "auto",   # Auto / Quality / Performance
    "fast_motion": "blend",       # Fast Motion Response: Repeat Frame / Blended Frame
    # half the built-in refresh rate: 60 Hz LCD -> 30, 90 Hz OLED -> 45
    "limit": {"lcd": 30, "oled": 45}.get(DECK_MODEL or "", 0),
}

CHOICES = {
    "performance_mode": ("auto", "performance", "quality"),
    "search_mode": ("auto", "standard", "high"),
    "fast_motion": ("blend", "repeat"),
}
INTS = {"limit": (0, 240)}


# ---------------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------------

def _atomic_write(path: str, data: bytes, mode: int = 0o644):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)  # new inode: a game that has the old .so mapped keeps running
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def sanitize_profile(raw) -> dict:
    """Whitelist every key and value: these end up in a file the wrapper exports into games."""
    out = copy.deepcopy(DEFAULT_PROFILE)
    if not isinstance(raw, dict):
        return out
    if "limit" not in raw and "limit_internal" in raw:  # settings from 0.3.0
        raw = {**raw, "limit": raw["limit_internal"]}
    for key, allowed in CHOICES.items():
        if raw.get(key) in allowed:
            out[key] = raw[key]
    for key, (lo, hi) in INTS.items():
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            val = int(val)
            out[key] = 0 if val <= 0 else max(max(lo, 10), min(hi, val))
    return out


def valid_appid(appid) -> str:
    s = str(appid).strip()
    if not s.isdigit() or len(s) > 20:
        raise ValueError(f"invalid app id: {appid!r}")
    return s


def profile_to_env(profile: dict, appid: str, enabled: bool = True) -> str:
    """Render one profile as KEY=value lines (no quoting needed: values are whitelisted)."""
    lines = ["# Generated by Decky Smooth Frames. Edits are overwritten; use the plugin instead."]
    if not enabled:
        lines.append(f"{LAYER_DISABLE_VAR}=1")
        return "\n".join(lines) + "\n"
    p = sanitize_profile(profile)
    perf = p["performance_mode"]
    if perf == "auto" and IS_INTEGRATED_GPU:
        # AMD's "Auto" means Performance on integrated graphics (AFMF 2 release notes);
        # afmf-linux's own auto only looks at the resolution.
        perf = "performance"
    lines += [
        f"{LAYER_ENABLE_VAR}=1",
        # never stack two interpolators in one game: an upstream afmf-linux, the layer of this
        # plugin's earlier version (Decky-AFMF) and lsfg-vk stay out
        "DISABLE_AFMF=1",
        "DECKY_AFMF_DISABLE=1",
        "DISABLE_LSFGVK=1",
        f"AFMF_SEARCH_MODE={p['search_mode']}",
        f"AFMF_PERFORMANCE_MODE={perf}",
        f"AFMF_FAST_MOTION_RESPONSE={p['fast_motion']}",
        # info log + statistics every 300 presents: what the live status reads
        "AFMF_LOG=2",
        "AFMF_PROFILE=1",
        "SMOOTH_FRAMES_STATUS=1",
        f"SMOOTH_FRAMES_LIMIT={p['limit']}",
        # re-read by the layer while the game runs: the toggle and the limit apply at once
        f"AFMF_CONTROL_FILE={os.path.join(CONTROL_DIR, appid)}",
    ]
    return "\n".join(lines) + "\n"


WRAPPER_TEMPLATE = r"""#!/bin/sh
# smooth-frames-run - generated by the Decky Smooth Frames plugin.
# Steam launch option:  __WRAPPER__ <appid> %command%
# Reads the game's settings, exports only the layer's variables, then runs the game unchanged.
ENV_DIR='__ENV_DIR__'
STATUS_DIR='__STATUS_DIR__'

appid="$1"
case "$appid" in
  ''|*[!0-9]*) appid=default ;;   # called without an app id: keep every argument
  *) shift ;;
esac

if [ "$#" -eq 0 ]; then
  echo "smooth-frames-run: no command given (the launch option must end in %command%)" >&2
  exit 1
fi

f="$ENV_DIR/$appid.env"
[ -f "$f" ] || f="$ENV_DIR/default.env"

status=0
limit=0
if [ -f "$f" ]; then
  while IFS='=' read -r k v || [ -n "$k" ]; do
    case "$k" in
      AFMF_*|SMOOTH_FRAMES_ENABLE|SMOOTH_FRAMES_DISABLE|DECKY_AFMF_DISABLE|DISABLE_AFMF|DISABLE_LSFGVK) export "$k=$v" ;;
      SMOOTH_FRAMES_STATUS) status="$v" ;;
      SMOOTH_FRAMES_LIMIT) limit="$v" ;;
    esac
  done < "$f"
fi

on=0
[ "${SMOOTH_FRAMES_ENABLE:-0}" = 1 ] && [ "${SMOOTH_FRAMES_DISABLE:-0}" != 1 ] && on=1

case "$limit" in ''|*[!0-9]*) limit=0 ;; esac
[ "$on" = 1 ] || limit=0
if [ "$on" = 1 ]; then
  # the layer keeps its minimum-fps floor 15 % under this limit, also when it changes live
  [ "$limit" -gt 0 ] && export AFMF_MAX_BASE_FPS="$limit"
fi

# Gamescope (Steam Deck Game Mode) keeps more images in flight: give the layer enough spares.
gamescope=0
if [ "$on" = 1 ] && [ -z "${AFMF_GAMESCOPE:-}" ]; then
  if [ -n "${GAMESCOPE_WAYLAND_DISPLAY:-}" ] || [ "${XDG_CURRENT_DESKTOP:-}" = gamescope ]; then
    export AFMF_GAMESCOPE=1
    gamescope=1
  fi
fi

# Session marker for the plugin's live status. exec keeps this PID for the game (or Proton).
export SMOOTH_FRAMES_APPID="$appid"
if mkdir -p "$STATUS_DIR" 2>/dev/null; then
  {
    echo "pid=$$"
    echo "start=$(date +%s)"
    echo "on=$on"
    echo "status=$status"
    echo "gamescope=$gamescope"
    echo "limit=$limit"
  } > "$STATUS_DIR/$appid.session" 2>/dev/null
  if [ "$on" = 1 ] && [ "$status" = 1 ]; then
    # The layer logs to stderr; a plain file (no pipe) cannot stall or kill the game.
    : > "$STATUS_DIR/$appid.log" 2>/dev/null && exec 2>>"$STATUS_DIR/$appid.log"
  fi
fi

exec "$@"
"""


# ---------------------------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------------------------

class Store:
    """Settings live with each enabled game; a new game starts from the device defaults."""

    def __init__(self):
        self.data = {"games": {}}
        self.migrated_from_old_name = False

    def load(self):
        path = SETTINGS_PATH
        if not os.path.isfile(path):
            for old in OLD_SETTINGS_PATHS:
                if os.path.isfile(old):
                    path = old
                    self.migrated_from_old_name = True
                    decky.logger.info(f"taking over settings from {old}")
                    break
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return
        except Exception as err:  # noqa: BLE001
            decky.logger.error(f"settings.json unreadable, using defaults: {err}")
            return
        # up to 0.3.1 games without their own settings followed a global profile: freeze it into them
        old_default = raw.get("default")
        games = {}
        for appid, g in (raw.get("games") or {}).items():
            if not str(appid).isdigit() or not isinstance(g, dict):
                continue
            own = g.get("custom", True) if "custom" in g else True
            profile = g.get("profile") if own or old_default is None else old_default
            changed = g.get("changed")
            games[str(appid)] = {
                "name": str(g.get("name") or "")[:200],
                "enabled": bool(g.get("enabled", True)),
                "profile": sanitize_profile(profile),
                "changed": float(changed) if isinstance(changed, (int, float)) else 0.0,
            }
        self.data["games"] = games
        # launch options still pointing at the old wrapper are rewritten by the frontend
        self.data["launch_options_migrated"] = bool(raw.get("launch_options_migrated", False)) and not self.migrated_from_old_name

    def save(self):
        _atomic_write(SETTINGS_PATH, json.dumps(self.data, indent=2, ensure_ascii=False).encode("utf-8"))
        self.write_env_files()

    def effective_profile(self, appid: str) -> dict:
        g = self.data["games"].get(appid)
        return g["profile"] if g else copy.deepcopy(DEFAULT_PROFILE)

    def write_env_files(self):
        os.makedirs(ENV_DIR, exist_ok=True)
        # The wrapper falls back to default.env for an unknown app id: off.
        off = profile_to_env({}, "0", enabled=False)
        wanted = {"default.env": off}
        for appid, g in self.data["games"].items():
            wanted[f"{appid}.env"] = profile_to_env(g["profile"], appid, g["enabled"])
        for name, content in wanted.items():
            _atomic_write(os.path.join(ENV_DIR, name), content.encode("utf-8"))
        os.makedirs(CONTROL_DIR, exist_ok=True)
        for appid, g in self.data["games"].items():
            control = f"{1 if g['enabled'] else 0} {g['profile']['limit']}\n"
            path = os.path.join(CONTROL_DIR, appid)
            try:
                with open(path, "r") as fh:
                    if fh.read() == control:
                        continue
            except OSError:
                pass
            _atomic_write(path, control.encode())
        # Games removed from the plugin: keep a file that turns the layer off in case Steam still
        # has the old launch option somewhere.
        for name in os.listdir(ENV_DIR):
            if name.endswith(".env") and name not in wanted:
                _atomic_write(os.path.join(ENV_DIR, name), off.encode())


def write_wrapper():
    script = (
        WRAPPER_TEMPLATE.replace("__ENV_DIR__", ENV_DIR)
        .replace("__STATUS_DIR__", STATUS_DIR)
        .replace("__WRAPPER__", WRAPPER_PATH)
    )
    _atomic_write(WRAPPER_PATH, script.encode("utf-8"), mode=0o755)


# ---------------------------------------------------------------------------------------------
# layer install
# ---------------------------------------------------------------------------------------------

def _sha256_file(path: str):
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def install_bundled_layer() -> dict:
    """Copy the layer shipped in the plugin to ~/.local and register it (only when it changed)."""
    src = os.path.join(BUNDLE_DIR, "libafmf-linux.so")
    if not os.path.isfile(src):
        raise RuntimeError(f"bundled layer missing: {src}")
    src_hash = _sha256_file(src)
    if _sha256_file(LIB_PATH) != src_hash:
        with open(src, "rb") as fh:
            blob = fh.read()
        if blob[:4] != b"\x7fELF":
            raise RuntimeError("bundled libafmf-linux.so is not an ELF library")
        _atomic_write(LIB_PATH, blob, mode=0o644)  # new inode: running games keep the old one
        decky.logger.info(f"layer installed to {LIB_PATH}")
    manifest = {
        "file_format_version": "1.0.0",
        "layer": {
            "name": LAYER_NAME,
            "type": "GLOBAL",
            "library_path": LIB_PATH,
            "api_version": "1.4.341",
            "implementation_version": "5",
            "description": f"afmf-linux {LAYER_VERSION} with base frame rate limiter (Decky Smooth Frames)",
            "functions": {"vkNegotiateLoaderLayerInterfaceVersion": "vkNegotiateLoaderLayerInterfaceVersion"},
            # dormant in every process that did not ask for it
            "enable_environment": {LAYER_ENABLE_VAR: "1"},
            "disable_environment": {LAYER_DISABLE_VAR: "1"},
        },
    }
    data = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    try:
        with open(MANIFEST_PATH, "rb") as fh:
            same = fh.read() == data
    except OSError:
        same = False
    if not same:
        _atomic_write(MANIFEST_PATH, data)
    os.makedirs(LICENSE_DIR, exist_ok=True)
    for name in ("afmf-linux.LICENSE", "LICENSE.fidelityfx.txt", "NOTICE.fidelityfx.md"):
        path = os.path.join(BUNDLE_DIR, name)
        if os.path.isfile(path):
            shutil.copyfile(path, os.path.join(LICENSE_DIR, name))
    return {"version": LAYER_VERSION, "sha256": src_hash}


def remove_layer():
    for path in (MANIFEST_PATH, LIB_PATH):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    try:
        os.rmdir(LIB_DIR)
    except OSError:
        pass


def layer_status() -> dict:
    installed = os.path.isfile(MANIFEST_PATH) and os.path.isfile(LIB_PATH)
    return {
        "installed": installed,
        "version": LAYER_VERSION if installed else None,
        "wrapper": WRAPPER_PATH,
        "deck_model": DECK_MODEL,
    }


# ---------------------------------------------------------------------------------------------
# live status: is the layer really generating frames in the running game?
# ---------------------------------------------------------------------------------------------

STATS_RE = re.compile(
    r"\[AFMF info\] swapchain (\S+): (\d+) presents, (\d+) generated, (\d+) no free image; "
    r"([\d.]+) ms between presents \((\d+) real fps\).*?governor step (\d+)"
)
CREATED_RE = re.compile(r"\[AFMF info\] swapchain (\S+) created: (\d+)x(\d+).*?generation (on|off)")
PASS_RE = re.compile(r"\[AFMF info\] swapchain (\S+): pass-through \((.*)\)")
LAYER_MARK = "[AFMF info] layer active on instance"
READY_MARK = "[AFMF info] interpolation ready"
# The layer then shows the previous frame again instead of an interpolated one: more frames,
# no smoother motion. Must not be reported as working.
REPEAT_MARK = "repeating frames"
PROBLEM_RE = re.compile(r"\[AFMF (error|warn)\] (.*)")
TAIL_BYTES = 256 * 1024
LAYER_MIN_FPS = 30  # afmf-linux default for AFMF_MIN_FPS; the layer keeps it 15 % under the base limit


def read_control(appid: str):
    """(on, limit) as the layer reads it now, or None."""
    try:
        with open(os.path.join(CONTROL_DIR, appid), "r") as fh:
            parts = fh.read().split()
        return parts[0] == "1", int(parts[1])
    except (OSError, IndexError, ValueError):
        return None


def floor_for(limit: int) -> int:
    return limit * 85 // 100 if limit > 0 and LAYER_MIN_FPS * 100 > limit * 85 else LAYER_MIN_FPS


def _read_session(appid: str):
    path = os.path.join(STATUS_DIR, appid + ".session")
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            data = dict(line.strip().split("=", 1) for line in fh if "=" in line)
    except OSError:
        return None
    try:
        pid = int(data.get("pid", "0"))
        start = int(data.get("start", "0"))
    except ValueError:
        return None
    return {
        "pid": pid,
        "start": start,
        "on": data.get("on") == "1",
        "status": data.get("status") == "1",
        "limit": int(data["limit"]) if data.get("limit", "").isdigit() else 0,
    }


def _pid_is_session(pid: int, appid: str) -> bool:
    """True while the process the wrapper exec'd into is alive (guards against PID reuse)."""
    if pid <= 1:
        return False
    try:
        with open(f"/proc/{pid}/environ", "rb") as fh:
            env = fh.read()
    except OSError:
        return False
    return f"SMOOTH_FRAMES_APPID={appid}".encode() in env.split(b"\0")


def _tail(path: str) -> str:
    try:
        size = os.path.getsize(path)
        if size > STATUS_LOG_MAX:
            # The wrapper opened it with O_APPEND, so the game keeps writing at the new end.
            os.truncate(path, 0)
            return ""
        with open(path, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


class LiveTracker:
    """Remembers what scrolled out of a truncated log and when the last statistics arrived."""

    def __init__(self):
        self.cache = {}

    def status(self, appid: str) -> dict:
        now = time.time()
        sess = _read_session(appid)
        if not sess or not _pid_is_session(sess["pid"], appid):
            return {"state": "no_session"}
        age = max(0, int(now - sess["start"]))
        ctrl = read_control(appid)
        limit = ctrl[1] if ctrl else sess["limit"]
        base = {"age": age, "limit": limit, "start": sess["start"]}
        min_fps = floor_for(limit)
        if not sess["on"]:
            return {**base, "state": "off"}
        if ctrl and not ctrl[0]:
            # layer loaded, generation switched off from the plugin while the game runs
            return {**base, "state": "live_off"}
        if not sess["status"]:
            return {**base, "state": "no_live"}

        c = self.cache.get(appid)
        if not c or c["start"] != sess["start"]:
            c = {"start": sess["start"], "layer": False, "swapchain": None, "passthrough": None,
                 "last_stats": None, "prev_stats": None, "stats_seen": 0.0, "problem": None,
                 "pending_repeat": False}
            self.cache[appid] = c

        text = _tail(os.path.join(STATUS_DIR, appid + ".log"))
        stats = []
        for line in text.splitlines():
            if LAYER_MARK in line:
                c["layer"] = True
            if READY_MARK in line:
                c["pending_repeat"] = False
            m = CREATED_RE.search(line)
            if m:
                c["layer"] = True
                c["swapchain"] = {"handle": m.group(1), "w": int(m.group(2)), "h": int(m.group(3)),
                                  "gen": m.group(4) == "on", "repeat": c["pending_repeat"]}
                c["pending_repeat"] = False
                if m.group(4) == "on":
                    c["passthrough"] = None
                continue
            m = PASS_RE.search(line)
            if m:
                c["layer"] = True
                c["passthrough"] = m.group(2)
                continue
            m = STATS_RE.search(line)
            if m:
                c["layer"] = True
                stats.append({
                    "handle": m.group(1), "presents": int(m.group(2)), "generated": int(m.group(3)),
                    "no_image": int(m.group(4)), "real_fps": int(m.group(6)), "governor": int(m.group(7)),
                })
                continue
            m = PROBLEM_RE.search(line)
            if m:
                c["layer"] = True
                c["problem"] = m.group(2)[:200]
                if REPEAT_MARK in line:
                    c["pending_repeat"] = True

        if stats:
            last = stats[-1]
            if last != c["last_stats"]:
                same = [s for s in stats[:-1] if s["handle"] == last["handle"]]
                c["prev_stats"] = same[-1] if same else (
                    c["last_stats"] if c["last_stats"] and c["last_stats"]["handle"] == last["handle"] else None)
                c["last_stats"] = last
                c["stats_seen"] = now

        out = {**base, "layer": c["layer"], "problem": c["problem"]}
        if c["swapchain"]:
            out["resolution"] = f"{c['swapchain']['w']}x{c['swapchain']['h']}"
        last = c["last_stats"]
        if last is None:
            if c["passthrough"] or (c["swapchain"] and not c["swapchain"]["gen"]):
                return {**out, "state": "passthrough", "reason": c["passthrough"]}
            if c["layer"]:
                return {**out, "state": "loaded"}
            return {**out, "state": "starting" if age < 25 else "not_loaded"}

        prev = c["prev_stats"]
        if prev and last["presents"] > prev["presents"]:
            d_pres = last["presents"] - prev["presents"]
            d_gen = max(0, last["generated"] - prev["generated"])
            d_noimg = max(0, last["no_image"] - prev["no_image"])
        else:
            d_pres, d_gen, d_noimg = last["presents"], last["generated"], last["no_image"]
        ratio = d_gen / d_pres if d_pres else 0.0
        real = last["real_fps"]
        out.update({
            "real_fps": real,
            "output_fps": round(real * (1.0 + ratio)),
            "generated_pct": round(100 * ratio),
            "governor_step": last["governor"],
            "no_image_pct": round(100 * d_noimg / d_pres) if d_pres else 0,
        })
        # 300 presents per statistics line: at 30 fps that is 10 s
        stale_after = max(8.0, 2.5 * 300.0 / max(real, 1))
        if now - c["stats_seen"] > stale_after:
            return {**out, "state": "stale"}
        if c["swapchain"] and c["swapchain"].get("repeat") and c["swapchain"]["handle"] == last["handle"]:
            return {**out, "state": "repeating"}
        if ratio >= 0.9:
            return {**out, "state": "active"}
        if ratio >= 0.05:
            return {**out, "state": "partial"}
        if min_fps > 0 and real < min_fps * 1.1:
            return {**out, "state": "below_min", "min_fps": min_fps}
        return {**out, "state": "idle"}


# ---------------------------------------------------------------------------------------------
# plugin API (called from the frontend)
# ---------------------------------------------------------------------------------------------

class Plugin:
    store: Store

    async def _main(self):
        self.store = Store()
        self.store.load()
        self._lock = asyncio.Lock()
        self.live = LiveTracker()
        self.layer_error = None
        try:
            install_bundled_layer()
        except Exception as err:  # noqa: BLE001
            self.layer_error = str(err)
            decky.logger.error(f"layer install failed: {err}")
        try:
            write_wrapper()
            self.store.save()
        except Exception as err:  # noqa: BLE001
            decky.logger.error(f"startup write failed: {err}")
        decky.logger.info("Decky Smooth Frames ready")

    async def _unload(self):
        pass

    async def _uninstall(self):
        # Decky also calls this when a plugin is updated (the old version is uninstalled before
        # the new one starts), so the settings must stay as they are. Only the layer goes: the
        # new version installs it again on start. Without it, games that still have the launch
        # option start normally, since the wrapper and its env files stay in place.
        try:
            remove_layer()
        except Exception as err:  # noqa: BLE001
            decky.logger.error(f"uninstall cleanup failed: {err}")

    async def _run(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # --- layer ---------------------------------------------------------------------------------
    async def get_status(self) -> dict:
        st = await self._run(layer_status)
        st["error"] = self.layer_error
        return st

    async def get_launch_migration(self) -> dict:
        """Games whose launch options may still call the wrapper of the plugin's earlier name."""
        if self.store.data.get("launch_options_migrated"):
            return {"needed": False, "old_wrapper_name": OLD_WRAPPER_NAME, "appids": []}
        return {"needed": True, "old_wrapper_name": OLD_WRAPPER_NAME, "appids": list(self.store.data["games"])}

    async def set_launch_migration_done(self) -> bool:
        self.store.data["launch_options_migrated"] = True
        await self._run(self.store.save)
        return True

    async def repair_layer(self) -> dict:
        async with self._lock:
            try:
                await self._run(install_bundled_layer)
                self.layer_error = None
                return {"ok": True}
            except Exception as err:  # noqa: BLE001
                self.layer_error = str(err)
                return {"ok": False, "error": str(err)}

    # --- profiles ------------------------------------------------------------------------------
    def _game_view(self, appid: str) -> dict:
        g = self.store.data["games"].get(appid)
        return {
            "appid": appid,
            "enabled": bool(g and g["enabled"]),
            "profile": self.store.effective_profile(appid),
        }

    async def get_game(self, appid) -> dict:
        return self._game_view(valid_appid(appid))

    async def set_game_enabled(self, appid, name: str, enabled: bool) -> dict:
        appid = valid_appid(appid)
        g = self.store.data["games"].setdefault(
            appid, {"name": "", "enabled": False, "profile": copy.deepcopy(DEFAULT_PROFILE), "changed": 0.0}
        )
        g["name"] = str(name or g["name"])[:200]
        g["enabled"] = bool(enabled)
        await self._run(self.store.save)
        return self._game_view(appid)

    async def set_game_profile(self, appid, profile: dict) -> dict:
        appid = valid_appid(appid)
        g = self.store.data["games"].get(appid)
        if g is None:
            raise ValueError("game is not enabled")
        new = sanitize_profile(profile)
        if new != g["profile"]:
            # the limit applies live through the control file; the other options at the next start
            if any(new[k] != g["profile"][k] for k in CHOICES):
                g["changed"] = time.time()
            g["profile"] = new
            await self._run(self.store.save)
        return self._game_view(appid)

    async def reset_game_profile(self, appid) -> dict:
        return await self.set_game_profile(appid, copy.deepcopy(DEFAULT_PROFILE))

    async def forget_game(self, appid) -> bool:
        appid = valid_appid(appid)
        self.store.data["games"].pop(appid, None)
        await self._run(self.store.save)
        return True

    async def list_games(self) -> list:
        return [
            {"appid": a, "name": g["name"], "enabled": g["enabled"]}
            for a, g in sorted(self.store.data["games"].items(), key=lambda kv: kv[1]["name"].lower())
        ]

    async def get_live_status(self, appid) -> dict:
        appid = valid_appid(appid)
        try:
            st = await self._run(self.live.status, appid)
            g = self.store.data["games"].get(appid)
            # settings changed after this session started: the running game still has the old ones
            if st.get("state") not in ("no_session", "error") and g and g["changed"] > st.get("start", 0) + 1:  # start has 1 s resolution
                st["restart_needed"] = True
            return st
        except Exception as err:  # noqa: BLE001
            decky.logger.error(f"live status failed: {err}")
            return {"state": "error", "problem": str(err)}

    async def get_wrapper_path(self) -> str:
        return WRAPPER_PATH

    async def get_log_path(self, appid) -> str:
        return os.path.join(STATUS_DIR, valid_appid(appid) + ".log")
