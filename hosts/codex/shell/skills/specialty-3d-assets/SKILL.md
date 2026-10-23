---
name: specialty-3d-assets
description: Use when your brief says to load it - building a 3D asset as a Blender Python script (the script is the asset, the exported glTF a build output), cleaning a mesh from the owner's local image-to-3D generators into a game asset, validating the export against budgets and delivering a contact sheet. Props here; characters and rigs go to skill specialty-rigging.
---

# Specialty: scripted 3D assets

Dispatch: senior-worker at least, never weak-worker; reads the repo profile's **Branches and worktrees**, **Build and test**, **Shared resources** and **Art**.

Read `creative.md` beside this file first. Props are yours; characters, rigs and skins are skill `specialty-rigging`. A reader of your contact sheet should say what each object is, who would use it and what it feels like, without being told.

1. Inputs: what to build, the art direction it matches, the FEEL block, its real-world size in the project's units and what it sits next to, where it lives (asset path, the scene or material that uses it). Size missing: ask; scale is the classic scripted-asset failure. No art direction in the profile's **Art** section (the default profile has none): stop and say so.
2. Say once, in your first report, which category the brief is: parametric hard-surface (props, housings, frames, panels, barriers, blocky weapons, architecture) is what scripts do well; faces, cloth, creatures and hand-painted detail they do badly. Then make the best version the method allows.
3. New with a creative choice: options first (`creative.md`), Step 0 as generated images or rough blockouts.
4. Write the script, not the mesh: named constants at the top for what they control, a comment where a number is load-bearing; build from primitives, explicit vertex sections and modifiers. Changing the shape means changing a number. Nobody edits the mesh by hand, you included.
5. A generated mesh (`to3d`) is raw material: you turn it into a game asset - scale, pivot, triangle budget, separate materials for parts that glow or tint - and keep that cleanup as a script like any other asset.
6. Split by material, not convenience: a part tinted, lit or swapped separately is its own object with its own material.
7. Match the engine's export conventions (up and forward axis, scale, origin where the game positions it) so no scene corrects yours. Deterministic output: no random seeds, no timestamps, no interactive state.
8. Render and look straight after the first run: several angles, one proving the feature you rely on (a profile, a bevel, a cutout). Read every image back and say what you see. `<kit>/optional/comfy/render_glb.py` is a kit lock wrapper, run directly (skill machine-lock-run); an engine-side render needs the profile's lock, and is what ships.
9. Validate the exported file re-imported from disk, never the scene that made it: object names, triangle budget, overall dimensions against the given size, one material per object, UVs present; print each measurement beside its budget. The validator passes a right-size wrong-shape mesh, so never report a result you have not looked at.
10. Critique (`creative.md`), deliver the contact sheet (all renders, measurements beside budgets, the shape constants, the rebuild command), commit (skill builder).

## Never

- Hand-edit an exported mesh, or commit one whose script you did not write.
- Sculpt by hand: report what is needed instead.

## Report

The contact sheet path or URL first. Then what you saw in the renders, in your own words, including what you fixed between rounds; your pick and why (an options sheet); the critique's three weaknesses; the files built; the validator table, pass or fail per check; the engine-side render; branch and commit; anything the asset still needs that you cannot make.
