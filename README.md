# audio_to_video

Generate a stylized music video from prepared song audio, lyrics, lyric timing, and ComfyUI workflows.

This repository contains the orchestration code and versioned prompt/workflow templates. Song-specific files live in `input/`, and generated artifacts live in `output/`. The default `.gitignore` excludes `input*/` and `output*/` so the repository can be used as code/config while keeping large media files outside git.

## 1. What this project does

`aligned_song_video_runner.py` builds a music video in these stages:

1. Reads a song project from `--input-dir`.
2. Parses lyric timing from `alignment.json` or `alignment.lrc`.
3. Builds semantic timeline blocks: `intro`, `verse`, `instrumental`, `outro`.
4. Uses a ComfyUI LLM workflow to create visual prompts for each block.
5. Renders each semantic block as one final clip.
6. Splits long semantic blocks into internal subranges when needed.
7. Uses ComfyUI image/video workflows to generate clips.
8. Concatenates generated clips, prepares audio, and burns karaoke ASS subtitles into `final_video.mp4`.

The public unit is always a semantic range/block. Internal subranges are only used to render long blocks. User-facing options such as `--rework N`, `video_style_N.txt`, and `subtitle_styles_N.ass` refer to semantic block numbers, not internal subranges.

## 2. Repository layout

```text
audio_to_video/
  aligned_song_video_runner.py
  align_stable_ts.cmd
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

`websocket-client` is required. The runner listens to ComfyUI websocket execution/progress events and intentionally has no polling fallback.

### 3.2 FFmpeg

FFmpeg and ffprobe must be available from the shell where the runner is started.

Check:

```powershell
ffmpeg -version
ffprobe -version
```

On Windows, install a compiled FFmpeg build, extract it, and add the `bin` directory to `PATH`, for example:

```text
G:\Tools\ffmpeg\bin
```

The runner uses FFmpeg for:

- audio conversion and mixing,
- video trim/pad,
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

The runner also needs to find ComfyUI output files. The default value is currently set in the script:

```text
G:\Git\ComfyUI\output
```

Override it with:

```powershell
python.exe .\aligned_song_video_runner.py --comfy-output-dir G:\Git\ComfyUI\output
```

Install all ComfyUI custom nodes and models required by the workflow JSON files in `workflows/`. If ComfyUI reports an unknown node type, install the missing custom node into `ComfyUI/custom_nodes/` and restart ComfyUI. If it reports a missing model, place the model file where the workflow expects it.

### 3.4 stable-ts, optional alignment tool

stable-ts is not used by the video runner directly. It is an optional external tool for creating `input/alignment.json` from `lyrics.txt` and audio.

Typical setup:

```powershell
cd G:\Git
git clone https://github.com/jianfch/stable-ts.git
cd stable-ts
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -U stable-ts
```

Edit `STABLE_TS_EXE` inside `align_stable_ts.cmd` if your stable-ts path is different.

Run alignment:

```cmd
align_stable_ts.cmd
align_stable_ts.cmd .\input en
align_stable_ts.cmd .\input ru
```

The wrapper reads from the input folder:

```text
lyrics.txt
vocals.mp3 OR audio.mp3
```

and writes:

```text
alignment.json
```

Language is a command argument. Default language is `en`.

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
input/alignment.json
input/video_style.txt
```

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

Rebuild final video without regenerating ComfyUI clips:

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

Use only the first `N` parsed lyric verses for testing. If `N` equals the total verse count, it behaves like a full-song run. The limit is by semantic lyric verse, not by internal subrange.

```text
--rework N [N ...]
```

Regenerate only selected semantic blocks and reuse existing clips for all others. `--rework` expects existing output from a previous normal run. If a selected semantic block is internally split into subranges, the whole semantic block is regenerated.

```text
--rebuild-final
```

Do not run LLM/image/video generation. Reuse existing semantic clips from `output/work/clips/`, regenerate subtitles/final concat/mux, and write a fresh manifest.

```text
--comfy-url URL
```

ComfyUI server URL. Default: `http://127.0.0.1:8188`.

```text
--comfy-output-dir PATH
```

ComfyUI output directory used to locate generated image/video files.

```text
--ffmpeg PATH
--ffprobe PATH
```

Override FFmpeg/ffprobe commands.

## 6. Run modes

### Normal run

A normal run means no `--rework` and no `--rebuild-final`.

Behavior:

```text
clean --output-dir
generate song context
generate all selected semantic block plans
generate all selected semantic clips
generate subtitles
generate final video
write manifest
```

A normal run is treated as a new creative attempt. Existing files in `--output-dir` are removed first.

### Rework run

`--rework` keeps the output directory and regenerates only requested semantic block numbers.

Behavior:

```text
keep --output-dir
reuse frozen song_context.json
reuse semantic clips not listed in --rework
regenerate selected semantic blocks
regenerate subtitles and final video
write manifest
```

If `song_context.json` does not exist, `--rework` fails. Run a normal generation first.

### Rebuild-final run

`--rebuild-final` keeps the output directory and does not call ComfyUI for LLM/image/video generation.

Behavior:

```text
keep --output-dir
reuse semantic clips from output/work/clips/
regenerate subtitles and final video
write manifest
```

Internal subclips are not required for `--rebuild-final`.

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

Art direction for the whole video. This is prompt style only. Technical width/height/fps live in `config.json`.

```text
alignment.json OR alignment.lrc
```

Lyric timing. `alignment.json` gives word-level timing and smoother karaoke. `alignment.lrc` gives line-level timing.

```text
audio.mp3
```

Full song audio. `.wav`, `.m4a`, and `.flac` are also accepted as `audio.wav`, `audio.m4a`, or `audio.flac`.

Alternative stem input:

```text
vocals.mp3
instrumental.mp3
```

If both stems exist, the runner mixes them into the full song internally. `.wav`, `.m4a`, and `.flac` stem fallbacks are also accepted.

### 7.2 Recommended file

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

Art direction override for semantic block `N`.

Examples:

```text
video_style_0.txt      # intro
video_style_1.txt      # first semantic block after intro, usually verse 1
video_style_2.txt
video_style_001.txt    # also accepted
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

ASS style override for semantic block `N`.

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
output/final_video.mp4
output/manifest.json
```

Work folder:

```text
output/work/
  audio/
    full_mix.wav
    final_audio.wav

  clips/
    clip_000_intro.mp4
    clip_001_verse_001.mp4
    ...

  subclips/
    block_NNN/
      part_001.mp4
      part_002.mp4

  frames/
    block_NNN/
      part_001_start.png
      part_001_last.png

  clips_raw/
    raw ComfyUI video copies and semantic intermediates

  plans/
    song_context.json
    plan_NNN.json
    plan_NNN_part_MMM.json

  subs/
    karaoke.ass

  video/
    video_only.mp4

  debug/
    parsed_verses_all.json
    timeline_blocks.json
    config_used.json
    video_style_map.json
    subtitle_styles_map.json
    timing_report.json
    video_generation_NNN.json

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
          planner_template.txt
          planner_raw.json
          planner_result.json
          planner_history.json
          image_patched.json
          image_history.json
          video_patched.json
          video_history.json
          video_generation.json
```

`output/work/clips/` contains semantic clips used by final assembly. `output/work/subclips/` and `output/work/frames/` are internal artifacts for long-range rendering.

## 9. Semantic blocks and internal subranges

The runner builds semantic timeline blocks from parsed lyric timing:

```text
intro
verse
instrumental
outro
```

Long gaps without lyrics become `instrumental` blocks when they exceed the threshold configured in `config.json`.

Every semantic block is rendered through one or more internal subranges:

```text
semantic block -> subrange(s) -> one semantic clip
```

If the block duration is within `max_workflow_seconds`, it has exactly one subrange. That single subrange has empty subrange text, so the prompt does not repeat the full lyrics twice.

If the block is longer than `max_workflow_seconds`, it is split near `recommended_workflow_seconds`, preferring line/word timing boundaries when available.

Rendering flow:

```text
first subrange:
  image_from_prompt_api.json -> start image
  video_from_image_api.json -> subclip

next subrange:
  extract previous subclip last frame -> start image
  video_from_image_api.json -> subclip

after all subranges:
  concatenate subclips -> semantic clip
  trim/pad semantic clip to exact block duration
```

The next semantic block starts from a fresh generated image. Last-frame chaining is only inside one semantic block.

### Visual preroll and subtitle lead-in

For lyric ranges, `range_visual_preroll_seconds` lets the semantic clip start slightly before the first sung word, but only by taking time from lyric-free gap before that range. It never overlaps a previous sung lyric. This means boundaries such as `intro -> verse` or `instrumental -> verse` can show the new verse scene before the first word is sung.

`subtitle_line_preroll_seconds` makes a subtitle line visible slightly before its first word. The karaoke timing itself is not shifted: the ASS event starts earlier, but a silent karaoke gap is inserted before the first word so highlighting follows the audio timing.

Word-level karaoke is gap-aware: gaps between word timestamps are preserved instead of compressing all words together.

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
  "width": 1280,
  "height": 720,
  "fps": 24,
  "recommended_workflow_seconds": 20,
  "max_workflow_seconds": 30,
  "instrumental_gap_min_seconds": 8.0,
  "instrumental_gap_min_ratio_of_median_verse": 0.5,
  "local_context_radius": 2,
  "range_visual_preroll_seconds": 0.25,
  "subtitle_line_preroll_seconds": 0.25,
  "min_karaoke_unit_seconds": 0.01
}
```

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

The runner patches start image, video prompt, negative prompt, duration, fps, width, height, seeds, and output prefix.

## 15. External repositories and tools

### FFmpeg

Purpose in this project:

```text
audio conversion
audio mixing
video trim/pad
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

Then edit `STABLE_TS_EXE` in `align_stable_ts.cmd`.

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

### `--rework` fails because song context is missing

Run a normal generation first. `--rework` intentionally keeps existing `song_context.json` frozen.

### A normal run removed my output folder

That is expected. A normal run is a fresh creative attempt and cleans `--output-dir` first. Use a new `--output-dir` to keep multiple attempts side by side.
