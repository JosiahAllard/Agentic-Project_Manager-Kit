# Asset generation: shared knowledge base

Every agent on this machine reads this folder before generating anything and writes to it
afterwards. It holds what we have learnt about making game assets with the owner's local
models: what works, what does not, and how to do it well. Nothing here is specific to one
repository. Repository rules (asset policy, attribution format, art direction) live in each
repository and win over this folder.

| File | What it holds | Who writes |
|---|---|---|
| `MODELS.md` | The approved models, licences, measured speed, strengths and weaknesses | Update when a model is added or a measurement changes |
| `BEST_PRACTICES.md` | Distilled recipes and rules of thumb | Owner only. Agents propose a rule once it has held up at least twice in a repo's `docs/ops/assets.md` |

## The tool

```
python ~/.claude/tools/comfy/comfy_gen.py check
python ~/.claude/tools/comfy/comfy_gen.py <image|edit|rembg|upscale|to3d|music|template> --help
python ~/.claude/tools/comfy/render_glb.py  (run through Blender, see its docstring)
python ~/.claude/tools/comfy/compare_sheet.py OUT.html [LABEL=]IMAGE ... [--cols N]
```

- It only talks to ComfyUI on this machine (`127.0.0.1:8188`). The owner starts and stops
  ComfyUI; agents never start, install, download or update anything in it.
- It takes the machine-wide GPU lock for each command (see below) and writes a sidecar
  `<file>.json` next to every output: model, licence, prompt, seed, settings, date and
  seconds taken. Copy those fields into the repository's attribution record.
- Each command saves ComfyUI's own copies into its own folder,
  `output/agent/<YYYYmmdd-HHMMSS>_<command>_<name>/`; the tool does this itself, so
  do not set a `filename_prefix` by hand. Those copies are only a cache, and the
  owner may clear `output/` at any time. (Owner's rule, 2026-09-19.)
- **`--out` is always inside the repository the asset is for**: the worktree's
  asset folder, never a scratch or temp folder. That makes the file and its
  sidecar part of the branch as soon as it is generated. Candidates the owner
  has not picked yet go there too; delete the rejected ones on the branch before
  it lands. (Owner's rule, 2026-09-19.)
- `comfy_api.py` converts any installed ComfyUI template into an API prompt, so a new
  workflow does not need hand-building: `comfy_gen.py template --template NAME --print-api`.

## Rules every agent follows

1. **Only approved models** (`MODELS.md`). A new model needs the owner's approval and a
   licence check first. Templates or nodes marked as API, partner or "requires credits" are
   paid cloud services and are never used.
2. **Share the machine.** Generation uses the same GPU as the game engine and render runs.
   The tool holds the GPU lock for one command at a time, so keep each command to one
   slice of about 10 minutes at most. Do not generate during a measurement window. Batch
   generation into quiet periods when the coordinator says the machine is not busy, rather
   than interleaving it with engine and network test runs, because heavy generation slowed
   test runs on 2026-09-18.
3. **Look at every output you keep.** Read the image back, or render a 3D model with
   `render_glb.py`, and describe what you see. A validator or a file size is not a check.
4. **Generated output is a source, not a finished asset.** Say what clean-up it needs
   (cut-out, tiling, retopology, scale, pivot, materials) and do it, or hand it on.
5. **Record it, in the repository.** Agents write nothing under `~/.claude` (owner,
   2026-09-20). Add one keyword line per session, failures included, to the repo's
   `docs/ops/assets.md`: `date | command | model | what for | result | lesson`. When a lesson has
   held up twice, propose it to the owner for `BEST_PRACTICES.md`, citing those lines.
6. **The owner approves images before 3D.** When images are made as the starting point for
   a 3D model, publish the candidates for the owner, recommend one, and wait. `to3d` runs
   only on an image the owner has picked. (Owner's rule, 2026-09-18.)
7. **Outage stops the run.** If the generator is down (`check` fails, or a command exits 2
   with `GENERATOR UNAVAILABLE`), stop and report the outage and what was saved. Do not
   produce prompts, placeholders or a sheet of them in place of outputs.
8. **Compare side by side.** Put candidates on one labelled page with `compare_sheet.py`
   (images, or the tiles `render_glb.py --keep-tiles` writes); `render_glb.py --grid COLS`
   renders many models from one view in one sheet.
