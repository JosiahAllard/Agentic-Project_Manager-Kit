# Best practices

Rules of thumb that have held up. Add a rule only once a lesson has repeated in a repository's
`docs/ops/assets.md` (README rule 5).

## Images

- **Stay near 1 megapixel.** Use 1024x1024, or the same area in another shape (896x1088,
  1152x896). Much larger images lose detail, repeat parts and saturate the GPU for everyone.
  Upscale afterwards with `upscale`.
- **Anything that will ship uses Z-Image** (Apache-2.0). Krea 2 follows shape details better
  but is **reference only** by the owner's decision (licence revenue cap), so use it for
  concepts and blockouts, then reproduce the design with Z-Image, or model it, for the real
  asset. (MODELS.md)
- **Write the prompt like a spec:** subject first, then every detail you need in order of
  importance, then framing ("full length, centered, side view"), background ("plain mid-grey
  background" or "plain white background"), lighting ("even studio lighting") and exclusions
  ("no text, no hands"). Whatever you leave out, the model invents.
- **Ask for a plain background** for anything that will be cut out or turned into 3D.
- **Try a few seeds** (`--count 4`) before rewriting the prompt. Composition changes a lot
  between seeds.
- **For variants use `edit`, not a new prompt.** "Change X to Y. Keep everything else exactly
  the same." keeps the design identical. It is the right tool for team colours and skins.

## Image to 3D

- **Pipeline:** a concept on a plain background (Z-Image if the mesh may ship, Krea 2 for
  reference), then **show the owner the candidate images and wait for a pick** (owner's
  rule), then `to3d --engine trellis2` on the chosen image, then
  render with `render_glb.py` and look, then clean up in Blender (skill `specialty-3d-assets`: scale, pivot,
  budget, separate materials for glowing parts).
- **TRELLIS.2 first.** Hunyuan3D gives a clean untextured shape but is **reference only**
  (licence region exclusions). Pixal3D only as a third opinion.
- **Hunyuan3D needs the object alone:** cut out, cropped, centred on white. `comfy_gen.py`
  does this automatically. Giving it the raw image produces a flat plate.
- **Keep `--faces` at 50000 or less and `--texture` at 2048 or less.** The template defaults
  (700k faces, 4096² textures) make 20+ MB files that need heavy reduction anyway.
- **Glow does not survive** as emission: generated textures bake it into colour. Rebuild
  glowing parts as their own emissive material.
- **A 3D model is never checked by reading its numbers.** Render it from at least front, three
  quarter and side.

## Art that is code

- **Reach for a shader before a generator** for ribbons, trails, beams and rings. An analytic
  core, rim and banding in a shader beats a painted streak texture and adds no binary asset or
  licence to record. Consider a generated sheet only for effects like smoke or sparks.

## Music

- **Tags, not sentences:** genre, mood, instruments, BPM, and "instrumental, loopable, video
  game soundtrack". Lyrics `[Instrumental]`. Keep `--bpm` consistent with the tags.
- **Short first:** 30 s to judge a direction, then generate the full length.
- **Nobody here can hear.** Measure the file (length, loudness, clipping) and hand the owner
  a playable page. The owner is the ear.

## Sound effects

- **ACE-Step is music only.** One-shot effects come from procedural synthesis or a CC0 library.
- **Measure a candidate against the set it joins.** Compare crest factor and RMS with the clip
  it replaces: a procedural explosion whose crest factor needed a peak above 0 dBFS to match
  the set's level failed on measurement, which justified a library import instead.
- **Decode library previews with Blender's bundled `aud` module** (`blender --background
  --python`) rather than installing a decoder.
- **Do not judge brightness or band motion from a saturating spectrogram colormap.** Measure a
  per-frame spectral centroid instead.
- **Prove the engine loaded the new file** (sample count, or a SHA-256 of the decoded PCM). A
  green build is not proof: an engine can silently keep the old sound when its import sidecar
  is missing (in Godot, `.import` files are often gitignored, so run a headless import).

## Scheduling

- Each command is one GPU slice. Run heavy jobs (`edit`, `music`, `to3d`) one at a time.
- Batch generation into a quiet window agreed with the coordinator instead of interleaving it
  with engine or network test runs: heavy generation slows them.
