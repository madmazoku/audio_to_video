diff --git a/README.md b/README.md
index c546c0a..6c1ec46 100644
--- a/README.md
+++ b/README.md
@@ -230,7 +230,7 @@ input/lyrics.txt
 input/video_style.txt
 ```
 
-`output/work/alignment/alignment.json`, `output/work/alignment/matched_verses.json`, release subtitles, preview subtitles, debug preview subtitles, and `output/subtitle_preview.mp4` are lazy artifacts. If raw alignment is missing, the runner creates it from `input/vocals.*`; if there are no vocals, provide `input/alignment.lrc` for line-level timing without stable-ts. If `matched_verses.json` exists, the runner reads it and does not rematch lyrics. If subtitle artifacts already exist, the runner reuses them. Use `--refresh-alignment` to invalidate alignment/matching/timeline/subtitle/preview caches after editing lyrics. To refresh subtitle styling only, delete the relevant files under `output/work/subs/` and/or `output/subtitle_preview.mp4`; they will be recreated lazily.
+`output/work/alignment/alignment.json`, `output/work/alignment/alignment.lrc`, `output/work/alignment/matched_verses.json`, release subtitles, preview subtitles, debug preview subtitles, and `output/subtitle_preview.mp4` are lazy artifacts. If raw alignment is missing, the runner creates it from `input/vocals.*`; if there are no vocals, provide `input/alignment.lrc` for line-level timing without stable-ts. `alignment.lrc` is a standard line-level LRC file: for stable-ts word timing it is generated from the matched lyric lines, and for no-vocals line timing it is copied/normalized from the input LRC source. If `matched_verses.json` exists, the runner reads it and does not rematch lyrics. If subtitle artifacts already exist, the runner reuses them. Use `--refresh-alignment` to invalidate alignment/matching/timeline/subtitle/preview caches after editing lyrics. To refresh subtitle styling only, delete the relevant files under `output/work/subs/` and/or `output/subtitle_preview.mp4`; they will be recreated lazily.
 
 Start ComfyUI in another terminal.
 
@@ -689,6 +689,11 @@ CURRENT SUBRANGE TEXT, when present
 Visual style is the mandatory style contract. Current subrange text is the highest factual priority when a semantic block is split. Bracket directives are metadata and must not be rendered as visible text.
 
 
+
+### Future prompt generation architecture
+
+The planned prompt-generation replacement is documented in [`PROMPT_GENERATION_PLAN.md`](PROMPT_GENERATION_PLAN.md). It uses an effective style contract, semantic planner, prompt writer, critic, and retry loop. `video_style_N.txt` is planned as a full zero-based range style override rather than a diff. No legacy single-pass prompt path is kept in that design.
+
 ### Action-oriented video prompts
 
 The default block planner rules are tuned for LTXV image-to-video. The LLM is asked to write every `video_prompt` as a short non-looping event arc instead of an idle animated illustration. The image prompt defines the starting keyframe; the video prompt must describe what happens after that frame.
