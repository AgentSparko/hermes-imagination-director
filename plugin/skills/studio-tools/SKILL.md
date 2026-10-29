---
name: studio-tools
description: Drive Imagination Studio jobs by hand.
version: 0.1.0
author: AgentSparko
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [video, image, imagination-studio, local]
    category: creative
---

# Imagination Studio by hand

Stills, clips, long videos, voice, music, sound and exports through the studio's API, one job at a time.

## When to Use

- A single asset or a small change: "make a still of ...", "animate this picture", "add rain sound".
- Checking or fixing what the `autonomous-film` pipeline produced.

## Prerequisites

`studio_state` answers. Paths you pass (`image`, `video`, `path`) are studio-relative as `studio_library` lists them.

## Quick Reference

| Want | `studio_submit` kind and params |
|---|---|
| 4 candidate stills | `scout` `{prompt, count: 4, preset: "2k"}` |
| Clip from a still | `video` `{prompt, seconds: 8, image: "scouts/x/scout_s2.png"}` |
| Still + clip in one | `studio` `{image_prompt, prompt, seconds}` |
| 16 s - 30 min | `long` `{prompt, total_seconds, segment_seconds: 15}` |
| Sound on a clip | `foley` `{video, prompt, keep_sound: true}` / `sfx` `{prompt, seconds}` |
| Join clips | `export` `{clips: [{path}], soundtrack: {take, music_db: -14}}` |

Then `studio_wait` with the job id. `studio_estimate` before anything 2K or long. `studio_voice_take`
and `studio_music_take` for audio. `studio_review` (LFM3-Omni) to look at the result;
`studio_qa` for the studio's own gate.

## Procedure

1. `studio_state` for presets and durations; `studio_compose` when the user gives fields rather than prose.
2. `studio_prompt_check` on a hand-written video prompt; rewrite negations into positive description.
3. Submit, wait, review, report the path (`studio_url/files/<path>`).

## Pitfalls

- Frames are `17n + 5`: ask in seconds and read back the exact seconds the job reports.
- Reference pictures/videos are for single `video` clips only; never with `image` (first frame).
- One render at a time on a Spark; `studio_jobs` shows the queue.

## Verification

The job's `status` is `done` and `outputs` lists a file that `studio_library` also shows.
