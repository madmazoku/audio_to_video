# aligned_song_video_project

Generate a music video from prepared audio and lyric alignment.

## Requirements and external services

The repository has one Python requirements file for the runner itself:

```powershell
python.exe -m pip install -r requirements.txt
```

`requirements.txt` contains only runtime dependencies used directly by `aligned_song_video_runner.py`:

```text
requests
websocket-client
```

External tools/services such as FFmpeg, ComfyUI, ComfyUI custom nodes/models, and stable-ts are not Python dependencies of this project. They are installed separately and described below.

`websocket-client` is required because the runner listens to ComfyUI websocket progress messages. There is no polling fallback. If `websocket-client` is missing, the runner fails at import time.



### Required external tools/services

The project expects these external pieces:

```text
Python 3.10+ recommended
FFmpeg / ffprobe available in PATH
ComfyUI running at http://127.0.0.1:8188
ComfyUI custom nodes/models required by workflows/
stable-ts optional, only for creating input/alignment.json
```

No Suno API or ACE-Step API is required by the runner. Suno/ACE can be used outside this project to create `audio.mp3`, stems, or alignment files.

## Setup: FFmpeg

FFmpeg is required for audio conversion, video trimming/padding, concat, muxing, and burning ASS subtitles. FFmpeg describes itself as a cross-platform solution for recording, converting, and streaming audio/video. citeturn898315search5

Install FFmpeg so both commands work from the same shell where you run the project:

```powershell
ffmpeg -version
ffprobe -version
```

On Windows, download a prebuilt FFmpeg package from the official download page. The official page notes that FFmpeg itself provides source code and links to compiled packages. citeturn898315search2

Typical Windows setup:

```text
1. Download a Windows build.
2. Extract it, for example to G:\Tools\ffmpeg.
3. Add G:\Tools\ffmpeg\bin to PATH.
4. Open a new terminal.
5. Run ffmpeg -version and ffprobe -version.
```

The runner defaults to:

```text
ffmpeg
ffprobe
```

so PATH setup is the cleanest option.

## Setup: ComfyUI

ComfyUI must be running before the runner starts. The official ComfyUI repository documents manual installation and launching with `python main.py`. citeturn898315search1

Typical setup:

```powershell
cd G:\Git
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --listen 127.0.0.1 --port 8188
```

The runner uses:

```text
http://127.0.0.1:8188
```

by default.

You can override it:

```powershell
python.exe .\aligned_song_video_runner.py --comfy-url http://127.0.0.1:8188
```

### ComfyUI workflows and required nodes

This project uses the workflow files in:

```text
workflows/
  planner_visual_prompts_api.json
  video_from_generated_image_api.json
```

The script and workflows are versioned together. Do not move workflows into `input/`.

The current workflows require ComfyUI nodes/classes including:

```text
GGUFLoader
LLM_local
Basic data handling: PathSaveStringFile
```

and the image/video generation nodes present in `video_from_generated_image_api.json`.

If ComfyUI fails with an unknown node type, install the missing custom node in your ComfyUI `custom_nodes/` folder and restart ComfyUI. If ComfyUI fails with a missing model path, download or place the model where the workflow expects it.

The planner workflow currently expects a local GGUF LLM model path in the workflow JSON. Check:

```text
workflows/planner_visual_prompts_api.json
```

and verify that the `GGUFLoader` `model_path` exists on your machine.


There is intentionally no separate requirements file for stable-ts. Keep stable-ts as an external alignment tool and configure its path in `align_stable_ts.cmd`.

## Setup: stable-ts alignment

stable-ts is optional and installed separately. It is not part of `requirements.txt`. It is used only to create `input/alignment.json` from known lyrics and audio.

Install it in a separate environment if preferred:

```powershell
cd G:\Git
git clone https://github.com/jianfch/stable-ts.git
cd stable-ts
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -U stable-ts
```

Install stable-ts in its own environment or in any environment you prefer. The wrapper only needs the path to `stable-ts.exe`. citeturn898315search3

This project includes an alignment wrapper:

```text
align_stable_ts.cmd
```

The wrapper expects stable-ts at:

```text
G:\Git\stable-ts\.venv\Scripts\stable-ts.exe
```

Edit the `STABLE_TS_EXE` variable inside `align_stable_ts.cmd` if your path is different.

Run alignment with default input folder and English language:

```cmd
align_stable_ts.cmd
```

Run alignment with an explicit input folder and language:

```cmd
align_stable_ts.cmd .\input en
align_stable_ts.cmd .\input ru
```

The wrapper reads:

```text
input/lyrics.txt
input/vocals.mp3
```

or, if `vocals.mp3` is missing:

```text
input/audio.mp3
```

and writes:

```text
input/alignment.json
```


## Random seeds and ComfyUI output isolation

Each newly generated video block now uses fresh random seeds for:

```text
image_seed
video_seed
video_refine_seed
```

This means:

```text
a new full run generates new variants
--rework regenerates selected blocks with different results
--rebuild-final does not regenerate blocks and does not change seeds
```

The runner also writes each run into a unique ComfyUI output subfolder:

```text
aligned_song/run_YYYYMMDD_HHMMSS_xxxxxxxx/segment_NNN/
```

This prevents accidental pickup of stale videos from previous runs.

Seeds and the ComfyUI segment subfolder are recorded in:

```text
output/manifest.json
output/work/debug/video_generation_NNN.json
```

## Quick start

```powershell
python.exe -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Start ComfyUI in a different terminal first.
# Then run:
.\.venv\Scripts\python.exe .\aligned_song_video_runner.py --limit 2
```

Input files must be placed in `input/`. Output is written to `output/` by default.

## Folder layout

`workflows/` and `rules/` are part of the algorithm and must stay next to the runner script. There are no CLI parameters for them.

```text
aligned_song_video_project/
  aligned_song_video_runner.py
  align_stable_ts_project.cmd

  workflows/
    planner_visual_prompts_api.json
    video_from_generated_image_api.json

  rules/
    song_context_system.txt
    song_context_user.txt
    block_planner_system.txt
    block_planner_intro.txt
    block_planner_verse.txt
    block_planner_outro.txt
    literal_scene_rules.txt

  input/
    video_style.txt
    lyrics.txt
    audio.mp3 / vocals.mp3 + instrumental.mp3
    alignment.json / alignment.lrc

  output/
    final_video.mp4
    manifest.json
    work/
```

## Inputs

By default, the runner reads from:

```text
./input
```

You can override it:

```powershell
python.exe .\aligned_song_video_runner.py --input-dir G:\Git\audio_to_video\input
```

Required input:

```text
video_style.txt
```

Audio mode A:

```text
audio.mp3
```

Audio mode B:

```text
vocals.mp3
instrumental.mp3
```

Alignment mode A:

```text
alignment.json
```

Alignment mode B:

```text
alignment.lrc
```

`lyrics.txt` is recommended for `alignment.json`, because it preserves the real verse and line breaks while word timestamps come from JSON.

## Stage 1 features

This version supports the first implementation stage.

### Instrumental timeranges

The runner can split long lyric gaps into separate timeline blocks with `kind="instrumental"`.

Gap detection is controlled by `config.json`:

```json
{
  "instrumental_gap_min_seconds": 8.0,
  "instrumental_gap_min_ratio_of_median_verse": 0.5
}
```

The effective threshold is:

```text
max(instrumental_gap_min_seconds, median_verse_duration * instrumental_gap_min_ratio_of_median_verse)
```

A gap greater than or equal to that threshold becomes an instrumental block. Short gaps stay attached to the previous verse block.

Instrumental blocks use:

```text
rules/block_planner_instrumental.txt
```

### Bracket directives in lyrics

Lines in square brackets inside `lyrics.txt` are metadata, not sung lyrics:

```text
[Verse]
[Vocal: alto female]
[Fasten, move to major key, allegro]
Real lyric line
Real lyric line
```

Bracket directives:

```text
do not participate in alignment matching
do not appear in subtitles
are stored as bracket_directives
are passed to the planner below actual lyrics in priority
```

They are intentionally not parsed as an enum because Suno-style directives may be arbitrary.

### Config and per-block video style

`video_style.txt` is artistic prompt style only.

Technical and timeline settings are read from:

```text
data/config.json
input/config.json
```

`input/config.json` is optional and overrides default values.

Default config:

```json
{
  "width": 1280,
  "height": 720,
  "fps": 24,
  "recommended_workflow_seconds": 20,
  "max_workflow_seconds": 30,
  "instrumental_gap_min_seconds": 8.0,
  "instrumental_gap_min_ratio_of_median_verse": 0.5
}
```

Per-block artistic style overrides:

```text
input/video_style_0.txt
input/video_style_1.txt
input/video_style_2.txt
```

Padded names are also accepted:

```text
input/video_style_000.txt
input/video_style_001.txt
```

Duplicate numeric ids are an error, for example:

```text
video_style_1.txt + video_style_001.txt
```

For block `N`, the effective style is:

```text
input/video_style_N.txt if present, otherwise input/video_style.txt
```

No special prompt marker is added. The effective text is simply substituted into the normal `{{VIDEO_STYLE}}` placeholder.

Debug files:

```text
output/work/debug/config_used.json
output/work/debug/video_style_map.json
output/work/debug/video_style_N_used.txt
```

## Input folder contents

`input/` is runtime data and can be excluded from git. Create it locally and put the song-specific files there.

Required:

```text
input/video_style.txt
```

Audio input: choose one mode.

Mode A, mixed song audio:

```text
input/audio.mp3
```

Mode B, separated stems:

```text
input/vocals.mp3
input/instrumental.mp3
```

Alignment input: choose one mode.

Word-level alignment:

```text
input/alignment.json
```

Line-level alignment:

```text
input/alignment.lrc
```

Recommended with `alignment.json`:

```text
input/lyrics.txt
```

`lyrics.txt` preserves verse and line breaks while word timestamps come from `alignment.json`.

Optional subtitle styles:

```text
input/subtitle_styles.ass
input/subtitle_styles_0.ass
input/subtitle_styles_1.ass
input/subtitle_styles_2.ass
```

Padded per-block subtitle style names are also accepted:

```text
input/subtitle_styles_000.ass
input/subtitle_styles_001.ass
input/subtitle_styles_002.ass
```

Each subtitle style file must define:

```text
Style: line,...
```

The default fallback is stored in:

```text
data/subtitle_styles.ass
```

`input/PUT_FILES_HERE.txt` is only a local reminder. The same information is duplicated here so `input/` does not need to be committed.

## Git repository recommendation

Commit the algorithm files:

```text
aligned_song_video_runner.py
align_stable_ts.cmd
README.md
workflows/
rules/
data/
run_full.cmd
run_limit_2.cmd
run_rebuild_final.cmd
run_rework_2.cmd
```

Exclude runtime/song-specific folders:

```gitignore
/input/
/output/
/out/
__pycache__/
*.pyc
```

If you want to keep empty folders in git, commit placeholder files such as `.gitkeep`, but do not commit audio, alignment, generated clips, or final renders.

## Outputs

By default, the runner writes to:

```text
./output
```

You can override it:

```powershell
python.exe .\aligned_song_video_runner.py --output-dir G:\Git\audio_to_video\output
```

Final outputs:

```text
output/final_video.mp4
output/manifest.json
```

Intermediate outputs:

```text
output/work/audio/
output/work/subs/karaoke.ass
output/work/plans/
output/work/clips_raw/
output/work/clips/
output/work/video/
output/work/debug/
```


## Usage

Full run:

```powershell
python.exe .\aligned_song_video_runner.py
```

Test the first two verses:

```powershell
python.exe .\aligned_song_video_runner.py --limit 2
```

Rework only block 2 while testing the first two verses:

```powershell
python.exe .\aligned_song_video_runner.py --limit 2 --rework 2
```

Rework intro:

```powershell
python.exe .\aligned_song_video_runner.py --rework 0
```

Rework blocks 2, 4 and 8:

```powershell
python.exe .\aligned_song_video_runner.py --rework 2 4 8
```

Rebuild final video from existing clips without generating video:

```powershell
python.exe .\aligned_song_video_runner.py --rebuild-final
```

## Timeline blocks

Video is generated as continuous timeline blocks, not just `verse.start -> verse.end`.

```text
clip_000_intro.mp4        audio_start -> verse_001.start
clip_001_verse_001.mp4    verse_001.start -> verse_002.start
clip_002_verse_002.mp4    verse_002.start -> verse_003.start
...
clip_NNN_verse_NNN.mp4
clip_N+1_outro.mp4        last_verse.end -> audio_end
```

`--rework` uses the same numbering:

```text
0      intro
1..N   verse blocks
N+1    outro, only in full-song mode
```

With `--limit 3`, blocks are:

```text
0 intro
1 verse 1
2 verse 2
3 verse 3
```

and audio/video ends at `verse_004.start`. No outro is created in limit mode.

Without `--limit`, audio is not cut by verse boundaries.

## Rules templates

Prompt construction is configured by files in `rules/`.

The runner loads:

```text
rules/song_context_system.txt
rules/song_context_user.txt
rules/block_planner_system.txt
rules/block_planner_intro.txt
rules/block_planner_verse.txt
rules/block_planner_outro.txt
rules/literal_scene_rules.txt
```

These files are part of the algorithm. Edit them to tune how LLM prompts are generated without changing Python code.

The block planner prompt is built as a priority sandwich:

```text
0. visual style
1. low-priority global song context
2. medium-priority local context +/- 2 verses
3. high-priority current block instruction/text
```

The literal scene rules make the current block's actual event more important than global motifs, neighboring verses, or previous visual summaries.

## Run modes and output directory policy

A normal run means no `--rework` and no `--rebuild-final`.

Normal run behavior:

```text
clean --output-dir
generate song_context.json
generate all block plans
generate all video clips
generate subtitles
generate final video
```

This matches the expectation that a normal run is a new creative attempt and should not reuse old generated artifacts from the same output directory.

`--rework` behavior:

```text
keep --output-dir
reuse existing clips for blocks not listed in --rework
require and reuse existing song_context.json
generate selected block plans/videos with new random seeds
generate subtitles and final video
```

`--rebuild-final` behavior:

```text
keep --output-dir
do not call LLM
do not generate video
reuse existing clips
generate subtitles and final video
```

Use a different `--output-dir` if you want to keep multiple full attempts side by side.

## End-of-run statistics

At the end of every successful run, including `--rework` and `--rebuild-final`, the runner prints a statistics block:

```text
[stats]
  total elapsed        : ...
  verses total/selected: ...
  timeline blocks      : ...
  clips generated/reused: ...
  parse alignment      : ...
  song context         : ...
  prepare audio        : ...
  timeline build       : ...
  render audio         : ...
  subtitles            : ...
  video generation     : ...
  concat video         : ...
  final mux            : ...
  final output         : ...
```

For `--rebuild-final`, video generation time is omitted if no video blocks were generated, and reused clips are counted under `clips generated/reused`.

## Recovery after late manifest errors

If a run fails after all clips were generated and after final muxing, for example at `write_timeline_manifest(...)`, you usually do not need to generate from zero again.

After applying the fixed runner, run:

```powershell
python.exe .\aligned_song_video_runner.py --rebuild-final --output-dir .\same-output-dir --input-dir .\input
```

`--rebuild-final` keeps the existing output directory, reuses existing clips, regenerates subtitles/final mux, and writes a fresh `manifest.json`.


## Song context policy

`song_context.json` is the global visual bible for the song.

The runner uses this policy:

```text
normal generation run  -> clean output dir and generate song_context.json
--rework               -> require and reuse existing song_context.json
--rebuild-final        -> do not call LLM for song context
```

This means a new normal run with the same `input/` is treated as a new creative attempt. The global context is regenerated, block prompts are regenerated, and generated video blocks use fresh random seeds.

`--rework` is different: it is meant to replace selected blocks inside an already established video. Therefore it keeps the existing global context frozen and regenerates only selected block plans/videos.

If `--rework` is used before a normal generation has created this file, the runner fails:

```text
output/work/plans/song_context.json
```

Debug files:

```text
output/work/plans/song_context_template.txt
output/work/plans/song_context_request.txt
output/work/plans/song_context_raw.json
output/work/plans/song_context_history.json
output/work/debug/song_context_used.json
```


## Subtitle styles

Subtitle rendering uses ASS karaoke colors inside a single ASS style.

Default style:

```text
data/subtitle_styles.ass
```

Song-level override:

```text
input/subtitle_styles.ass
```

Per-block override:

```text
input/subtitle_styles_0.ass
input/subtitle_styles_1.ass
input/subtitle_styles_2.ass
```

Padded names are also accepted:

```text
input/subtitle_styles_000.ass
input/subtitle_styles_001.ass
input/subtitle_styles_002.ass
```

The block number is parsed by regex:

```text
subtitle_styles_(\d+)\.ass
```

Duplicate numeric block IDs are an error, regardless of padding. These two files cannot exist together:

```text
input/subtitle_styles_1.ass
input/subtitle_styles_001.ass
```

Style resolution for block `N`:

```text
input/subtitle_styles_N.ass or input/subtitle_styles_NNN.ass
input/subtitle_styles.ass
data/subtitle_styles.ass
```

Every subtitle style file must contain a `[V4+ Styles]` section, a `Format:` line, and exactly this semantic style name:

```text
Style: line,...
```

The default colors are:

```text
PrimaryColour   = white text before karaoke highlight
SecondaryColour = yellow karaoke highlight
```

For word-level alignment, each word time range is split evenly across its timed characters. For line-level LRC, the whole line time range is split evenly across its timed characters. Spaces and punctuation remain visible but do not receive their own timing tags. This makes karaoke highlighting smoother, especially for long or slowly sung words.

The final generated ASS file always creates:

```text
default_line
```

and only creates clip-specific styles for blocks that have override files:

```text
clip_3_line
```

Debug files:

```text
output/work/debug/subtitle_styles_map.json
output/work/debug/subtitle_style_default_used.ass
output/work/debug/subtitle_style_3_used.ass
```


## stable-ts alignment wrapper

Default input folder is `./input`, and default language is `en`:

```cmd
align_stable_ts_project.cmd
```

Explicit input folder:

```cmd
align_stable_ts_project.cmd G:\Git\audio_to_video\input
```

Explicit input folder and language:

```cmd
align_stable_ts_project.cmd .\input en
align_stable_ts_project.cmd .\input ru
```

The wrapper reads:

```text
input/lyrics.txt
input/vocals.mp3
```

or, if `vocals.mp3` is missing:

```text
input/audio.mp3
```

It writes:

```text
input/alignment.json
```

Language is controlled only by the second argument. If it is not provided, `en` is used.

## Raw clip audio

`clips_raw` may contain MP4 files with audio tracks produced by the workflow. They are not used. The runner always creates final clips without audio in:

```text
output/work/clips/
```

Final audio comes only from `audio.mp3` or from stems.

## Duration policy

Duration policy is controlled by constants in `aligned_song_video_runner.py`:

```python
VIDEO_RECOMMENDED_SECONDS = 20
VIDEO_MAX_SECONDS = 30
```

Blocks up to 20 seconds are normal. Blocks from 20 to 30 seconds show a warning and continue. Blocks longer than 30 seconds fail.
