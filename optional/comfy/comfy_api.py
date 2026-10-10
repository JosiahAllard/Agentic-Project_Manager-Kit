"""Talk to the owner's local ComfyUI and turn its UI templates into API prompts.

Local only: every request goes to COMFY_URL (default http://127.0.0.1:8188).
Nothing here downloads models, installs nodes or calls a paid service.

Main pieces:
  get/post/upload/view      thin HTTP helpers
  load_template(name)       a template JSON as served by ComfyUI
  to_api(workflow)          UI workflow (with subgraphs) -> API prompt dict
  run(prompt)               queue a prompt, wait, return its history entry
  outputs(entry)            every file the run produced (images, 3D, audio)
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HOST = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
FRONTEND_ONLY = {"Note", "MarkdownNote", "PrimitiveNode", "Reroute"}
CONNECTION_TYPES_ARE_UPPER = True  # widget types: INT FLOAT STRING BOOLEAN COMBO / list

_object_info = None


# ---------------------------------------------------------------- http
def get(path, timeout=15):
    with urllib.request.urlopen(HOST + path, timeout=timeout) as r:
        return r.read()


def get_json(path, timeout=15):
    return json.loads(get(path, timeout))


def post_json(path, payload, timeout=30):
    req = urllib.request.Request(HOST + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ComfyUI refused the request: {e.read().decode(errors='replace')[:3000]}")


def upload_image(path, subfolder="agent"):
    """Upload a local image into ComfyUI's input folder; returns the name LoadImage wants."""
    boundary = uuid.uuid4().hex
    name = os.path.basename(path)
    with open(path, "rb") as f:
        data = f.read()
    body = b"".join([
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"subfolder\"\r\n\r\n{subfolder}\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n".encode(),
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"{name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode(), data, b"\r\n",
        f"--{boundary}--\r\n".encode()])
    req = urllib.request.Request(HOST + "/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.loads(r.read())
    return f"{res['subfolder']}/{res['name']}" if res.get("subfolder") else res["name"]


def view(item, timeout=120):
    q = urllib.parse.urlencode({"filename": item["filename"], "subfolder": item.get("subfolder", ""),
                                "type": item.get("type", "output")})
    return get(f"/view?{q}", timeout)


# ---------------------------------------------------------------- templates
def object_info():
    global _object_info
    if _object_info is None:
        _object_info = get_json("/object_info", timeout=60)
    return _object_info


def load_template(name):
    return get_json(f"/templates/{name}.json")


def _widget_inputs(class_type):
    """Ordered (name, spec, has_control) for every widget input of a node class."""
    info = object_info().get(class_type)
    if info is None:
        raise RuntimeError(f"node type '{class_type}' is not installed in ComfyUI")
    out = []
    for section in ("required", "optional"):
        for name, spec in (info.get("input", {}).get(section) or {}).items():
            typ = spec[0] if isinstance(spec, (list, tuple)) and spec else spec
            opts = spec[1] if isinstance(spec, (list, tuple)) and len(spec) > 1 and isinstance(spec[1], dict) else {}
            is_widget = isinstance(typ, list) or typ in (
                "INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "COLOR", "LOAD_3D", "COMFY_DYNAMICCOMBO_V3") or (
                "default" in opts and not str(typ).isupper())
            if opts.get("forceInput"):
                is_widget = False
            if is_widget:
                control = bool(opts.get("control_after_generate")) or (
                    typ == "INT" and name in ("seed", "noise_seed"))
                out.append((name, typ, control, opts))
    return out


def _widget_values(node):
    """Map a UI node's widgets_values onto input names."""
    wv = node.get("widgets_values")
    if isinstance(wv, dict):
        return {k: v for k, v in wv.items() if k not in ("videopreview",)}
    values = {}
    if not isinstance(wv, list):
        return values
    i = 0
    for name, typ, control, opts in _widget_inputs(node["type"]):
        if i >= len(wv):
            break
        values[name] = wv[i]
        i += 1
        if control and i < len(wv) and wv[i] in ("fixed", "increment", "decrement", "randomize"):
            i += 1
        if typ == "COMFY_DYNAMICCOMBO_V3":
            # the chosen option's own widgets follow it, keyed "<combo>.<input>"
            chosen = next((o for o in opts.get("options", []) if o.get("key") == values[name]), None)
            for sub in ((chosen or {}).get("inputs", {}).get("required") or {}):
                if i < len(wv):
                    values[f"{name}.{sub}"] = wv[i]
                    i += 1
    return values


def _flatten(workflow):
    """Expand subgraph instances into one flat list of nodes and links.

    Returns (nodes, links, primitive_values) where every node id is a string and
    each link is (origin_id, origin_slot, target_id, target_input_name).
    """
    subgraphs = {s["id"]: s for s in workflow.get("definitions", {}).get("subgraphs", [])}
    nodes = {}
    links = []

    def link_rows(raw):
        rows = []
        for l in raw or []:
            if isinstance(l, dict):
                rows.append((l["id"], l["origin_id"], l["origin_slot"], l["target_id"], l["target_slot"]))
            else:
                rows.append((l[0], l[1], l[2], l[3], l[4]))
        return rows

    def add_level(level_nodes, level_links, prefix, sg_inputs, sg_outputs_sink):
        """sg_inputs: slot -> ("link", origin, slot) or ("value", v) for this level's -10 node."""
        by_id = {n["id"]: n for n in level_nodes}
        # Which input name a target slot is, per node.
        def input_name(node, slot):
            ins = node.get("inputs", [])
            return ins[slot]["name"] if slot < len(ins) else None

        inner_outputs = {}  # (inner instance id, out slot) -> (origin_id, origin_slot)
        for n in level_nodes:
            nid = f"{prefix}{n['id']}"
            if n["type"] in subgraphs and n.get("mode", 0) not in (2, 4):
                sg = subgraphs[n["type"]]
                # Resolve this instance's inputs: linked at this level, or a promoted widget value.
                resolved = {}
                inst_links = {r[4]: r for r in link_rows(level_links) if r[3] == n["id"]}
                widget_vals = n.get("widgets_values") if isinstance(n.get("widgets_values"), list) else []
                sg_names = [sgi["name"] for sgi in sg["inputs"]]
                sg_slot = {}
                # Older saves keep one value per subgraph input on the instance.
                if widget_vals and len(widget_vals) == len(sg_names):
                    for j, v in enumerate(widget_vals):
                        sg_slot[j] = ("value", v)
                # Links into the instance win over stored values; match by input name.
                for k, inp in enumerate(n.get("inputs", [])):
                    if k in inst_links and inp["name"] in sg_names:
                        r = inst_links[k]
                        sg_slot[sg_names.index(inp["name"])] = ("link", f"{prefix}{r[1]}", r[2])
                sink = {}
                add_level(sg["nodes"], sg["links"], f"{nid}:", sg_slot, sink)
                for out_slot, src in sink.items():
                    inner_outputs[(n["id"], out_slot)] = src
                continue
            nodes[nid] = n

        for (_, o_id, o_slot, t_id, t_slot) in link_rows(level_links):
            if t_id == -20:
                src = (f"{prefix}{o_id}", o_slot)
                if (o_id, o_slot) in inner_outputs:
                    src = inner_outputs[(o_id, o_slot)]
                sg_outputs_sink[t_slot] = src
                continue
            target = by_id.get(t_id)
            if target is None or target["type"] in subgraphs:
                continue  # instance inputs were resolved above
            name = input_name(target, t_slot)
            if o_id == -10:
                src = sg_inputs.get(o_slot)
                if src is None:
                    continue
                if src[0] == "value":
                    key = f"{prefix}{t_id}"
                    current = nodes.get(key, target)
                    nodes[key] = dict(current, _override={**current.get("_override", {}), name: src[1]})
                    continue
                links.append((src[1], src[2], f"{prefix}{t_id}", name))
                continue
            if (o_id, o_slot) in inner_outputs:
                o, s = inner_outputs[(o_id, o_slot)]
                links.append((o, s, f"{prefix}{t_id}", name))
            else:
                links.append((f"{prefix}{o_id}", o_slot, f"{prefix}{t_id}", name))

    add_level(workflow["nodes"], workflow.get("links", []), "", {}, {})
    return nodes, links


def to_api(workflow):
    """Convert a UI workflow (as saved or served as a template) into an API prompt."""
    nodes, links = _flatten(workflow)
    api = {}
    for nid, n in nodes.items():
        if n["type"] in FRONTEND_ONLY or n.get("mode", 0) in (2, 4):
            continue
        inputs = _widget_values(n)
        inputs.update(n.get("_override", {}))
        api[nid] = {"class_type": n["type"], "inputs": inputs}
    for o_id, o_slot, t_id, name in links:
        if t_id not in api or name is None:
            continue
        src = nodes.get(o_id)
        if src is None:
            continue
        if src["type"] == "PrimitiveNode":
            wv = src.get("widgets_values") or []
            if wv:
                api[t_id]["inputs"][name] = wv[0]
            continue
        if src["type"] == "Reroute":
            # follow the reroute back to its source
            back = [l for l in links if l[2] == o_id]
            if not back:
                continue
            o_id, o_slot = back[0][0], back[0][1]
        if o_id in api:
            api[t_id]["inputs"][name] = [o_id, o_slot]
    return api


def find(api, class_type):
    """Ids of every node of a class in an API prompt."""
    return [k for k, v in api.items() if v["class_type"] == class_type]


# ---------------------------------------------------------------- running
def run(api, timeout=900, poll=2):
    job = post_json("/prompt", {"prompt": api, "client_id": "claude-agent"})
    if job.get("node_errors"):
        # ComfyUI still runs the outputs that validated; a partial run is a failure for us.
        try:
            post_json("/queue", {"delete": [job["prompt_id"]]})
            post_json("/interrupt", {})
        except Exception:
            pass
        raise RuntimeError(f"ComfyUI rejected part of the workflow: {json.dumps(job['node_errors'])[:3000]}")
    pid = job["prompt_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        hist = get_json(f"/history/{pid}")
        if pid in hist:
            entry = hist[pid]
            st = entry.get("status", {})
            if st.get("status_str") == "error" or (st.get("completed") and st.get("status_str") != "success"):
                msgs = [m for m in st.get("messages", []) if m[0] == "execution_error"]
                raise RuntimeError(f"ComfyUI job failed: {json.dumps(msgs)[:3000]}")
            if st.get("completed"):
                return entry
        time.sleep(poll)
    raise RuntimeError(f"timed out after {timeout}s waiting for job {pid}")


def outputs(entry, kinds=("images", "3d", "audio", "gifs", "files", "mesh", "model_3d", "result")):
    """Every saved file in a finished run, as (node_id, kind, item) with type 'output'."""
    found = []
    for nid, out in entry.get("outputs", {}).items():
        for kind, items in out.items():
            if not isinstance(items, list):
                continue
            for it in items:
                if isinstance(it, dict) and "filename" in it and it.get("type", "output") == "output":
                    found.append((nid, kind, it))
    return found
