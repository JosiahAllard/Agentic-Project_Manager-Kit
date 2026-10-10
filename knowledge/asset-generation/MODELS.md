# Approved models

Measured on the owner's PC: RTX 3080 Ti (12 GB), 64 GB RAM, ComfyUI 0.36.0 Desktop.
Times are one run each on 2026-09-18, with other work competing for the GPU at times, so
treat them as rough. Licences were checked on each model's official page on 2026-09-18.
Recheck before shipping if a model is updated.

| `comfy_gen.py` key | Model | Licence | Time (measured) | Best for | Known weaknesses |
|---|---|---|---|---|---|
| `image --model krea2` | Krea 2 Turbo (fp8, 12B) | **Krea 2 Community License**: commercial use only while the company's annual revenue is under USD 1M; acceptable-use policy applies | ~40 s per 1024², warm | **Reference only (owner decision).** Best prompt adherence seen (drew "two parallel prongs" correctly); clean concept art and props | Revenue limit. Its template can also run an LLM prompt-rewriter and a style LoRA; the tool switches both off |
| `image --model zimage` | Z-Image-Turbo (6B) | Apache-2.0 | 30 s warm; 200 s when it had to load while memory was short | **Default for shippable images.** Fast, photoreal, clean lettering in images | Ignored "two prongs" twice and drew a single blade. Loses detail far above 1 MP |
| `edit` | Qwen-Image-Edit 2511 (fp8) with Lightning 4-step LoRA | Apache-2.0 | ~45 s | **Variants and skins:** recolours and material swaps that keep the rest of the image identical | 20 GB model on a 12 GB card, so it offloads. It is the heaviest job, so run it alone |
| `rembg` | BiRefNet | MIT | ~6 s | Cut-outs for decals, icons and 3D input | none seen |
| `upscale` | Real-ESRGAN x4plus | BSD-3-Clause | ~10 s for 1024² to 4096² | Enlarging a good 1 MP image | 4x only. A 4096² PNG is ~14 MB, so downscale to what you need |
| `to3d --engine trellis2` | TRELLIS.2 4B (Microsoft) | MIT | ~130 s | **Default image-to-3D, shippable:** textured GLB, kept the forked blade | Emissive glow is baked into plain colour. The template bakes 4096² textures by default; the tool uses 2048 |
| `to3d --engine pixal3d` | Pixal3D (TencentARC) | MIT (bundled components keep their own licences) | ~150 s | Second opinion, textured | Merged the two prongs into one slab and added a stray fin |
| `to3d --engine hunyuan3d` | Hunyuan3D 2.1 (Tencent) | Tencent Hunyuan 3D 2.1 Community License: **not valid in the EU, UK or South Korea**; over 1M monthly users needs a Tencent licence; outputs may not train other models | ~85 s | **Reference only (owner decision).** Clean untextured shape (33k tris), good silhouette | **Needs a cut-out, cropped input.** Given a full image it rebuilt the background as a flat plate. `comfy_gen.py` now prepares the input itself |
| `music` | ACE-Step 1.5 XL Turbo | MIT (model card: trained on licensed, royalty-free and synthetic data; outputs may be used commercially) | ~47 s for 30 s of audio | Music beds, menu and match themes | Uses about 12 GB with offloading, so run it alone. Vocals are unreliable: use `[Instrumental]` |

## Licence caveats not yet checked

- The **Lightning 4-step LoRA** used by `edit` is listed under Qwen-Image-Edit's Apache-2.0,
  but its own licence page has not been read.
- The **TRELLIS.2 / Pixal3D template** runs DINOv3 (Meta DINOv3 License) and MoGe-2
  (weights licence unchecked) as part of the pipeline. Whether those licences place
  conditions on the output mesh has not been checked.
- **Pixal3D**'s bundled components keep their own licences (its NOTICE file), which have not
  been read.
- **Krea 2** (revenue limit) and **Hunyuan3D 2.1** (region exclusions): **the owner decided
  on 2026-09-18 that their outputs are REFERENCE ONLY**: fine for look-dev, blockouts and
  contact sheets, never shipped as game assets. Shippable images use **Z-Image**, and
  shippable 3D uses **TRELLIS.2** (subject to the two caveats above). This holds until the
  owner reads those licences and says otherwise.

## Installed but not approved

- **MoGe-2** (`moge_2_vitl_normal_fp16`): the weights' licence is unchecked. It is used
  internally by the TRELLIS.2/Pixal3D template; do not use its outputs directly.
- **krea2_darkbrush** LoRA: licence unknown. Keep it off (the tool does).
- **Every API, partner or "requires credits" node or template** (Tripo, Bria, Recraft,
  Magnific, Topaz, "HY 3D: Image to Model", and so on): paid cloud services. Never use them.

## Model files

`comfy_gen.py check` lists each key and any missing file. Models live in
the ComfyUI `Shared Models` folder. Only the owner adds or removes them.
