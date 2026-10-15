# Sound: sources, searching, preparing, measuring

The specialty-sound skill's reference file.

## Sources, in order

Go down only when the step above genuinely cannot, and say why.

1. **Procedural** - tune the project's synthesis or write a script. Free, instant, deterministic,
   no licence or attribution. Best for tones, hums, blips, whooshes, loops.
2. **Free-licence library** - real-world material synthesis cannot reach: impacts, crowd,
   footsteps. Prefer public domain over attribution-required. Record every import where the
   project records them, whatever the licence.
3. **Text to speech** - announcer lines only.
4. **Local music generation** - music only (menu, match and victory themes, stingers over a few
   seconds), never sound effects. ACE-Step via `python <kit>/optional/comfy/comfy_gen.py music`
   (MIT; `<kit>/knowledge/asset-generation/MODELS.md`). It re-runs itself under the machine lock
   (kind local-ai) and keeps a private GPU self-lock per run. Short runs while exploring; ask for
   `[Instrumental]`. `check` fails: start and install nothing, say so. Record every session,
   failures included, in the repo's `docs/ops/assets.md`: tags, seconds, seed, what you measured,
   the owner's verdict once you have it.

## Searching a library

- Two or three words per query: terms are ANDed, so a long query returns nothing.
- Filter licence and duration in the same query: a game one-shot is a second or two, and long
  field recordings otherwise drown the results.
- Sort by rating; take several candidates; measure all before choosing. Pick on numbers and
  spectrogram; the owner hears the shortlist.
- Preview files usually suffice for a short effect and need no authorisation beyond the API key.
  Full-quality originals may need an interactive login only the owner can do: try preview first,
  say if it falls short.

## Preparing a clip

Trim silence at both ends; remove DC offset; fade a couple of milliseconds in and out; resample to
the game's rate; normalise to a peak target leaving headroom (the mixer pitch-shifts and overlaps
these). Use the project's tool if it has one, and print what changed: a step that silently altered
a clip looks identical to one that did nothing.

Add no dependencies: a modelling tool's bundled audio library decodes compressed formats without
an install.

## Measuring

Every clip, before and after: duration, peak, average level, clipped samples, DC offset, first and
last sample, trailing silence, largest jump between neighbouring samples.

- **First and last sample at zero**, or the clip clicks. The most common defect.
- **Read the largest jump against what the sound should do.** A tone's own slope is
  `2*pi*f/rate` times its amplitude, so a bright note legitimately reads high; far above that,
  landing mid-sustain rather than on an attack, is a real discontinuity.
- **Compare against the set, not nothing.** Dump every existing sound and put the new one beside
  them: fine alone and 10 dB louder than its neighbours is a defect, and only the comparison shows
  it.
- **Look at the spectrogram:** a click is a vertical stripe; a truncated tail a hard edge; all
  hiss a wash with no structure.
