# Creative work: brief, options first, generation, delivery, limits

Shared by the specialty-3d-assets, specialty-tech-art and specialty-sound skills; the same bytes in
each folder.

## The brief block

Every creative brief carries this block, filled in by the coordinator:

> FEEL: one sentence. REFERENCES: one to three (a game and what specifically, or an image or clip
> path, including any inspiration the owner sent). AVOID: one line. SCOPE OF CHOICE: the agent may
> propose X, may not change Y.

A missing FEEL does not stop the work: propose one, mark it a default, and say in your report that
the block was missing. A coordinator that cannot fill FEEL asks the owner once, through the PM,
using your options as the question.

## Options first

Anything new with a creative choice in it runs options first, one dispatch per step. A fix to
something the owner already approved skips to the fix.

0. **Step 0, cheap and wide.** One labelled sheet of 5-10 very different designs that differ in
   kind (silhouette, concept, source), not in tint or proportion. Built from the owner's
   inspiration where the owner sent some, naming what each takes from which. Publish it and stop:
   the owner narrows it.
1. **Intent, before building.** In your scratch note: what it must feel like in play, what reads
   at gameplay distance (or in the mix), and what you take from each reference.
2. **Takes.** From the narrowed set, about three takes that differ in kind, on one page, same
   camera or same playback: each labelled with its intent and the knobs that differ, your pick
   marked with why. Small, background or repeated assets can be one take.
3. **Polish only the take the owner picked.** Never polish one the owner has not chosen.

## Generation: the owner's local models

- Images, meshes from images, and music come through `python <kit>/optional/comfy/comfy_gen.py`.
  Read `<kit>/knowledge/asset-generation/README.md`, with `MODELS.md` and `BEST_PRACTICES.md`
  beside it, before generating, and follow its rules.
- A generator outage stops the run: report it; start and install nothing.
- Your session record goes as keyword lines in the repo's `docs/ops/assets.md`, failures
  included, never in your host's user folder.
- Generate only in the windows your coordinator gives you: never in a measurement window, never
  overlapping an engine, network or render run.
- Check the repo's asset policy before relying on any generated asset; anything it does not yet
  allow is the owner's decision.

## Limits a worker type does not enforce for you

A worker type carries the full tool set; these limits are yours to keep, and the brief may only
narrow them:

- Paid generation (an API key, a paid service, a credits node) needs the owner's approval for that
  specific task. Never sign up for, spend on, or reach for a source the profile marks shelved.
- Never wire a new asset into the game, or replace an existing one, unless the brief says to:
  changing how the game looks or sounds is the owner's decision.
- Never invent an asset, a licence or an attribution; record every import's licence and origin
  where the project records them, public domain included.
- Never change gameplay, physics or netcode to make something look or sound better: report the
  conflict.
- Needs something no route here produces (a rigged or animated character, a hand-painted
  texture, a recorded performance): stop and report exactly what - subject, format, size or
  budget, where it would live - so the owner can source it. Never ship a placeholder that could
  pass for the real thing.

## Delivering it

- Output ends the way the repo profile's **Art** (or **Audio**) section says. Default when it is
  silent: a self-contained HTML page in your scratch folder, outside the worktree, images inlined
  and clips playable, each item beside its measurements and, for a change, its before.
- Your report leads with that absolute path, or the URL where the profile says published pages
  reach the owner. No route at all: say so and give workspace-relative paths to every file.
- **Pages, renders and clips are disposable.** Delete them once the owner approves. Never delete
  before approval, or one you did not publish. Renders are never committed.

## Critique before you report

Write the three weakest things you can see (or measure) against the brief - the look or the
sound, not the bookkeeping. Fix the one that costs least; list the rest.
