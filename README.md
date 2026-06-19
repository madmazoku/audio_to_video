# audio_to_video

Generate a stylized music video from prepared song audio, lyrics, lyric timing, and ComfyUI workflows.

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

The runner resolves FFmpeg commands from `PATH` by default. Optional environment overrides are:

```text




```

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

Override it with:

```powershell
python.exe .\aligned_song_video_runner.py --comfy-url http://127.0.0.1:8188
```

The runner also needs to find ComfyUI output files. The default value is configured as a sibling repository path:

```text
G:\Git\ComfyUI\output
```

Override it with:

```powershell
python.exe .\aligned_song_video_runner.py --comfy-output-dir G:\Git\ComfyUI\output
```

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
--comfy-url URL
```

ComfyUI server URL. Default: `http://127.0.0.1:8188`.

```text
--comfy-output-dir PATH
```

ComfyUI output directory used to locate generated image/video files.

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

```text
video_style_N.txt
```

Art direction override for public zero-based range `N`.

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

ASS style override for public zero-based range `N`.

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

Long gaps without lyrics become `instrumental` blocks when they exceed the threshold configured in `config.json`.
Short intro/outro gaps use the same threshold as instrumental pauses. If the intro before the first sung line or the outro after the last sung line is shorter than the instrumental gap threshold, it is merged into the first/last lyric range instead of becoming a separate generated clip.


Every semantic block is rendered through one or more internal subranges:

```text
semantic block -> subrange(s) -> one semantic clip
```

If the block duration is within `max_workflow_seconds`, it has exactly one subrange. That single subrange has empty subrange text, so the prompt does not repeat the full lyrics twice.

If the block is longer than `max_workflow_seconds`, lyric-aware line/word boundaries are used only when they naturally fit under the workflow cap. Any remaining oversized segment is split evenly into near-`recommended_workflow_seconds` pieces, so the result is several medium subranges rather than one oversized subrange plus a tiny remainder.

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

Intro, outro, and instrumental gaps use the same threshold predicate. A silent gap becomes its own semantic block only when its duration is at least `instrumental_gap_threshold`; shorter intro/outro gaps are merged into the nearest lyric range.

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



### Future prompt generation architecture

The planned prompt-generation replacement is documented in [`PROMPT_GENERATION_PLAN.md`](PROMPT_GENERATION_PLAN.md). It uses an effective style contract, semantic planner, prompt writer, critic, and retry loop. `video_style_N.txt` is planned as a full zero-based range style override rather than a diff. No legacy single-pass prompt path is kept in that design.

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
  "clip_duration_tolerance_ratio": 0.05,
  "recommended_workflow_seconds": 12,
  "max_workflow_seconds": 16,
  "instrumental_gap_min_seconds": 8.0,
  "instrumental_gap_min_ratio_of_median_verse": 0.5,
  "local_context_radius": 2,
  "range_visual_preroll_seconds": 0.25,
  "subtitle_line_preroll_seconds": 0.25,
  "min_karaoke_unit_seconds": 0.01,
  "alignment_match_lookahead_words": 5,
  "alignment_match_similarity_threshold": 0.72,
  "alignment_match_warn_ratio": 0.2,
  "alignment_match_max_extra_ratio": 0.5
}
```

`clip_duration_tolerance_ratio` is the allowed relative difference between an unscaled clip file duration and the current range duration before final retime/scale. The same validation is applied to freshly generated and reused clips.

Input override:

```text
input/config.json
```

The instrumental gap threshold is:

```text
max(
  instrumental_gap_min_seconds,
  median_verse_duration * instrumental_gap_min_ratio_of_median_verse
)
```

`local_context_radius` controls how many neighboring verses are passed to the block planner as local context. For normal verse blocks, `2` means up to two previous and two next verses. For intro, the runner passes the first `radius` verses as early-song context. For outro, it passes the last `radius` verses as final-song context.

```text
data/subtitle_styles.ass
```

Default ASS subtitle style.

## 14. Workflows

Workflow files are ComfyUI API workflows. They are versioned with the runner. Do not move them into `input/`.

```text
workflows/planner_visual_prompts_api.json
```

LLM planner workflow. Expected node classes include:

```text
GGUFLoader
LLM_local
Basic data handling: PathSaveStringFile
```

This workflow writes JSON prompt plans to `output/work/plans/`.

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

Then edit `` in `the runner's stable-ts integration`.

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

### Line-aware matching improvements

The alignment matcher now scores candidate lyric-line spans instead of greedily pairing words. It rejects collapsed/low-confidence spans as timing anchors, supports partial lines without dropping lyric text, and performs a final global timing-estimation pass across range boundaries so missing/collapsed ranges do not become near-zero length.

Diagnostic files:

- `work/debug/alignment_diagnostics.json`
- `work/debug/alignment_diagnostics.txt`
- `work/debug/alignment_match_report.json`
- `work/debug/alignment_match_report.txt`

