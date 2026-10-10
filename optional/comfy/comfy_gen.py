#!/usr/bin/env python3
"""Asset generation for art agents through the owner's local ComfyUI.

Free by construction: talks only to ComfyUI on this machine (127.0.0.1:8188),
never downloads models, installs nodes or calls a paid/API node. Every command
below `check`/`free`/`stop` starts ComfyUI itself if it is not already up
(headless where possible, else the Comfy Desktop app the owner uses)
and, if it is the one who started it, shuts it down again after the job or
~10 idle minutes. A ComfyUI the owner started by hand is left running; agents
still reach it only through this tool. If it cannot be reached even after
trying to start it, or it stops answering mid-run, the command exits 2 with a
GENERATOR UNAVAILABLE error naming any files it already saved: the run stops there and
the agent reports the outage. It never writes prompts or placeholders in place of outputs.

Commands (all take --out DIR --name STEM; see `<command> --help`):
  check                         ComfyUI up? which approved models are present?
  free                          unload models + free RAM/VRAM (run when your work is done)
  stop                          stop ComfyUI if this tool started it (manual; the idle
                                watcher calls the same code automatically)
  image   --prompt  [--model zimage|krea2] [--width --height --count --seed]
  edit    --image A [--image2 B] --prompt       Qwen-Image-Edit 2511 (4-step)
  rembg   --image A                             BiRefNet background removal -> RGBA PNG
  upscale --image A                             Real-ESRGAN x4
  to3d    --image A [--engine trellis2|pixal3d|hunyuan3d] [--faces N]
  music   --tags "..." [--lyrics "[Instrumental]"] [--seconds 60] [--bpm 120]
  template --template NAME [--set NODE.INPUT=JSON ...] [--load NODE=FILE ...]
                                                run any installed, non-API template

Every command writes ComfyUI's own copies into a folder of its own,
output/agent/<YYYYmmdd-HHMMSS>_<command>_<name>/, and copies each result to
--out, which must be inside the repository the asset is for (the worktree's
asset folder), never a scratch folder. ComfyUI's copies are only a cache; the
owner may clear output/ at any time. A ComfyUI this tool starts puts output/ under
%AGENT_TEMP%\\comfy when AGENT_TEMP is set; a local ComfyUI keeps its folder.

Every saved file gets a sidecar <file>.json (tool, model, licence, inputs, seed,
settings, date, seconds taken) for the repository's ATTRIBUTIONS row.

GPU etiquette: generation shares the GPU with the game engine. Every command
except `check` takes this tool's own GPU self-lock for its own duration,
waiting up to --lock-wait seconds (exit 4 if it never frees), so two
generations never overlap; ordering against the repos' engine, network and
render runs is the machine lock's job, not this file's: every generating
command (with --force too) runs as a local-ai machine-lock run, and with no
machine lock tool it refuses (exit 2). When that run's slot ends, a ComfyUI
this tool started is stopped and a local ComfyUI has its models unloaded if
idle (the hidden `release` command, the run's --on-release). One
command is one slice: keep it under ~10 minutes. Pass --force only if you
already hold this tool's GPU self-lock. Keep images near 1 MP, and never
generate during a measurement window.

Knowledge base (read before; record sessions in the repo's docs/ops/assets.md): ~/.claude/knowledge/asset-generation/
"""
import argparse
import datetime
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import uuid

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_DIR)
import comfy_api as C  # noqa: E402
# winproc.py (Toolhelp + job-object helpers, standard library only) lives beside procwatch.py,
# a sibling tool folder installed next to this one (~/.claude/tools/procwatch/); reused here
# instead of re-implementing pid/ctime verification and process termination.
sys.path.insert(0, os.path.join(TOOL_DIR, "..", "procwatch"))
import winproc as W  # noqa: E402

# Approved local models. Anything not here needs the owner's approval and a licence check.
MODELS = {
    "zimage": {"name": "Z-Image-Turbo (Tongyi-MAI)", "licence": "Apache-2.0",
               "files": ["z_image_turbo_bf16.safetensors", "qwen_3_4b.safetensors", "ae.safetensors"]},
    "krea2": {"name": "Krea 2 Turbo (fp8)", "licence": "Krea 2 Community License (commercial use only while "
              "company revenue < USD 1M/yr; acceptable-use policy applies)",
              "files": ["krea2_turbo_fp8_scaled.safetensors", "qwen3vl_4b_fp8_scaled.safetensors",
                        "qwen_image_vae.safetensors"]},
    "edit": {"name": "Qwen-Image-Edit 2511 (fp8) + Lightning 4-step LoRA", "licence": "Apache-2.0",
             "files": ["qwen_image_edit_2511_fp8mixed.safetensors", "qwen_2.5_vl_7b_fp8_scaled.safetensors",
                       "qwen_image_vae.safetensors",
                       "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors"]},
    "rembg": {"name": "BiRefNet", "licence": "MIT", "files": ["birefnet.safetensors"]},
    "upscale": {"name": "Real-ESRGAN x4plus", "licence": "BSD-3-Clause",
                "files": ["RealESRGAN_x4plus.safetensors"]},
    "trellis2": {"name": "TRELLIS.2 4B (Microsoft)", "licence": "MIT",
                 "files": ["trellis_2_int8_convrot.safetensors", "trellis_2_shape_vae_bf16.safetensors",
                           "trellis_2_texture_vae_bf16.safetensors", "dino_v3_L_naf_fp32.safetensors",
                           "birefnet.safetensors", "moge_2_vitl_normal_fp16.safetensors"]},
    "pixal3d": {"name": "Pixal3D (TencentARC)", "licence": "MIT (bundled components keep their own licences)",
                "files": ["pixal3d_int8_convrot.safetensors", "trellis_2_shape_vae_bf16.safetensors",
                          "trellis_2_texture_vae_bf16.safetensors", "dino_v3_L_naf_fp32.safetensors",
                          "birefnet.safetensors", "moge_2_vitl_normal_fp16.safetensors"]},
    "hunyuan3d": {"name": "Hunyuan3D 2.1 (Tencent)", "licence": "Tencent Hunyuan 3D 2.1 Community License "
                  "(not valid in EU/UK/South Korea; >1M MAU needs a Tencent licence; outputs may not train "
                  "other models)", "files": ["hunyuan_3d_v2.1.safetensors"]},
    "music": {"name": "ACE-Step 1.5 XL Turbo", "licence": "MIT (trained on licensed/royalty-free/synthetic data)",
              "files": ["acestep_v1.5_xl_turbo_bf16.safetensors", "qwen_0.6b_ace15.safetensors",
                        "qwen_4b_ace15.safetensors", "ace_1.5_vae.safetensors"]},
}
# MoGe-2 weights and the krea2_darkbrush LoRA are NOT approved (licence unchecked).
MAX_PIXELS = 2_100_000
# comfy_gen's own self-lock: never two generations at once on this GPU. Held by keeping the
# file open; never delete a lock you did not create. This is not the machine lock (kitlib/lock):
# machine_lock_rerun re-runs each command under it as kind local-ai, which covers ordering
# against the repos' engine and render runs; this file just serialises comfy_gen against itself.
GPU_LOCK = os.environ.get("GPU_LOCK", os.path.join(os.environ.get("LOCALAPPDATA", ""), "Temp",
                                                   "comfy-gen.lock"))


# ---------------------------------------------------------------- helpers
class gpu_lock:
    """Hold the machine-wide GPU lock for one command (one slice: keep it under ~10 minutes)."""

    def __init__(self, args):
        self.args, self.fd = args, None

    def __enter__(self):
        deadline = time.time() + self.args.lock_wait
        waited = False
        while True:
            try:
                self.fd = os.open(GPU_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                # The holder line carries the pid, so a reader can tell who holds it.
                slug = re.sub(r"\s+", "_", f"comfy_gen-{self.args.cmd}-{getattr(self.args, 'name', '') or ''}")
                os.write(self.fd, f"token={uuid.uuid4()} pid={os.getpid()} slug={slug} "
                                  f"{datetime.datetime.now().astimezone().isoformat()}\n".encode())
                if waited:
                    print("GPU lock acquired", file=sys.stderr)
                return self
            except FileExistsError:
                if time.time() > deadline:
                    print(f"GPU lock {GPU_LOCK} still held after {self.args.lock_wait}s; try later "
                          "(never delete a lock you did not create)", file=sys.stderr)
                    sys.exit(4)
                if not waited:
                    print("waiting for the GPU lock (an engine run or another generation is using the GPU)",
                          file=sys.stderr)
                    waited = True
                time.sleep(5)

    def __exit__(self, *exc):
        os.close(self.fd)
        try:
            os.remove(GPU_LOCK)
        except OSError:
            pass


# ---------------------------------------------------------------- ComfyUI lifecycle 
# Before that the owner always started ComfyUI by hand (a taskbar shortcut to Comfy Desktop);
# agents only ever found it up or not. Now `ensure_running()` may start it, headless where
# possible, and this tool shuts down only what IT started - a ComfyUI the owner started is
# never touched. Design: one small state file under %TEMP%\comfy_gen\ records the pid, its
# creation time (so a reused pid is never mistaken for ours - see winproc.terminate_verified)
# and when it was last used; a detached "idle watcher" process (this script re-invoked as
# `stop --idle-watch`) wakes every 60s and stops ComfyUI once ~10 minutes have passed with no
# job touching last_used and the queue is empty. A watcher is simpler and more robust here than
# a foreground timer thread: it survives this process exiting (every command is one-shot) and
# a crashed watcher just means ComfyUI is not auto-stopped, not that it is wrongly stopped.
# LOCALAPPDATA\Temp, not TEMP: `stop` from the owner's shell must find the state an agent wrote,
# even when agents run with TEMP moved
STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or ".", "Temp", "comfy_gen")
STATE_FILE = os.path.join(STATE_DIR, "state.json")
IDLE_STOP_SECONDS = 600
DESKTOP_EXE_DEFAULT = r"C:\Program Files\Comfy Desktop\Comfy Desktop.exe"   # the installer's default


def desktop_exe(env=os.environ):
    """Comfy Desktop's exe: COMFY_DESKTOP_EXE when set, else the installer's default."""
    return env.get("COMFY_DESKTOP_EXE") or DESKTOP_EXE_DEFAULT


DESKTOP_EXE = desktop_exe()
APPDATA_COMFY = os.path.join(os.environ.get("APPDATA", ""), "Comfy Desktop")


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (dt.microsecond // 1000)


def _parse_iso(s):
    if not s:
        return None
    s = s[:-1] + "+00:00" if s.endswith("Z") else s
    try:
        return datetime.datetime.fromisoformat(s)
    except ValueError:
        return None


def read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return None


def write_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def clear_state():
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass


def _pid_alive_at(pid, ctime):
    """True when pid is still the exact process created at ctime (never a reused pid)."""
    d = W.details(pid)
    return d["ctime"] is not None and d["ctime"] == ctime


def _tool_started_and_alive(state):
    return bool(state and state.get("pid") is not None
                and _pid_alive_at(state["pid"], state.get("ctime")))


def touch_last_used(state=None):
    """Refresh the idle clock. Safe to call whether or not ComfyUI was started by this tool."""
    try:
        state = state or read_state()
        if state:
            state["last_used"] = _iso(_now())
            write_state(state)
    except OSError:
        pass


def _reachable(timeout=3):
    try:
        C.get_json("/system_stats", timeout=timeout)
        return True
    except Exception:
        return False


def _wait_for_stats(wait):
    deadline = time.time() + wait
    while time.time() < deadline:
        if _reachable():
            return True
        time.sleep(2)
    return False


def _agent_output_dir():
    """<AGENT_TEMP>\\comfy when AGENT_TEMP is set (process env, else the user's registry
    environment), else None. A ComfyUI this tool starts writes there, so agent generations land in
    the agents' temp folder rather than the ComfyUI install . A local ComfyUI
    keeps its folder."""
    root = os.environ.get("AGENT_TEMP")
    if not root:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                root = winreg.QueryValueEx(key, "AGENT_TEMP")[0]
        except (ImportError, OSError):
            root = None
    if not root:
        return None
    path = os.path.join(root, "comfy")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        return None
    return path


def _direct_launch_command():
    """The exact command Comfy Desktop would use for its last-opened install: the venv python
    running ComfyUI/main.py from the install path, with the same extra-model-paths config and
    input/output directories, and the install's own launchArgs. Read-only: nothing here is
    written to the Comfy Desktop config. Returns (argv, cwd) or (None, None) if any piece of
    that config is missing (caller falls back to Comfy Desktop.exe)."""
    try:
        with open(os.path.join(APPDATA_COMFY, "last-session.json"), encoding="utf-8") as f:
            inst_id = json.load(f)["installationId"]
        with open(os.path.join(APPDATA_COMFY, "installations.json"), encoding="utf-8") as f:
            installs = json.load(f)
        inst = next(i for i in installs if i.get("id") == inst_id)
        install_path = inst["installPath"]
        python_exe = os.path.join(install_path, "ComfyUI", ".venv", "Scripts", "python.exe")
        main_py = os.path.join("ComfyUI", "main.py")
        if not os.path.isfile(python_exe) or not os.path.isfile(os.path.join(install_path, main_py)):
            return None, None
        args = ["-s", main_py]
        yaml_cfg = os.path.join(APPDATA_COMFY, "instance-model-paths", f"{inst_id}.yaml")
        if os.path.isfile(yaml_cfg):
            args += ["--extra-model-paths-config", yaml_cfg]
        try:
            with open(os.path.join(APPDATA_COMFY, "settings.json"), encoding="utf-8") as f:
                settings = json.load(f)
        except (FileNotFoundError, ValueError):
            settings = {}
        if settings.get("inputDir"):
            args += ["--input-directory", settings["inputDir"]]
        out_dir = _agent_output_dir() or settings.get("outputDir")
        if out_dir:
            args += ["--output-directory", out_dir]
        # posix=False keeps Windows backslashes but also keeps the quotes round a token; Comfy
        # Desktop writes `--temp-directory "C:\ComfyUI"`, so strip one pair (seen 2026-10-01).
        args += [a[1:-1] if len(a) >= 2 and a[0] == a[-1] == '"' else a
                 for a in shlex.split(inst.get("launchArgs") or "", posix=False)]
        # Headless and on the same host/port this tool talks to (C.HOST), forced last so an
        # earlier occurrence in launchArgs never wins (argparse keeps the last of a repeated flag).
        parsed = urllib.parse.urlparse(C.HOST)
        args += ["--listen", parsed.hostname or "127.0.0.1", "--port", str(parsed.port or 8188),
                 "--disable-auto-launch"]
        return [python_exe] + args, install_path
    except (FileNotFoundError, KeyError, ValueError, StopIteration, OSError):
        return None, None


def _desktop_launch_command():
    """Fallback when the direct config above is not (fully) reproducible: start the Comfy Desktop
    app itself, the same way the owner's taskbar shortcut does. Less reliable to know exactly
    when /system_stats will answer (Electron startup, possibly a GUI window) but always available
    when the app is installed."""
    if not os.path.isfile(DESKTOP_EXE):
        return None, None
    return [DESKTOP_EXE], None


def _launch(wait):
    cmd, cwd = _direct_launch_command()
    source = "direct"
    if cmd is None:
        cmd, cwd = _desktop_launch_command()
        source = "desktop-exe"
        if cmd is None:
            raise RuntimeError("cannot start ComfyUI: no Comfy Desktop config at "
                              f"{APPDATA_COMFY} and no {DESKTOP_EXE}")
    print(f"[comfy_gen] ComfyUI not reachable at {C.HOST}; starting it ({source})", file=sys.stderr)
    os.makedirs(STATE_DIR, exist_ok=True)
    log_path = os.path.join(STATE_DIR, f"comfy-{datetime.datetime.now():%Y%m%d-%H%M%S}.log")
    logf = open(log_path, "ab")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
                            creationflags=flags, close_fds=True)
    d = W.details(proc.pid)
    write_state({"pid": proc.pid, "ctime": d["ctime"], "cmd": " ".join(cmd), "source": source,
                "log": log_path, "started": _iso(_now()), "last_used": _iso(_now())})
    print(f"[comfy_gen] ComfyUI starting, pid {proc.pid}; log: {log_path}", file=sys.stderr)


def _ensure_watcher(state):
    """A detached `stop --idle-watch` for this ComfyUI, unless one is already running."""
    wpid = state.get("watcher_pid")
    if wpid and W.details(wpid)["ctime"] is not None:
        return
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
        p = subprocess.Popen([sys.executable, "-B", os.path.abspath(__file__), "stop", "--idle-watch"],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=flags, close_fds=True)
    except OSError as e:
        print(f"[comfy_gen] could not start the idle watcher: {e} "
              "(ComfyUI will stay up until `stop` is run by hand)", file=sys.stderr)
        return
    state["watcher_pid"] = p.pid
    write_state(state)


def ensure_running(wait=120):
    """Make sure ComfyUI answers at C.HOST, starting it if needed. Returns True when THIS tool
    owns the running ComfyUI (freshly started, or still the one it started earlier) - the only
    case `stop`/the idle watcher will ever touch it. Returns False when it was already reachable
    and is not ours: the owner started it by hand, and it is left running untouched."""
    state = read_state()
    if _reachable():
        if _tool_started_and_alive(state or {}):
            touch_last_used(state)
            _ensure_watcher(read_state())
            return True
        if state:
            clear_state()   # stale record: a previous run of ours, now gone or pid reused
        return False
    if not _tool_started_and_alive(state or {}):
        clear_state()
        _launch(wait)
    if not _wait_for_stats(wait):
        state = read_state()
        raise RuntimeError(f"ComfyUI did not answer at {C.HOST} within {wait}s of starting it"
                          + (f" (log: {state['log']})" if state else ""))
    state = read_state()
    touch_last_used(state)
    _ensure_watcher(state)
    return True


def _stop_now(manual):
    state = read_state()
    if not state:
        if manual:
            print("[comfy_gen] nothing recorded as started by this tool; nothing to stop")
        return 0
    pid, ctime = state.get("pid"), state.get("ctime")
    d = W.details(pid)
    if d["ctime"] is None:
        print(f"[comfy_gen] pid {pid} is already gone")
    elif d["ctime"] != ctime:
        print(f"[comfy_gen] pid {pid} was reused by another process since we started ComfyUI; "
              "not touching it")
    else:
        victims = [(pid, ctime)]
        for p, ppid, _name in W.snapshot():
            if ppid == pid:
                dd = W.details(p)
                # a child started after ours; an older process only names our pid as a stale ppid
                if dd["ctime"] is not None and dd["ctime"] >= ctime:
                    victims.append((p, dd["ctime"]))
        for p, ct in victims:
            print(f"[comfy_gen] stop pid {p}: {W.terminate_verified(p, ct)}")
    clear_state()
    return 0


def _watch_and_stop():
    """Idle watcher body (`stop --idle-watch`): wake every 60s; stop once ~10 minutes have
    passed since the last job touched last_used and the queue is empty; exit the moment the
    state file no longer names this same pid (someone already stopped it, or replaced it)."""
    state = read_state()
    if not state:
        return 0
    watch_pid, watch_ctime = state.get("pid"), state.get("ctime")
    while True:
        time.sleep(60)
        state = read_state()
        if not state or state.get("pid") != watch_pid or state.get("ctime") != watch_ctime:
            return 0
        last = _parse_iso(state.get("last_used")) or _now()
        if (_now() - last).total_seconds() < IDLE_STOP_SECONDS:
            continue
        try:
            q = C.get_json("/queue", timeout=5)
            if q.get("queue_running") or q.get("queue_pending"):
                continue
        except Exception:
            pass   # unreachable is not "busy": stop it rather than leak it forever
        return _stop_now(manual=False)


def cmd_stop(args):
    if getattr(args, "idle_watch", False):
        return _watch_and_stop()
    return _stop_now(manual=True)


def engine_running():
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    return sorted({line.split(",")[0].strip('"') for line in out.splitlines() if "godot" in line.lower()})


def installed_files():
    names = set()
    for folder in C.get_json("/models"):
        try:
            names.update(os.path.basename(f.replace("\\", "/")) for f in C.get_json(f"/models/{folder}"))
        except Exception:
            pass
    return names


EXIT_GENERATOR_DOWN = 2
SAVED = []  # every file this command saved so far, named when the generator is lost mid-run


class GeneratorDown(Exception):
    """ComfyUI cannot be reached: not started, or lost mid-run."""


# what a lost connection to ComfyUI raises: a URLError whose reason is an OSError (refused, reset,
# timed out), or a dropped or stalled socket while a response is read. An HTTPError is a URLError
# too, but it is a live ComfyUI answering: never an outage (is_outage).
CONNECTION_ERRORS = (urllib.error.URLError, ConnectionError, TimeoutError)


def is_outage(e):
    if isinstance(e, urllib.error.HTTPError):
        return False
    if isinstance(e, urllib.error.URLError):
        return isinstance(e.reason, OSError)
    return isinstance(e, (ConnectionError, TimeoutError))


def outage_lines(why, saved):
    lines = [f"GENERATOR UNAVAILABLE: ComfyUI at {C.HOST}: {why}",
             "Stop this run and report the outage. Do not write prompts or placeholders in place "
             "of the outputs; rerun the command once the generator is back."]
    if saved:
        lines.append("saved before it was lost: " + ", ".join(saved))
    return lines


def _ensure_running_or_down():
    try:
        ensure_running()
    except RuntimeError as e:
        raise GeneratorDown(str(e))


def preflight(args, model_keys):
    _ensure_running_or_down()
    have = installed_files()
    missing = [f for k in model_keys for f in MODELS[k]["files"] if f not in have]
    if missing:
        print("missing model files (still downloading, or not installed): " + ", ".join(missing), file=sys.stderr)
        sys.exit(3)



RUN_FOLDER = None  # set once per command in run_handler


def run_job(api, timeout=900):
    """Submit with every save node writing into this command's own folder under ComfyUI's output/agent/."""
    for node in api.values():
        inputs = node.get("inputs", {})
        if isinstance(inputs.get("filename_prefix"), str):
            leaf = inputs["filename_prefix"].replace("\\", "/").rstrip("/").split("/")[-1] or "out"
            inputs["filename_prefix"] = f"{RUN_FOLDER}/{leaf}"
    return C.run(api, timeout=timeout)


def save_outputs(args, entry, meta, started, only_nodes=None):
    os.makedirs(args.out, exist_ok=True)
    saved = []
    items = [o for o in C.outputs(entry) if only_nodes is None or o[0] in only_nodes]
    for n, (nid, kind, item) in enumerate(items):
        ext = os.path.splitext(item["filename"])[1] or ".bin"
        stem = args.name if len(items) == 1 else f"{args.name}_{n + 1:02d}"
        path = os.path.join(args.out, stem + ext)
        with open(path, "wb") as f:
            f.write(C.view(item, timeout=600))
        side = dict(meta, file=os.path.basename(path), output_node=nid, output_kind=kind,
                    tool="ComfyUI (local)", generated=datetime.datetime.now().isoformat(timespec="seconds"),
                    seconds=round(time.time() - started, 1))
        with open(path + ".json", "w", encoding="utf-8") as f:
            json.dump(side, f, indent=2)
        saved.append(path)
        SAVED.append(path)
        print(f"saved {path}")
    if not saved:
        print("the run finished but produced no saved files", file=sys.stderr)
        sys.exit(5)
    return saved


def meta_for(key, **extra):
    m = MODELS[key]
    return {"model": m["name"], "licence": m["licence"], **extra}


def seed_of(args):
    return args.seed if args.seed is not None else random.randrange(2**48)


def set_input(api, node, name, value):
    if node not in api:
        raise SystemExit(f"node {node} not in this workflow")
    api[node]["inputs"][name] = value


def keep_outputs(api, keep):
    """Drop output nodes we don't want saved (previews stay: they write temp files only)."""
    for nid in [k for k, v in api.items() if v["class_type"].startswith("Save") and k not in keep]:
        del api[nid]


def prepare_object_image(args):
    """BiRefNet cut-out -> tight crop -> centred on a white square, saved next to the output."""
    from PIL import Image
    api = C.to_api(C.load_template("utility_birefnet_remove_background"))
    set_input(api, "17", "image", C.upload_image(args.image))
    api["90"] = {"class_type": "SaveImage", "inputs": {"images": ["19:16", 0], "filename_prefix": "agent/prep"}}
    entry = run_job(api, timeout=300)
    item = [o for o in C.outputs(entry) if o[0] == "90"][0][2]
    os.makedirs(args.out, exist_ok=True)
    cut = os.path.join(args.out, f"{args.name}_input_cutout.png")
    with open(cut, "wb") as f:
        f.write(C.view(item))
    im = Image.open(cut).convert("RGBA")
    box = im.getchannel("A").point(lambda a: 255 if a > 24 else 0).getbbox() or (0, 0, *im.size)
    obj = im.crop(box)
    side = int(max(obj.size) * 1.15)
    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    canvas.alpha_composite(obj, ((side - obj.size[0]) // 2, (side - obj.size[1]) // 2))
    prepared = os.path.join(args.out, f"{args.name}_input_prepared.png")
    canvas.convert("RGB").resize((1024, 1024)).save(prepared)
    return prepared


# ---------------------------------------------------------------- commands
def cmd_free(args):
    """Unload ComfyUI's models and free its RAM/VRAM, unless work is queued."""
    import urllib.request
    try:
        q = C.get_json("/queue", timeout=5)
        before = C.get_json("/system_stats", timeout=5)["devices"][0].get("vram_free", 0)
    except Exception as e:
        print(f"ComfyUI not reachable at {C.HOST}: {e}")
        return 2
    busy = len(q.get("queue_running", [])) + len(q.get("queue_pending", []))
    if busy:
        print(f"ComfyUI has {busy} job(s) running or queued; not freeing. Run `free` again when it is idle.")
        return 3
    req = urllib.request.Request(C.HOST + "/free", data=b'{"unload_models": true, "free_memory": true}',
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()
    time.sleep(3)
    after = C.get_json("/system_stats", timeout=5)["devices"][0].get("vram_free", 0)
    print(f"freed: VRAM free {before // 2**20} -> {after // 2**20} MiB")
    return 0


def cmd_check(args):
    try:
        stats = C.get_json("/system_stats", timeout=3)
    except Exception as e:
        # check never starts it (it holds no lock); it only says whether a job could
        if _direct_launch_command()[0] is None and _desktop_launch_command()[0] is None:
            print(f"ComfyUI not reachable at {C.HOST} ({e}) and no Comfy Desktop install to start it from")
            return 2
        busy = engine_running()
        print(f"ComfyUI is off; the first job starts it headless and it stops after "
              f"{IDLE_STOP_SECONDS // 60} idle minutes. Models are checked then.")
        print("  engine: " + (("RUNNING " + ", ".join(busy) + " -> wait or take the engine lock") if busy else "idle"))
        return 0
    have = installed_files()
    dev = stats.get("devices", [{}])[0]
    print(f"ComfyUI {stats['system'].get('comfyui_version')} at {C.HOST}; "
          f"GPU {dev.get('name', '?')} free {dev.get('vram_free', 0) // 2**20} MiB")
    code = 0
    for key, m in MODELS.items():
        missing = [f for f in m["files"] if f not in have]
        print(f"  {'OK     ' if not missing else 'MISSING'} {key:9} {m['name']}"
              + (f"  (missing: {', '.join(missing)})" if missing else ""))
        if missing:
            code = 3
    busy = engine_running()
    print("  engine: " + (("RUNNING " + ", ".join(busy) + " -> wait or take the engine lock") if busy else "idle"))
    return code


def cmd_image(args):
    if args.width % 16 or args.height % 16:
        raise SystemExit("width and height must be multiples of 16")
    if args.width * args.height > MAX_PIXELS:
        raise SystemExit(f"{args.width}x{args.height} is too large; stay near 1 MP and upscale later")
    preflight(args, [args.model])
    first = seed_of(args)
    for i in range(args.count):
        seed = first + i
        started = time.time()
        if args.model == "zimage":
            api = C.to_api(C.load_template("image_z_image_turbo"))
            set_input(api, "57:27", "text", args.prompt)
            set_input(api, "57:13", "width", args.width)
            set_input(api, "57:13", "height", args.height)
            set_input(api, "57:3", "seed", seed)
            set_input(api, "57:3", "steps", args.steps or 8)
            save = "9"
        else:
            api = C.to_api(C.load_template("image_krea2_turbo_t2i"))
            set_input(api, "30:19", "value", args.prompt)   # raw prompt
            set_input(api, "30:24", "value", False)         # no LLM prompt rewrite
            set_input(api, "30:23", "value", False)         # no darkbrush LoRA (licence unchecked)
            set_input(api, "30:5", "width", args.width)
            set_input(api, "30:5", "height", args.height)
            set_input(api, "30:3", "seed", seed)
            if args.steps:
                set_input(api, "30:3", "steps", args.steps)
            save = "29"
        keep_outputs(api, {save})
        entry = run_job(api, timeout=args.timeout)
        stem = args.name
        args.name = stem if args.count == 1 else f"{stem}_{i + 1:02d}"
        save_outputs(args, entry, meta_for(args.model, prompt=args.prompt, seed=seed,
                                           width=args.width, height=args.height), started, {save})
        args.name = stem
    return 0


def cmd_edit(args):
    preflight(args, ["edit"])
    started = time.time()
    api = C.to_api(C.load_template("image_qwen_image_edit_2511"))
    set_input(api, "41", "image", C.upload_image(args.image))
    if args.image2:
        set_input(api, "83", "image", C.upload_image(args.image2))
    else:
        del api["83"]
        for nid in ("170:149", "170:151"):
            api[nid]["inputs"].pop("image2", None)
    set_input(api, "170:151", "prompt", args.prompt)
    set_input(api, "170:168", "value", True)   # Lightning LoRA: 4 steps, cfg 1
    seed = seed_of(args)
    set_input(api, "170:169", "seed", seed)
    keep_outputs(api, {"9"})
    entry = run_job(api, timeout=args.timeout)
    return 0 if save_outputs(args, entry, meta_for("edit", prompt=args.prompt, seed=seed,
                                                   source=os.path.basename(args.image),
                                                   source2=os.path.basename(args.image2 or "")),
                             started, {"9"}) else 1


def cmd_rembg(args):
    preflight(args, ["rembg"])
    started = time.time()
    api = C.to_api(C.load_template("utility_birefnet_remove_background"))
    set_input(api, "17", "image", C.upload_image(args.image))
    api["90"] = {"class_type": "SaveImage", "inputs": {"images": ["19:16", 0], "filename_prefix": "agent/rembg"}}
    entry = run_job(api, timeout=args.timeout)
    return 0 if save_outputs(args, entry, meta_for("rembg", source=os.path.basename(args.image)),
                             started, {"90"}) else 1


def cmd_upscale(args):
    preflight(args, ["upscale"])
    started = time.time()
    api = {
        "1": {"class_type": "LoadImage", "inputs": {"image": C.upload_image(args.image)}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": "RealESRGAN_x4plus.safetensors"}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "SaveImage", "inputs": {"images": ["3", 0], "filename_prefix": "agent/upscale"}},
    }
    entry = run_job(api, timeout=args.timeout)
    return 0 if save_outputs(args, entry, meta_for("upscale", source=os.path.basename(args.image), scale=4),
                             started, {"4"}) else 1


def cmd_to3d(args):
    preflight(args, [args.engine])
    started = time.time()
    seed = seed_of(args)
    if args.engine == "hunyuan3d":
        # Hunyuan3D reconstructs whatever is in frame, background included (a flat plate
        # with the object in relief). Cut the object out, crop it and centre it on white first.
        prepared = prepare_object_image(args)
        api = C.to_api(C.load_template("3d_hunyuan3d-v2.1"))
        set_input(api, "2", "image", C.upload_image(prepared))
        set_input(api, "7", "seed", seed)
        set_input(api, "10", "filename_prefix", "agent/3d")
        keep_outputs(api, {"10"})
        outs = {"10"}
    else:
        api = C.to_api(C.load_template("3d_pixal3d_trellis2_image_to_model"))
        set_input(api, "122", "image", C.upload_image(args.image))
        set_input(api, "316", "value", args.engine == "trellis2")  # switch: TRELLIS.2 vs Pixal3D
        for k in C.find(api, "KSampler"):
            set_input(api, k, "seed", seed + int(k))
        if args.faces:
            set_input(api, "186", "target_face_count", args.faces)
        set_input(api, "288", "value", args.texture)  # baked texture size (template default 4096)
        set_input(api, "322", "filename_prefix", "agent/3d")
        keep_outputs(api, {"322"})
        outs = {"322"}
    entry = run_job(api, timeout=args.timeout)
    return 0 if save_outputs(args, entry, meta_for(args.engine, source=os.path.basename(args.image), seed=seed,
                                                   faces=args.faces, texture=args.texture), started, outs) else 1


def cmd_music(args):
    preflight(args, ["music"])
    started = time.time()
    seed = seed_of(args)
    api = C.to_api(C.load_template("audio_ace_step1_5_xl_turbo"))
    enc = C.find(api, "TextEncodeAceStepAudio1.5")[0]
    set_input(api, enc, "tags", args.tags)
    set_input(api, enc, "lyrics", args.lyrics)
    set_input(api, enc, "bpm", args.bpm)
    set_input(api, enc, "duration", float(args.seconds))
    set_input(api, enc, "seed", seed)
    set_input(api, C.find(api, "EmptyAceStep1.5LatentAudio")[0], "seconds", float(args.seconds))
    set_input(api, "3", "seed", seed)
    save = C.find(api, "SaveAudioAdvanced")[0]
    api[save] = {"class_type": "SaveAudio", "inputs": {"audio": api[save]["inputs"]["audio"],
                                                       "filename_prefix": "agent/music"}}
    entry = run_job(api, timeout=args.timeout)
    return 0 if save_outputs(args, entry, meta_for("music", tags=args.tags, lyrics=args.lyrics, bpm=args.bpm,
                                                   seconds=args.seconds, seed=seed), started, {save}) else 1


def cmd_template(args):
    if args.template.startswith("api_"):
        raise SystemExit("API templates are paid cloud nodes and are not approved")
    busy_ok = preflight_generic(args)
    api_check = C.to_api(C.load_template(args.template))
    paid = [v["class_type"] for v in api_check.values()
            if C.object_info().get(v["class_type"], {}).get("api_node")]
    if paid:
        raise SystemExit("this template uses paid API nodes (" + ", ".join(sorted(set(paid))) + "); not approved")
    started = time.time()
    api = C.to_api(C.load_template(args.template))
    for spec in args.load or []:
        node, path = spec.split("=", 1)
        set_input(api, node, "image", C.upload_image(path))
    for spec in args.set or []:
        key, val = spec.split("=", 1)
        node, name = key.split(".", 1)
        try:
            val = json.loads(val)
        except json.JSONDecodeError:
            pass
        set_input(api, node, name, val)
    if args.print_api:
        print(json.dumps(api, indent=1))
        return 0
    entry = run_job(api, timeout=args.timeout)
    save_outputs(args, entry, {"model": f"template {args.template}", "licence": "CHECK: see knowledge base",
                               "settings": args.set, "loaded": args.load}, started)
    return 0 if busy_ok else 0


def preflight_generic(args):
    _ensure_running_or_down()
    return True


def main():
    argv = sys.argv[1:]
    if argv and argv[0].startswith("--") and argv[0] not in ("-h", "--help"):
        argv = (["check"] if argv[0] == "--check" else ["image"]) + [a for a in argv if a != "--check"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, needs_out=True):
        if needs_out:
            p.add_argument("--out", required=True, help="directory to save into")
            p.add_argument("--name", required=True, help="file name stem")
        p.add_argument("--seed", type=int)
        p.add_argument("--timeout", type=int, default=900)
        p.add_argument("--force", action="store_true",
                       help="do not take this tool's GPU self-lock because the caller already holds "
                            "it (the machine lock is still taken)")
        p.add_argument("--lock-wait", type=int, default=900, help="seconds to wait for the GPU lock (exit 4 after)")
        return p

    sub.add_parser("check")
    sub.add_parser("free")
    p = sub.add_parser("stop", help="stop ComfyUI if this tool started it (no-op otherwise)")
    p.add_argument("--idle-watch", action="store_true", help=argparse.SUPPRESS)  # internal: the detached watcher
    p = sub.add_parser("release")   # internal: the local-ai run's --on-release (cmd_release)
    p.add_argument("--outer", default="")
    p = common(sub.add_parser("image"))
    p.add_argument("--prompt", required=True)
    p.add_argument("--model", choices=["zimage", "krea2"], default="zimage")
    p.add_argument("--width", type=int, default=1024)
    p.add_argument("--height", type=int, default=1024)
    p.add_argument("--count", type=int, default=1, choices=range(1, 9))
    p.add_argument("--steps", type=int)
    p = common(sub.add_parser("edit"))
    p.add_argument("--image", required=True)
    p.add_argument("--image2")
    p.add_argument("--prompt", required=True)
    p = common(sub.add_parser("rembg"))
    p.add_argument("--image", required=True)
    p = common(sub.add_parser("upscale"))
    p.add_argument("--image", required=True)
    p = common(sub.add_parser("to3d"))
    p.add_argument("--image", required=True)
    p.add_argument("--engine", choices=["trellis2", "pixal3d", "hunyuan3d"], default="trellis2")
    p.add_argument("--faces", type=int, default=50000,
                   help="target face count before UV unwrap (template default 700000; 50000 is plenty to clean up)")
    p.add_argument("--texture", type=int, default=2048, choices=[512, 1024, 2048, 4096],
                   help="baked texture size for trellis2/pixal3d (4096 makes ~20 MB GLBs)")
    p = common(sub.add_parser("music"))
    p.add_argument("--tags", required=True, help="style, instruments, mood, BPM words")
    p.add_argument("--lyrics", default="[Instrumental]")
    p.add_argument("--seconds", type=float, default=60)
    p.add_argument("--bpm", type=int, default=120)
    p = common(sub.add_parser("template"))
    p.add_argument("--template", required=True)
    p.add_argument("--set", action="append", help="NODE.INPUT=JSON-or-text")
    p.add_argument("--load", action="append", help="NODE=local image path for a LoadImage node")
    p.add_argument("--print-api", action="store_true", help="print the converted prompt and stop")

    args = ap.parse_args(argv)
    if args.cmd not in UNLOCKED and not getattr(args, "print_api", False):
        machine_lock_rerun(args, argv)
    if (args.cmd not in UNLOCKED and not getattr(args, "force", False)
            and not getattr(args, "print_api", False)):
        with gpu_lock(args):
            return run_handler(args)
    return run_handler(args)


# the kit's machine lock entry point: <plugin root>/tools/machine_lock.py, or KIT_MACHINE_LOCK to override
MACHINE_LOCK_TOOL = os.environ.get("KIT_MACHINE_LOCK") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "tools", "machine_lock.py")


RERUN_ENV = "COMFY_GEN_LOCKED"     # set only for the command this function re-runs
UNLOCKED = ("check", "free", "stop", "release")   # commands that never take a slot


def release_cmdline():
    """The --on-release command: `release`, told which run (if any) encloses this one, so a run
    nested in it (holding no slot of its own) leaves ComfyUI to that run."""
    return subprocess.list2cmdline([sys.executable, "-B", os.path.abspath(__file__), "release",
                                    "--outer=" + os.environ.get("MACHINE_LOCK_RUN", "")])


def machine_lock_rerun(args, argv):
    """Re-run this command under the machine-wide lock (kind local-ai), then exit with its code.
    Returns without re-running only in that re-run itself (RERUN_ENV, consumed here so nothing it
    starts inherits it). Inside another locked run (MACHINE_LOCK_RUN) it still goes through the
    tool: that runs it directly when the enclosing run holds gpu and takes gpu when it holds only
    cpu. --force goes through it too (it skips only the GPU self-lock). With no tool it refuses
    (exit 2) rather than run on a GPU slot nobody holds. The run's --on-release is `release`: the
    ComfyUI it used is freed when the slot ends, not ~10 idle minutes later."""
    if os.environ.pop(RERUN_ENV, None) and os.environ.get("MACHINE_LOCK_RUN"):
        return
    if not os.path.isfile(MACHINE_LOCK_TOOL):
        print(f"[machine-lock] refused: no machine lock tool at {MACHINE_LOCK_TOOL}, and generation "
              "takes a local-ai slot. Install the kit's tools/machine_lock.py or set "
              "KIT_MACHINE_LOCK to it. Exit 2.", file=sys.stderr)
        sys.exit(2)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", getattr(args, "name", None) or args.cmd).strip("-")[:48] or args.cmd
    wait = max(1, min(int(getattr(args, "lock_wait", 540)), 540))
    cmd = [sys.executable, "-B", MACHINE_LOCK_TOOL, "run", "--repo", "comfy", "--slug", stem,
           "--kind", "local-ai", "--max-wait", f"{wait}s", "--on-release", release_cmdline(), "--",
           sys.executable, os.path.abspath(__file__)] + list(argv)
    sys.exit(subprocess.call(cmd, env=dict(os.environ, **{RERUN_ENV: "1"})))


def cmd_release(args):
    """The local-ai run's --on-release: its slot is ending, so free ComfyUI now. A ComfyUI this tool
    started is stopped (with its children, ctime-checked); a local ComfyUI is never stopped, only
    its models unloaded, and only when its queue is idle (as `free`). Skipped when the run was
    nested in an enclosing run (MACHINE_LOCK_RUN still the enclosing one's): that run still holds
    the slot. Always exits 0; what it did is printed."""
    if os.environ.get("MACHINE_LOCK_RUN", "") == (args.outer or ""):
        print("[comfy_gen] release: nested in an enclosing run that still holds the GPU; "
              "ComfyUI left as is")
        return 0
    state = read_state()
    if _tool_started_and_alive(state):
        print("[comfy_gen] release: the local-ai slot ended; stopping the ComfyUI this tool started")
        _stop_now(manual=False)
    elif _reachable():
        print("[comfy_gen] release: the local-ai slot ended; freeing the models of the running "
              "ComfyUI (not started by this tool, so it stays up)")
        try:
            cmd_free(args)
        except Exception as e:      # an on-release step reports, it never fails the run
            print(f"[comfy_gen] release: free failed: {e}")
    else:
        print("[comfy_gen] release: no ComfyUI running; nothing to free")
    return 0


def run_handler(args):
    global RUN_FOLDER
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", getattr(args, "name", None) or args.cmd).strip("-")[:48] or args.cmd
    RUN_FOLDER = f"agent/{datetime.datetime.now():%Y%m%d-%H%M%S}_{args.cmd}_{stem}"
    handler = {"check": cmd_check, "free": cmd_free, "stop": cmd_stop, "release": cmd_release,
               "image": cmd_image, "edit": cmd_edit,
               "rembg": cmd_rembg, "upscale": cmd_upscale, "to3d": cmd_to3d, "music": cmd_music,
               "template": cmd_template}[args.cmd]
    del SAVED[:]
    try:
        code = handler(args) or 0
    except GeneratorDown as e:
        print("\n".join(outage_lines(e, SAVED)), file=sys.stderr)
        code = EXIT_GENERATOR_DOWN
    except urllib.error.HTTPError as e:     # a URLError, but ComfyUI answered: the job failed
        print(f"ComfyUI at {C.HOST} answered HTTP {e.code}: {e.reason}", file=sys.stderr)
        code = 1
    except CONNECTION_ERRORS as e:
        if is_outage(e):
            print("\n".join(outage_lines(f"lost mid-run ({e})", SAVED)), file=sys.stderr)
            code = EXIT_GENERATOR_DOWN
        else:
            print(f"ComfyUI at {C.HOST}: {e}", file=sys.stderr)
            code = 1
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        code = 1
    finally:
        # Refresh the idle clock the moment the job ends (a no-op if ComfyUI is not ours, or
        # the owner's): the ~10-minute idle window the watcher counts starts from here.
        touch_last_used()
    sys.exit(code)


if __name__ == "__main__":
    main()
