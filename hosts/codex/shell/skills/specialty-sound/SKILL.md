---
name: specialty-sound
description: Use when your brief says to load it - designing, building or upgrading game sound effects from procedural synthesis, a free-licence library, text-to-speech for announcer lines, or (music only) the owner's local music model. You cannot hear, so you measure every clip, compare it with the set it joins, and end with a question the owner answers by listening.
---

# Specialty: sound

Dispatch: worker at least, senior-worker for design (a new sound or family with no picked direction), never weak-worker; reads the repo profile's **Branches and worktrees**, **Build and test**, **Shared resources** and **Audio**.

Read `creative.md` (brief block, options first, generation, the limits you keep yourself, delivery, critique) and `measuring.md` (sources, library search, preparing and measuring a clip) beside this file first.

**You cannot hear.** Never call a clip punchy, muddy or satisfying, and never claim an improvement from the strength of its source. Describe a clip only by what you measured or what its source says; the owner is the ear.

1. Inputs: which sound (or family) by the game's id, and the event it must convey ("a hammer killing a player", not "a meaty thud"); the FEEL block; replace or propose only (default propose); which sources are live. No audio direction in the profile's **Audio** section (the default profile has none): stop and say so.
2. Sound brief, before any source, per sound in your scratch note: the event, what the player must understand from it, how often it plays, what it sits near in the mix.
3. A new sound or family with no picked direction: Step 0 (`creative.md`) as 5-10 directions of a few lines each (source; layers - transient, body, tail; envelope), with a rough procedural sketch where cheap. Publish and stop.
4. Read, don't listen: library tags and descriptions, and game-audio design notes, are your references; cite what you used.
5. Three named options per sound, each testing a different answer to the brief: intent, layers and envelope, numbers, a drop-in path, what to listen for; your pick marked with why. Polish only the owner's pick.
6. Take sources in the order `measuring.md` gives, going down only when the step above genuinely cannot, and say why.
7. Prepare and measure every clip, before and after, against the whole existing set (`measuring.md`); look at each spectrogram.
8. Critique from the numbers and spectrograms (`creative.md`), deliver the page (each clip playable, before and after, spectrogram and numbers beside the ones it replaces), commit by explicit path: `python <kit>/tools/commit.py -C <worktree> -m "<subject>" <path> ...`.

## Never

- Claim to have heard something.
- Replace a sound the brief did not say you could.
- Add a runtime dependency for a build-time convenience.
- Land, merge to the main branch or push.

## Report

Page path or URL first. Then the sound and its sound brief; the options, each with intent, source and why a cheaper source could not; measurements before and after as a table; what you saw in the spectrograms; the critique's three weaknesses; **what the owner should listen for, as a question the owner can answer** ("does the impact still read at distance?"); licence and where you recorded it; branch and commit; the gate (the final one `--full` on that tip) with the line that proves each result. For a shortlist, end with "Which of these three, or none?"

Details: `creative.md` and `measuring.md` beside this file; `<kit>/knowledge/asset-generation/MODELS.md`; the repo profile's **Audio** section.
