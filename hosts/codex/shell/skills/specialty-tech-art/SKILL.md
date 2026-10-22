---
name: specialty-tech-art
description: Use when your brief says to load it - art that is code (shaders, materials, palettes and UI themes, procedural meshes and textures, lighting, camera, post-processing), built against a written art direction or proposed as options where none exists, and checked by rendering a frame and looking at it.
---

# Specialty: art that is code

Dispatch: senior-worker at least, never weak-worker; reads the repo profile's **Branches and worktrees**, **Build and test**, **Shared resources** and **Art**.

Read `creative.md` beside this file first. You work inside the art direction where one exists; where it is silent you propose, you do not stall. You never change a look the owner approved, and never edit the direction document.

1. Inputs: the art direction by document and section, the FEEL block, what you own (scenes, shaders, resources, scripts) and what belongs to someone else, and a lock and port if you will render. An open direction is a question for the owner, asked by showing answers.
2. Propose mode: dispatched to propose looks because no direction exists, you propose strongly different options and choose none yourself; this overrides the direction input above.
3. You cannot model, animate or record by hand. Images, edits, cut-outs and upscales come only from the generators the profile's **Art** section names (`creative.md`); meshes are specialty-3d-assets work, sounds and music specialty-sound work. Anything else: stop and report exactly what is needed.
4. A design question (a new look, a skin, an area the direction is silent on): options first (`creative.md`), Step 0 as generated images or quick shader or palette variants, same scene and camera. A fix to something that exists skips to step 5.
5. Read the existing look first - the shaders, materials and theme resources already there - so a new element does not make the game look like two products.
6. Keep art data in resources, not code, where the project does: a palette, theme or material editable without a rebuild beats a constant in a script.
7. Shaders are code: uniforms named for what they mean, per-pixel work kept off anything the whole screen draws, a comment on any non-obvious term. Mind the budgets the direction names (overdraw, transparency, viewports, lights); say what your change costs when you cannot measure it.
8. Headless and server paths must still work: anything that exists only with a window needs a guard; the project's headless runs are the check.
9. Look at what you made: render a frame the way the project allows, under the profile's lock, after the orphan sweep. `<kit>/optional/comfy/render_glb.py` is a kit lock wrapper, run directly (skill machine-lock-run). Read the image back; say what you see (colour, contrast, edges, whether it reads at its real size); compare with the direction's words and with a before image. For anything animated, describe its first, peak and last frames.
10. Cannot render: say so plainly and mark every visual claim unverified. Never describe a result you have not seen.
11. Critique (`creative.md`), deliver the page (options side by side, or before and after for a fix, with the cost), commit (skill builder).

## Never

- Claim an asset exists that you have not seen on disk.

## Report

The page path or URL first. Then the frame you rendered and what you saw, in your own words; your pick and why (an options page); the critique's three weaknesses; what you built, with the files; what you could not verify; the performance cost and how you know it; any asset the work still needs, described so it can be sourced; anything outside your scope.
