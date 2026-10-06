# audio_to_video

Generate a stylized music video from prepared song audio, lyrics, lyric timing, and ComfyUI workflows.

Current runner version: **1.7.3** (`aligned_song_video_runner.py --version`).

This repository contains the orchestration code and versioned prompt/workflow templates. Song-specific files live in `input/`, and generated artifacts live in `output/`. The default `.gitignore` excludes `input*/` and `output*/` so the repository can be used as code/config while keeping large media files outside git.
## 1. What this project does

`aligned_song_video_runner.py` builds a music video in these stages:

1. Reads a song project from `--input-dir`.
2. Prepares or refreshes lyric timing in `output/work/alignment/`.
3. Builds semantic timeline blocks: `intro`, `verse`, `instrumental`, `outro`.
4. Builds karaoke subtitles and an early `subtitle_preview.mp4` with debug range/subrange progress bars.
5. Uses ComfyUI LLM/image/video workflows only for blocks that need visual generation.
6. Generates unscaled visual material for each semantic block.
7. Retimes each unscaled block clip to the current timeline duration by scaling video timestamps.
8. Concatenates scaled clips, normalizes final FPS, burns karaoke ASS subtitles, and muxes the full song audio into `final_video.mp4`.

The public unit is always a semantic range/block. Internal subranges are only used to render long blocks. User-facing options such as `--rework N`, `video_style_N.txt`, and `subtitle_styles_N.ass` refer to semantic block numbers, not internal subranges.

## 2. Repository layout

```text
audio_to_video/
  aligned_song_video_runner.py
  planner_vram_probe.py
  track-vram.ps1
  requirements.txt
  run_full.cmd
  run_limit_2.cmd
  run_rebuild_final.cmd
  run_rework_2.cmd

  rules/
    song_context_system.txt
    song_context_user.txt
    block_planner_system.txt
    block_planner_intro.txt
    block_planner_verse.txt
    block_planner_instrumental.txt
    block_planner_outro.txt
    literal_scene_rules.txt

  data/
    config.json
    subtitle_styles.ass

  workflows/
    planner_visual_prompts_api.json
    image_from_prompt_api.json
    video_from_image_api.json

  input/          # song-specific inputs, ignored by git
  output/         # generated artifacts, ignored by git
```

## 3. Installation

### 3.1 Python runner environment

Use Python 3.10+.

```powershell
cd G:\Git\audio_to_video
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` contains only dependencies imported by the runner:

```text
requests
websocket-client
```

`websocket-client` is required. The runner listens to ComfyUI websocket execution/progress events and intentionally has no polling generated.

### 3.2 FFmpeg

FFmpeg and ffprobe must be available from the shell where the runner is started.

Check:

```powershell
ffmpeg -version
ffprobe -version
```

The runner first checks sibling installations under `../ffmpeg/bin/`, then falls back to commands available on `PATH`.

On Windows, install a compiled FFmpeg build, extract it, and add the `bin` directory to `PATH`, for example:

```text
G:\Tools\ffmpeg\bin
```

The runner uses FFmpeg for:

- audio conversion and mixing,
- stream-copy video remuxing and timestamp retiming,
- subclip and final clip concatenation,
- extracting last frames for long-block subrange chaining,
- burning ASS subtitles into the final video.

### 3.3 ComfyUI

ComfyUI must be running before `aligned_song_video_runner.py` starts.

Typical setup:

```powershell
cd G:\Git
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --listen 127.0.0.1 --port 8188
```

The runner default is:

```text
http://127.0.0.1:8188
```

Override `comfy_url` in the input folder's `config.json`. The selected
ComfyUI generators read this connection setting internally.

The runner also needs to find ComfyUI output files. The default value is configured as a sibling repository path:

```text
G:\Git\ComfyUI\output
```

Override `comfy_output_dir` in the input folder's `config.json`. Relative
paths are resolved against the project directory by the generator factory.

Install all ComfyUI custom nodes and models required by the workflow JSON files in `workflows/`. If ComfyUI reports an unknown node type, install the missing custom node into `ComfyUI/custom_nodes/` and restart ComfyUI. If it reports a missing model, place the model file where the workflow expects it.

### 3.4 stable-ts

stable-ts is used by the runner to create `output/work/alignment/alignment.json` from `lyrics.txt` and audio during a normal fresh generation run.

Typical setup:

```powershell
cd G:\Git
git clone https://github.com/jianfch/stable-ts.git
cd stable-ts
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -U stable-ts
```

The runner resolves stable-ts in this order:

```text
../stable-ts/.venv/Scripts/stable-ts.exe
../stable-ts/.venv/bin/stable-ts
stable-ts from PATH
```

So the simplest layouts are either:

```text
G:\Git\audio_to_video
G:\Git\stable-ts
```

or putting `stable-ts` into `PATH`.

During a normal run, the runner writes cleaned sung-only lyrics to:

```text
output/work/audio/alignment_lyrics_clean.txt
output/work/debug/alignment_lyrics_clean.txt
```

and passes that file to stable-ts. Bracket directive lines such as `[Verse]` and semantic separators `***` are not sent to stable-ts.

Language is controlled by `--lyrics-language`, default `en`.

### 3.5 ACE-Step 1.5, optional upstream music source

ACE-Step 1.5 is not required by the runner. It can be used upstream to generate song audio, vocals/instrumental stems, or drafts before creating `input/` files for this repository.

Typical setup starts with the official repo:

```powershell
cd G:\Git
git clone https://github.com/ace-step/ACE-Step-1.5.git
cd ACE-Step-1.5
```

Then follow the installation path for your platform/GPU in the ACE-Step documentation. After generating a song, export or convert files for this project as:

```text
input/audio.mp3
```

or stems:

```text
input/vocals.mp3
input/instrumental.mp3
```

If you use ACE-Step only to create audio, no direct integration with `aligned_song_video_runner.py` is needed.

## 4. Quick start

Prepare `input/`:

```text
input/audio.mp3
input/lyrics.txt
input/video_style.txt
```

`output/work/alignment/alignment.json`, `output/work/alignment/alignment.lrc`, `output/work/alignment/matched_verses.json`, release subtitles, preview subtitles, debug preview subtitles, and `output/subtitle_preview.mp4` are lazy artifacts. If raw alignment is missing, the runner creates it from `input/vocals.*`; if there are no vocals, provide `input/alignment.lrc` for line-level timing without stable-ts. `alignment.lrc` is a standard line-level LRC file: for stable-ts word timing it is generated from the matched lyric lines, and for no-vocals line timing it is copied/normalized from the input LRC source. If `matched_verses.json` exists, the runner reads it and does not rematch lyrics. If subtitle artifacts already exist, the runner reuses them. Use `--refresh-alignment` to invalidate alignment/matching/timeline/subtitle/preview caches after editing lyrics. To refresh subtitle styling only, delete the relevant files under `output/work/subs/` and/or `output/subtitle_preview.mp4`; they will be recreated lazily.

Start ComfyUI in another terminal.

Then run:

```powershell
.\.venv\Scripts\python.exe .\aligned_song_video_runner.py
```

Testing on the first two lyric blocks:

```powershell
.\.venv\Scripts\python.exe .\aligned_song_video_runner.py --limit 2 --output-dir .\output-test
```

Rework one existing semantic block:

```powershell
.\.venv\Scripts\python.exe .\aligned_song_video_runner.py --output-dir .\output-test --rework 2
```

Render only the subtitle preview video and stop before ComfyUI generation:

```powershell
.\.venv\Scripts\python.exe .\aligned_song_video_runner.py --output-dir .\output-test --preview-subtitles-only
```

Refresh alignment from current lyrics/audio, render subtitle preview, and stop before visual generation:

```powershell
.\.venv\Scripts\python.exe .\aligned_song_video_runner.py --output-dir .\output-test --rebuild-final
```

## 5. Runner command-line options

```text
--input-dir PATH
```

Song project folder. Default: `./input`.

```text
--output-dir PATH
```

Generated artifact folder. Default: `./output`.

```text
--limit N
```

Use only the first `N` public zero-based ranges for testing/final assembly. Range numbers are exactly the numbers shown in `subtitle_preview.mp4`: `R000`, `R001`, ... `RNNN`. For example, `--limit 3` selects `R000..R002`; `--limit 27` selects `R000..R026` when the preview shows `/027`. The limit is by semantic range, not by internal subrange.

```text
--rework N [N ...]
```

Regenerate only selected public zero-based ranges and reuse existing unscaled clips for all other selected ranges. Use the same number shown in preview without the `R` prefix: `--rework 3` regenerates `R003`. If a selected range is internally split into subranges, the whole range is regenerated as a new unscaled clip.

```text
--rebuild-final
```

Do not run LLM/image/video generation. Reuse existing unscaled semantic clips from `output/work/clips_unscaled/`, retime them into `output/work/clips/`, reuse or lazily create subtitles, rebuild final concat/mux, and write a fresh manifest.

```text
--preview-subtitles-only
```

Reuse or lazily build full-song preview subtitles, debug-only `work/subs/preview_debug.ass`, and `subtitle_preview.mp4`, then stop before any ComfyUI song-context/planner/image/video work. The preview is always full song, regardless of `--limit`; `--limit` only affects visual generation/final assembly. This is the fastest way to check voice/subtitle alignment, karaoke timing, and semantic range/subrange boundaries.

```text
--refresh-alignment
```

Invalidate `output/work/alignment/` and matching-derived subtitle/preview artifacts, then rebuild them lazily when needed from the current `input/lyrics.txt` and the current alignment source. Use this after editing lyrics to match the actual vocals. With `--preview-subtitles-only`, this lets you validate new karaoke timing and range/subrange boundaries before any visual generation. Existing `clips_unscaled/` files are matched by the same zero-based range id and validated by their actual MP4 duration against the current selected range duration.

```text
--lyrics-language LANG
```

Language code passed to stable-ts. Default: `en`.




## 6. Run modes

### FFmpeg/stable-ts command discovery

FFmpeg, ffprobe, and stable-ts are resolved by sibling repo convention or `PATH`.

Stable-ts resolution order:

    ../stable-ts/.venv/Scripts/stable-ts.exe
    ../stable-ts/.venv/bin/stable-ts
    stable-ts from PATH

FFmpeg/ffprobe resolution order:

    ../ffmpeg/bin/ffmpeg.exe
    ../ffmpeg/bin/ffmpeg
    ffmpeg from PATH

    ../ffmpeg/bin/ffprobe.exe
    ../ffmpeg/bin/ffprobe
    ffprobe from PATH

### Normal run

A normal run means no `--rework` and no `--rebuild-final`.

Behavior:

```text
create missing alignment artifacts if needed
generate or reuse lazy song_context.json only if clip generation is needed
generate all selected semantic block plans/clips unless --rework or --rebuild-final changes the generation list
reuse or lazily create subtitles and subtitle preview
generate final video
write manifest
```

A normal run is a new creative attempt for the selected ranges. To force a completely clean start, delete the output folder first.

### Rework run

`--rework` keeps the output directory and regenerates only requested semantic block numbers as unscaled clips. Rework/rebuild-final do not rebuild alignment unless `--refresh-alignment` is also passed.

Behavior:

```text
keep --output-dir
reuse existing alignment.json
load song_context.json if present, otherwise build it lazily
reuse unscaled semantic clips not listed in --rework
regenerate selected semantic blocks
reuse or lazily create subtitles, then regenerate final video
write manifest
```

If `song_context.json` does not exist and `--rework` needs visual generation, it is built lazily.

### Rebuild-final run

`--rebuild-final` keeps the output directory and does not call ComfyUI for LLM/image/video generation.

Behavior:

```text
keep --output-dir
reuse existing alignment.json
reuse unscaled semantic clips from output/work/clips_unscaled/
reuse or lazily create subtitles, then regenerate final video
write manifest
```

Internal raw subclips are not required for `--rebuild-final`; `clips_unscaled/` is required. `--rebuild-final` validates each selected unscaled clip by actual file duration against the current range duration, then retimes/scales each clip into `work/clips/`. If a clip is missing or too far outside the configured duration tolerance, regenerate that range explicitly with `--rework` or increase `clip_duration_tolerance_ratio` in `input/config.json`.

## 7. Input folder

Default input folder:

```text
input/
```

Override with:

```powershell
python.exe .\aligned_song_video_runner.py --input-dir .\my-song-input
```

### 7.1 Required files

```text
video_style.txt
```

Art direction for the whole video. This is prompt style only. Technical video_width/video_height/video_fps live in `config.json`.

```text
vocals.mp3
```

Optional but recommended for word-level timing. If `vocals.*` exists and `output/work/alignment/alignment.json` is missing, the runner uses stable-ts to create it. Use `--refresh-alignment` after editing lyrics to invalidate alignment-derived caches.

```text
alignment.lrc
```

Line-level timing input for the no-vocals mode. If there is no `vocals.*`, provide `input/alignment.lrc`; stable-ts is not run in this mode. LRC is matched against `lyrics.txt` semantic ranges. `[metadata]` and `***` LRC lines are ignored for subtitles and used only as structure/boundary hints.


```text
lyrics.txt
```

Lyrics split into semantic verses/ranges with `***` separators:

```text
[Verse]
[Vocal: alto female]
First lyric line
Second lyric line
***
[Chorus]
Next lyric line
```

Separators may carry an absolute manual timestamp. The timestamp is always
absolute from the beginning of the source audio. Four modes are supported:

```text
*** @  02:54.300   # exact: use 02:54.300 exactly
*** #< 02:54.300   # soft: snap to the nearest valid lyric boundary at/before the requested time
*** #> 02:54.300   # soft: snap to the nearest valid lyric boundary at/after the requested time
*** #  02:54.300   # backward-compatible alias for #<
```

For `#<` and `#>`, the candidate must be within
`manual_boundary_snap_max_seconds` (10 seconds by default). If no candidate is
available in the requested direction inside that radius, the runner keeps the
requested timestamp exactly and logs a warning. `@` never snaps.

Example:

```text
[Outro]
Last sung line
*** #> 02:54.300
[Instrumental]
*** @ 03:20.000
[End]
```

`*** @`, `*** #<`, and `*** #>` set the shared boundary between two public
semantic ranges. Use `--refresh-alignment --preview-subtitles-only` after
changing manual boundaries; once the preview is accepted, normal runs reuse the
cached matched alignment.

Lines in square brackets are metadata/directives, not sung lyrics. They:

- do not participate in matching,
- do not appear in subtitles,
- are attached to the semantic range as `bracket_directives`,
- are passed to the planner below actual lyric facts in priority.

If `lyrics.txt` is missing, the runner tries to use text embedded in `alignment.json`, when available.

### 7.3 Optional project overrides

```text
config.json
```

Overrides defaults from `data/config.json`.
Objects are merged recursively; lists are replaced, so an explicit empty
`loras` list removes the template's adapters.

#### Generation templates (phase 1)

`data/model_templates.json` contains named image and video recipes. Select
them in `data/config.json` or in the input folder's `config.json`:

```json
{
  "image_generation": {
    "template": "flux2_dev_current"
  },
  "video_generation": {
    "template": "ltx23_current"
  },
  "llm_generation": {
    "template": "qwen25_14b_current"
  }
}
```

The catalog is indexed by template name under `image_generation`,
`video_generation`, and `llm_generation`. Each entry contains `type`, `workflow`, model files,
`loras`, and sampling parameters. The factory selects the generator by
`type`; FLUX2 and LTX use ComfyUI. Phase 1 supports FLUX2 Dev images and
LTX 2.3 image-to-video, and the existing llama-cpp Qwen 2.5 14B planner.
Other generator types fail before generation. The LLM recipe configures the
checkpoint, `n_ctx`, `max_tokens`, and the `parameters` object (temperature,
top-k/top-p, penalties and other sampling settings).

Available image recipes:

| Template | Recipe |
| --- | --- |
| `flux2_dev_current` | Existing FLUX2 Dev setup, Small Decoder, 30 steps, guidance 3.5, no LoRA |
| `flux2_dev_full_vae` | Same setup with the full `flux2-vae.safetensors` |
| `flux2_dev_turbo` | FLUX2 Dev with Turbo LoRA, eight steps and its explicit sigma schedule |

`ltx23_current` preserves the existing two-pass LTX setup and explicitly lists
its distilled acceleration LoRA at weight 0.7. Both passes receive the ordered
LoRA chain. These default recipes produce the same patched graphs as the
previous runner. The default setup and the alternative Turbo/FP8 selection
were exercised successfully in local user runs. Full VAE and unaccelerated
LTX remain comparison recipes requiring generation validation.

Available video recipes (all handled by `LtxGenerator`):

| Template | Recipe |
| --- | --- |
| `ltx23_current` | Existing LTX 2.3 Dev checkpoint and distilled acceleration LoRA |
| `ltx23_fp8` | Same recipe with the installed LTX 2.3 Dev FP8 checkpoint |
| `ltx23_unaccelerated` | Dev checkpoint without LoRA, ordinary sampling and longer refinement; experimental |

All FLUX2 recipes use `Flux2Generator`, including the Turbo schedule. Selecting
a recipe does not change the command or input file layout. Installed files and
graph wiring can be checked without generation; alternative recipes still
need visual validation on the target GPU.

The selected template is loaded first; other fields in the same configuration
object override it. Global and input configuration objects merge recursively,
so the input file can change only `steps` and inherit the template selection:

```json
{
  "image_generation": {
    "template": "flux2_dev_current",
    "checkpoint": "flux2_dev_fp8mixed.safetensors",
    "steps": 25,
    "loras": [
      { "name": "compatible_flux2_style.safetensors", "weight": 0.7 }
    ]
  },
  "video_generation": {
    "template": "ltx23_unaccelerated",
    "steps": 35,
    "refine_steps": 15
  }
}
```

Weights must be compatible with the selected model family. LoRA order is
preserved. There is no fixed number of LoRA slots: generators insert nodes
and reconnect explicitly declared inputs. Node IDs and fields live in the
generator classes in `core/generator.py`, not in a separate mapping file. Graph
bindings and links are checked before a job is submitted.

Reusable implementation lives under `core/`; executable entry points remain
separate. `core/generator.py` contains the generator classes, factory,
requests/results, configuration merging and shared ComfyUI interface.
`Generator` declares only `generate()` and `metadata()`; it has no workflow,
connection, or model-loading implementation. `ComfyUIGenerator` owns graph
loading, binding checks, LoRA chains, and workflow configuration. A future
AUTOMATIC1111 implementation can implement `Generator` independently.
`generate(request)` accepts one immutable, keyword-only request containing
the prompt, seed, output/debug locations and runtime parameters. `ImageRequest`,
`VideoRequest` and `LlmRequest` inherit independently from `GenerationRequest`.
Optional dimensions and LLM token limits inherit generator settings when omitted.
Video length is specified only by the required `seconds` field; optional `fps`
inherits the recipe. The LTX workflow converts seconds to a valid frame count
using its existing `8*k+1` rounding rule.
The current small generator subsystem stays in one module; separate modules
can be introduced when independent subsystems warrant them.
The factory configures a service client owned by the generator. Connections,
workflow submission, input upload, progress, memory cleanup and result lookup
are implemented in `core/comfyui.py` and called by generators. The runner
passes local files and requests, not server-specific callbacks or URLs.
The LLM generator returns response text; song-context construction, prompt
policy and semantic plan validation remain in the runner. Connection settings
are configured only through `config.json`.


An omitted `loras` field in a catalog entry means no adapters. Omitting a
local override inherits the recipe's list; explicitly setting `[]` removes
it. The `role: "acceleration"` marker identifies a recipe's speed adapter.
FLUX2 Turbo requires this adapter and its schedule; for no LoRA select a
standard recipe. LTX recipes explicitly select `sampling: "distilled"` or
`sampling: "full"`. Removing the required accelerator from a distilled
recipe is an error; select `ltx23_unaccelerated` for no LoRA. Its candidate
settings are 30 steps / CFG 3 and a 12-step refinement. Override them directly
in `video_generation` using `steps`, `cfg`, `refine_steps`, `refine_cfg`,
`sampler`, and `refine_sampler`. Unaccelerated generation is slower and needs
visual validation. No alternate sampling recipe is inferred by the code.

The resolved config requires `image_generation.template` and
`video_generation.template`, plus `llm_generation.template`; defaults are
defined only in `data/config.json`.
The runner does not infer a template when these selections are missing.
Workflow files must use the current bindings and contain no built-in LoRA
nodes; generators add all adapters from the recipe.

Global `video_width`, `video_height`, and `video_fps` override recipe defaults.
An explicit image override may set a different `width`/`height`; the existing
video workflow then resizes that generated image. Dimensions must be positive
integers divisible by eight. Video dimensions/fps must match the global video
settings; change those global settings to change the video output.

Supplied `start_image_N.*` files still bypass image generation entirely.
Continuation still uses the previous final frame in phase 1; video-prefix
overlap is planned for phase 2.

Debug output records resolved settings and graph signatures in
`generation_settings.json`, plus per-generated-part `image_generator.json`
and `video_generator.json`. Unscaled range clips also have a
`.generation.json` sidecar. Reuse keeps the existing clip: a warning identifies
changed generator settings. Reuse requires this metadata sidecar; old clips
without it must be regenerated. Use `--rework N` to apply new settings to an
existing range; `--rebuild-final` does not regenerate it.

Offline configuration and graph checks:

```powershell
python -m unittest discover -s tests -v
```

```text
video_style_N.txt
```

Art direction override for public **zero-based** range `N`. The number is the
same `RNNN` id shown in `subtitle_preview.mp4`, without the `R` prefix. For
example, `R000` uses `video_style_0.txt`. Adding a new semantic range at the
start of `lyrics.txt` shifts all following range ids by +1.

Optional `start_image_N.png` (also `.jpg`, `.jpeg`, `.webp`) supplies the first
frame of the same zero-based range `N`. The runner copies it instead of running
txt2img; absent files keep normal image generation. Padded ids are accepted,
but multiple files with the same numeric id are an error, even across extensions.
Images are copied without resizing or modifying the originals. The existing video
workflow handles scaling with Lanczos and central cropping to the configured video
size; it may trim edges when aspect ratios differ. Later subranges still use the
preceding video's last frame. Existing clip reuse is unchanged: use `--rework N`
to regenerate an already generated range after adding or changing its image.

Examples:

```text
video_style_0.txt      # R000
video_style_1.txt      # R001
video_style_2.txt      # R002
video_style_001.txt    # also accepted for R001
```

Duplicate numeric ids are an error:

```text
video_style_1.txt + video_style_001.txt
```

```text
subtitle_styles.ass
```

Song-level ASS style override.

```text
subtitle_styles_N.ass
```

ASS style override for public **zero-based** range `N`, using the same `RNNN`
id shown in `subtitle_preview.mp4`.

Examples:

```text
subtitle_styles_0.ass
subtitle_styles_1.ass
subtitle_styles_001.ass
```

Duplicate numeric ids are an error.

## 8. Output folder

Default output folder:

```text
output/
```

Important files:

```text
output/subtitle_preview.mp4
output/final_video.mp4
output/manifest.json
```

Work folder:

```text
output/work/
  audio/
    full_mix.wav
    final_audio.wav

  clips_unscaled/
    clip_000.mp4
    clip_001.mp4
    ...

  clips/
    clip_000.mp4
    clip_001.mp4
    ...

  subclips_raw/
    block_NNN/
      part_001.mp4
      part_002.mp4

  subclips_video/
    block_NNN/
      part_001.mp4
      part_002.mp4

  frames/
    block_NNN/
      part_001_start.png
      part_001_last.png

  plans/
    song_context.json
    plan_NNN.json
    plan_NNN_part_MMM.json

  subs/
    karaoke.ass
    preview_debug.ass

  video/
    video_only.mp4

  debug/
    parsed_verses_all.json
    alignment_match_report.txt
    alignment_match_report.json
    alignment_diagnostics.txt
    alignment_diagnostics.json
    alignment_ignored_meta_words.json
    alignment_lyrics_clean.txt
    timeline_blocks.json
    config_used.json
    video_style_map.json
    subtitle_styles_map.json
    timing_report.json
    video_generation_NNN.json
    clip_validation_report.json
    clip_scaling_report.json

    ranges/
      range_NNN/
        range_text.txt
        range_directives.txt
        range_context.json
        part_MMM/
          subrange_text.txt
          subrange_context.json
          planner_context.json
          planner_request.txt
          planner_request.json
          planner_response.txt
          planner_response.json
          planner_parsed.json
          planner_result.json
          planner_history.json
          image_patched.json
          image_history.json
          video_patched.json
          video_history.json
          video_generation.json
```

`output/work/clips_unscaled/` contains semantic range visual material as generated/assembled, without duration fitting. `output/work/clips/` contains timestamp-retimed copies fitted to the current timeline and used by final assembly. Final assembly validates each unscaled clip by actual MP4 duration against the selected range duration using `clip_duration_tolerance_ratio`. `output/work/subclips_raw/`, `output/work/subclips_video/`, and `output/work/frames/` are internal artifacts for long-range rendering.

## 9. Semantic blocks and internal subranges

The runner builds semantic timeline blocks from parsed lyric timing:

```text
intro
verse
instrumental
outro
```

The semantic block list follows `lyrics.txt` exactly: every segment separated by
`***` becomes one public range. A segment without lyric lines fills the available
gap between its neighboring lyric ranges (or the corresponding audio edge) and
becomes `intro`, `instrumental`, or `outro` according to its position. Consecutive
empty segments divide their available gap evenly unless manual `*** @`, `*** #<`, or `*** #>`
boundaries override those edges.


Every semantic block is rendered through one or more internal subranges:

```text
semantic block -> subrange(s) -> one semantic clip
```

If the block duration is within `max_workflow_seconds`, it has exactly one subrange. That single subrange has empty subrange text, so the prompt does not repeat the full lyrics twice.

If the block is longer than `max_workflow_seconds`, lyric-aware line/word boundaries are used only when they naturally fit under the workflow cap. Any remaining oversized segment is split evenly into near-`recommended_workflow_seconds` pieces, so the result is several medium subranges rather than one oversized subrange plus a tiny remainder.

`---` remains a preferred internal divider after the preceding lyric line. It
can also carry an absolute timestamp with the same exact/directional snap syntax
as `***`:

```text
First lyric line
--- #< 01:42.500
Second lyric line
--- #> 01:48.000
Third lyric line
--- @ 01:55.250
Fourth lyric line
```

The modes are:

- `--- @ TIME` — exact boundary at `TIME`; no snapping.
- `--- #< TIME` — snap to the nearest valid lyric line/word boundary at or before `TIME`.
- `--- #> TIME` — snap to the nearest valid lyric line/word boundary at or after `TIME`.
- `--- # TIME` — backward-compatible alias for `--- #< TIME`.

Directional snapping is limited by `manual_boundary_snap_max_seconds`. If no
candidate exists in the requested direction within that radius, the exact
requested time is used and a warning is logged.

Timed internal boundaries are locked: later automatic line/word/even splitting
may add boundaries inside either side, but the short-subrange merge cannot
remove or cross them. A timed boundary that creates a part shorter than
`min_workflow_seconds` is rejected before visual generation.

A useful pattern for silence between lyric lines is to put the requested time
somewhere inside the silent gap and choose its side explicitly. For example,
`--- #< 00:22.000` attaches the boundary to the lyric edge before the gap,
while `--- #> 00:22.000` attaches it to the lyric edge after the gap, provided
each candidate is within the configured snap radius.

Rendering flow:

```text
first subrange:
  image_from_prompt_api.json -> start image
  video_from_image_api.json -> subclip

next subrange:
  extract previous subclip last frame -> start image
  video_from_image_api.json -> subclip

after all subranges:
  concatenate video-only subclips -> clips_unscaled semantic clip
  later final assembly retimes clips_unscaled -> clips by timestamp scaling
```

The next semantic block starts from a fresh generated image. Last-frame chaining is only inside one semantic block.

### Visual preroll and subtitle lead-in

For lyric ranges, `range_visual_preroll_seconds` lets the semantic clip start slightly before the first sung word, but only by taking time from lyric-free gap before that range. It never overlaps a previous sung lyric. This means boundaries such as `intro -> verse` or `instrumental -> verse` can show the new verse scene before the first word is sung.

`subtitle_line_preroll_seconds` makes a subtitle line visible slightly before its first word. The karaoke timing itself is not shifted. The ASS file uses a transparent timed spacer for the lead-in/gaps, so the first visible word is not highlighted before its real word timestamp.

Word-level karaoke is gap-aware: gaps between word timestamps are preserved instead of compressing all words together. Silent gaps inside the karaoke overlay are consumed without making the next word highlight early.

Subtitle artifacts are lazy. `work/subs/karaoke.ass` is the full-song release subtitle file used by final rendering, `work/subs/preview_karaoke.ass` is the full-song preview karaoke subtitle file, `work/subs/preview_debug.ass` is the debug overlay, and `subtitle_preview.mp4` is a black-screen full-song preview video with audio, preview subtitles, and debug overlay. Existing files are reused until deleted or invalidated by `--refresh-alignment`. The preview is independent of `--limit`; a limited final render naturally burns only release subtitle events that fall inside the limited video/audio duration. The debug overlay is vector-drawn ASS graphics with three progress bars: full song progress with range/subrange boundary ticks, current range progress, and current subrange progress. Range labels use compact zero-based `Rnumber/count` labels without the range kind. `count` is always the total full-song range count, independent of `--limit`; this total is the maximum useful value for `--limit`. For example, `R000/027` through `R026/027` means `--limit 27` selects the whole song. Subranges use `Snumber/count`. `preview_debug.ass` is never used for the final video. This happens before song-context LLM, planner LLM, image generation, or video generation. Use `--preview-subtitles-only` to stop after this file is created.

Silent gaps do not create public ranges implicitly. Add an empty `***` segment
with an optional bracket directive such as `[Instrumental]` when a gap must be a
separate semantic block.

Debug for ranges/subranges is written under `output/work/debug/ranges/range_NNN/`, with one folder per semantic range and one `part_MMM/` folder per internal subrange.

## 10. Prompt priority

The planner receives a structured context. Effective priority:

```text
VISUAL STYLE
GLOBAL SONG CONTEXT
LOCAL CONTEXT
BRACKET DIRECTIVES
FULL SEMANTIC RANGE LYRICS / RANGE TEXT
CURRENT SUBRANGE TEXT, when present
```

Visual style is the mandatory style contract. Current subrange text is the highest factual priority when a semantic block is split. Bracket directives are metadata and must not be rendered as visible text.



### Action-oriented video prompts

The default block planner rules are tuned for LTXV image-to-video. The LLM is asked to write every `video_prompt` as a short non-looping event arc instead of an idle animated illustration. The image prompt defines the starting keyframe; the video prompt must describe what happens after that frame.

Every generated video prompt should contain a clear temporal structure:

```text
At the start...
Then...
By the end...
```

The event should include character action, object interaction, or environmental transformation, plus a visible consequence in the final frame. Camera drift, smoke, particles, hair movement, breathing, flickering light, and rhythmic swaying may support the shot, but they must not be the main motion.

The runner does not append or rewrite prompt fragments in code. Action policy lives in `rules/*.txt`; the planner JSON schema is unchanged.

The default `recommended_workflow_seconds` and `max_workflow_seconds` use the existing config keys and are intentionally shorter for LTXV action shots. Shorter subranges are more likely to produce visible action instead of slow idle motion.

## 11. Subtitle styling

Default subtitle style:

```text
data/subtitle_styles.ass
```

The style file must include an ASS `[V4+ Styles]` section and a style named:

```text
line
```

The runner renames styles internally:

```text
default_line
clip_N_line
```

Only one style is needed per song/block. Karaoke highlighting is generated by ASS override tags in the subtitle events, not by switching between unsung/sung styles.

Resolution order for block `N`:

```text
input/subtitle_styles_N.ass
input/subtitle_styles.ass
data/subtitle_styles.ass
```

Subtitle styles refer to semantic block numbers. Internal subranges do not affect subtitle style selection. Subtitles are burned only in the final mux pass.

## 12. Rules

Rules are plain text templates in `rules/`. They are versioned with the runner and workflows.

```text
song_context_system.txt
```

System prompt for global song context generation.

```text
song_context_user.txt
```

User prompt template for global song context. It receives song-level lyrics and visual style.

```text
block_planner_system.txt
```

System prompt for per-block visual prompt generation.

```text
block_planner_intro.txt
```

Planner template for intro blocks before the first lyric.

```text
block_planner_verse.txt
```

Planner template for lyric/verse semantic blocks. It knows about full semantic range text and highest-priority current subrange text.

```text
block_planner_instrumental.txt
```

Planner template for instrumental gaps. It creates a visual musical interlude without lyrics.

```text
block_planner_outro.txt
```

Planner template for outro blocks after the final lyric.

```text
literal_scene_rules.txt
```

Shared rules that keep the visual plan grounded in current lyrics/subrange facts and prevent visible text.

Edit rules when you want to change prompt behavior. Do not put rules in `input/`; they are part of the algorithm, not song data.

## 13. Data defaults

Defaults live in `data/`.

```text
data/config.json
```

Default technical/timeline configuration:

```json
{
  "comfy_url": "http://127.0.0.1:8188",
  "comfy_output_dir": "..\\ComfyUI\\output",
  "video_width": 1280,
  "video_height": 720,
  "video_fps": 24,
  "clip_duration_tolerance_ratio": 0.15,
  "min_workflow_seconds": 1.0,
  "recommended_workflow_seconds": 12,
  "max_workflow_seconds": 16,
  "manual_boundary_snap_max_seconds": 10.0,
  "local_context_radius": 2,
  "range_visual_preroll_seconds": 0.25,
  "subtitle_line_preroll_seconds": 0.25,
  "min_karaoke_unit_seconds": 0.01,
  "alignment_match_lookahead_words": 5,
  "alignment_match_similarity_threshold": 0.72,
  "alignment_match_warn_ratio": 0.2,
  "alignment_match_max_extra_ratio": 0.5,
  "image_generation": {
    "template": "flux2_dev_current"
  },
  "video_generation": {
    "template": "ltx23_current"
  },
  "llm_generation": {
    "template": "qwen25_14b_current"
  }
}
```

`clip_duration_tolerance_ratio` is the allowed relative difference between an unscaled clip file duration and the current range duration before final retime/scale. The same validation is applied to freshly generated and reused clips.

Input override:

```text
input/config.json
```

`local_context_radius` controls how many neighboring verses are passed to the block planner as local context. For normal verse blocks, `2` means up to two previous and two next verses. For intro, the runner passes the first `radius` verses as early-song context. For outro, it passes the last `radius` verses as final-song context.

The LLM recipe's `n_ctx` sets the context window and `max_tokens` sets the
response limit. Sampling controls are read from the recipe's `parameters`.
Override these settings in `input/config.json`, for example:

```json
{
  "llm_generation": {
    "template": "qwen25_14b_current",
    "n_ctx": 32768,
    "max_tokens": 4096,
    "parameters": {
      "temperature": 0.18
    }
  }
}
```

```text
data/subtitle_styles.ass
```

Default ASS subtitle style.

## 14. Workflows

Workflow files are ComfyUI API workflows. They are versioned with the runner. Do not move them into `input/`.

```text
workflows/planner_visual_prompts_api.json
```

The current llama-cpp-vlm planner uses:

```text
llama_cpp_model_loader
llama_cpp_parameters
llama_cpp_instruct_adv
llama_cpp_unload_model
Basic data handling: PathSaveStringFile
```

The runner replaces the workflow's system and request prompt placeholders from `rules/` and the current planning request, then writes JSON prompt plans to `output/work/plans/`. Context and output limits come from config; sampler settings remain in the workflow.

```text
workflows/image_from_prompt_api.json
```

Image generation workflow. Expected node classes include:

```text
CLIPLoader
CLIPTextEncode
UNETLoader
VAELoader
RandomNoise
Flux2Scheduler
SamplerCustomAdvanced
VAEDecode
SaveImage
```

The runner patches image prompt, width, height, seed, and output prefix.

```text
workflows/video_from_image_api.json
```

Image-to-video workflow. Expected node classes include:

```text
LoadImage
LTXVImgToVideoInplace
LTXVConditioning
LTXVPreprocess
LTXVScheduler
EmptyLTXVLatentVideo
LTXVConcatAVLatent
CreateVideo
SaveVideo
```

The runner patches start image, video prompt, negative prompt, float duration seconds, fps, width, height, seeds, and output prefix. The workflow converts duration seconds and fps to an LTXV-valid frame count.

The final video latent is decoded by the workflow's standard `VAEDecode` node before `CreateVideo`/`SaveVideo` output handling.

### 14.1 VRAM diagnostics

`planner_vram_probe.py` repeatedly runs the planner workflow while recording model-load and cleanup behavior. Its default artifacts are written under `output/work/vram_probe/planner/`.

```powershell
python.exe .\planner_vram_probe.py
```

`track-vram.ps1` is a lightweight continuous `nvidia-smi` logger. It writes `vram_log.csv` beside the script until interrupted:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\track-vram.ps1
```

## 15. External repositories and tools

### FFmpeg

Purpose in this project:

```text
audio conversion
audio mixing
stream-copy video remuxing and timestamp retiming
frame extraction
concat
subtitle burn-in
```

Where to get it:

```text
https://ffmpeg.org/
https://ffmpeg.org/download.html
```

On Windows, use one of the compiled builds linked from the official FFmpeg download page, extract it, and add `bin` to `PATH`.

### ComfyUI

Purpose in this project:

```text
LLM prompt planning
image generation
image-to-video generation
```

Where to get it:

```text
https://github.com/comfyanonymous/ComfyUI
```

Typical install:

```powershell
cd G:\Git
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --listen 127.0.0.1 --port 8188
```

After installation, install the custom nodes and models required by this repository's workflows.

### stable-ts

Purpose in this project:

```text
optional creation of alignment.json
```

Where to get it:

```text
https://github.com/jianfch/stable-ts
```

Typical install:

```powershell
cd G:\Git
git clone https://github.com/jianfch/stable-ts.git
cd stable-ts
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -U stable-ts
```

The runner discovers a sibling stable-ts virtual environment automatically, or falls back to `stable-ts` on `PATH`.

### ACE-Step 1.5

Purpose in this project:

```text
optional upstream music/audio generation
```

Where to get it:

```text
https://github.com/ace-step/ACE-Step-1.5
```

Typical start:

```powershell
cd G:\Git
git clone https://github.com/ace-step/ACE-Step-1.5.git
cd ACE-Step-1.5
```

Follow the official install guide for your platform and GPU. Export generated audio as `input/audio.mp3` or stems as `input/vocals.mp3` and `input/instrumental.mp3`.

## 16. Troubleshooting

### ComfyUI unknown node

Install the missing custom node into `ComfyUI/custom_nodes/`, restart ComfyUI, and load the workflow manually once to confirm it works.

### ComfyUI missing model

Open the workflow in ComfyUI and check the failing loader node. Place the model in the expected ComfyUI model folder or update the workflow JSON.

### FFmpeg not found

Make sure `ffmpeg -version` and `ffprobe -version` work in the same terminal.

### `--rework` needs song context but it is missing

The runner builds `output/work/plans/song_context.json` lazily when clip generation needs it. Rebuild-final and preview-only runs do not read or build song context.

### How do I force a completely fresh output?

Delete the output folder before running. The runner no longer treats the absence of `--rework` as permission to delete `--output-dir`; it regenerates the selected ranges and overwrites their generated artifacts.


### alignment contains bracket directives or `***`

Regenerate alignment by running a normal fresh generation. New alignment should be generated from cleaned sung-only lyrics. The runner can ignore metadata-looking words from old alignment files, but clean alignment is more reliable.

### alignment diagnostics and line-aware matching

For `alignment.json`, the runner uses a lyrics-driven line-aware matcher. `lyrics.txt` remains the text truth, and stable-ts words are treated as timing evidence. The matcher walks the song monotonically from start to end, matches one lyric line at a time, allows partial line matches, and reports low-confidence timing instead of silently accepting collapsed timestamps. The matched result is saved as `output/work/alignment/matched_verses.json`; later runs reuse it until the file is removed or `--refresh-alignment` invalidates the alignment cache.

Check:

```text
output/work/debug/alignment_match_report.txt
output/work/debug/alignment_match_report.json
output/work/debug/alignment_diagnostics.txt
output/work/debug/alignment_diagnostics.json
```

Important statuses include:

```text
GOOD
PARTIAL_PREFIX_MISSING
PARTIAL_SUFFIX_MISSING
PARTIAL_INTERNAL_GAP
MISSING
COLLAPSED
LOW_CONFIDENCE
HAS_MISMATCH
```

The full lyrics are still written to subtitles even when stable-ts misses part of a line. Missing, partial, or collapsed words are kept in the subtitle text, but unreliable timing is marked internally and estimated from neighboring reliable lines so it does not distort range/subrange boundaries or the following lines.

The alignment matcher scores candidate lyric-line spans instead of greedily pairing words. It rejects collapsed/low-confidence spans as timing anchors, supports partial lines without dropping lyric text, and performs a final global timing-estimation pass across range boundaries so missing/collapsed ranges do not become near-zero length.

Diagnostic files:

- `work/debug/alignment_diagnostics.json`
- `work/debug/alignment_diagnostics.txt`
- `work/debug/alignment_match_report.json`
- `work/debug/alignment_match_report.txt`
