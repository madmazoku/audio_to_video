from __future__ import annotations

import argparse
import difflib
import json
import math
import re
import shutil
import subprocess
import time
from datetime import datetime
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import random

from core.generator import ImageRequest, VideoRequest, LlmRequest, create_generator, merge_config

__version__ = "1.8.0"
RUNNER_BUILD_ID = __version__

# Rendering-only gap between adjacent identical karaoke lines.
# This is intentionally a code constant, not a config.json parameter.
KARAOKE_REPEAT_RESET_SECONDS = 0.08

# Final-song karaoke readability guard. Forced alignment often marks the
# lexical end of the last word too tightly because there is no following
# sung word to act as a right-hand anchor. This minimum applies only to the
# final lyric word before a terminal metadata-only/non-lyrical tail.
TERMINAL_SONG_WORD_MIN_KARAOKE_SECONDS = 1.10
TERMINAL_SONG_WORD_MAX_EXTRA_SECONDS = 0.55

def log_timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    text = str(msg)
    lines = text.splitlines()
    if not lines:
        print("", flush=True)
        return
    for line in lines:
        if line.strip():
            print(f"{log_timestamp()}  {line.lstrip()}", flush=True)
        else:
            print("", flush=True)


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def invalidate_alignment_related_artifacts(output_root: Path) -> None:
    """Invalidate caches derived from lyrics/audio alignment.

    This intentionally preserves visual source material in work/clips_unscaled
    and raw generated clips. Scaled clips and preview/final outputs are derived
    from the current timeline and must be rebuilt after rematching.
    """
    work = output_root / "work"
    for path in [
        work / "alignment",
        work / "clips",
        work / "subs" / "karaoke.ass",
        work / "subs" / "preview_karaoke.ass",
        work / "subs" / "preview_debug.ass",
        output_root / "subtitle_preview.mp4",
        output_root / "final_video.mp4",
        output_root / "manifest.json",
    ]:
        remove_path(path)

    debug = work / "debug"
    for pattern in [
        "alignment_*.json",
        "alignment_*.txt",
        "parsed_verses_all.json",
        "timeline_blocks.json",
        "preview_timeline_blocks.json",
        "timing_report.json",
        "preview_timing_report.json",
        "clip_scaling_report.json",
        "clip_validation_report.json",
    ]:
        for path in debug.glob(pattern):
            remove_path(path)


def ensure_alignment_artifact(
    input_dir: Path,
    out_dir: Path,
    debug_dir: Path,
    alignment_dir: Path,
    stable_ts_cmd: str,
    lyrics_language: str,
) -> None:
    """Create the alignment artifact if it is missing.

    Cache lifetime is explicit: existing alignment artifacts are trusted until
    the user removes the cache/work directory or requests --refresh-alignment.
    """
    json_path = alignment_dir / "alignment.json"
    lrc_path = alignment_dir / "alignment.lrc"
    if json_path.exists() or lrc_path.exists():
        log(f"[stage] use cached alignment: {json_path if json_path.exists() else lrc_path}")
        return

    align_kind, align_path, _ = detect_alignment_source(input_dir)
    alignment_dir.mkdir(parents=True, exist_ok=True)
    if align_kind == "vocals":
        run_stable_ts_alignment(
            input_dir,
            out_dir,
            debug_dir,
            stable_ts_cmd,
            lyrics_language,
        )
    elif align_kind == "lrc":
        target_lrc = alignment_dir / "alignment.lrc"
        shutil.copy2(align_path, target_lrc)
        write_json(debug_dir / "lrc_alignment_source.json", {
            "source": str(align_path),
            "copied_to": str(target_lrc),
            "mode": "line_level_no_stable_ts",
        })
        log(f"[stage] use LRC line timing without stable-ts: {align_path}")

def format_range_id(index: int) -> str:
    return f"R{int(index):03d}"


def format_range_id_list(indices: Iterable[int]) -> str:
    return ", ".join(format_range_id(i) for i in indices)


def select_ranges_for_final(all_blocks: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    if not limit:
        return list(all_blocks)
    if limit < 0:
        raise RuntimeError("--limit must be >= 0")
    if limit > len(all_blocks):
        last = len(all_blocks) - 1
        raise RuntimeError(
            f"--limit {limit} is greater than available range count {len(all_blocks)} "
            f"({format_range_id(0)}..{format_range_id(last)})."
        )
    return list(all_blocks[:limit])


def select_ranges_to_generate(ranges_for_final: List[Dict[str, Any]], rework: Optional[List[int]]) -> List[Dict[str, Any]]:
    if not rework:
        return list(ranges_for_final)
    by_index = {int(b["block_index"]): b for b in ranges_for_final}
    missing = [idx for idx in rework if idx not in by_index]
    if missing:
        available = sorted(by_index)
        selected_desc = "none"
        if available:
            selected_desc = f"{format_range_id(available[0])}..{format_range_id(available[-1])}"
        raise RuntimeError(
            f"--rework contains range(s) outside selected range set: {format_range_id_list(missing)}. "
            f"Selected ranges are {selected_desc}. Increase --limit or remove invalid indices."
        )
    return [by_index[idx] for idx in rework]


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 60:
        return f"{seconds:.2f}s"
    minutes = int(seconds // 60)
    rest = seconds - minutes * 60
    return f"{minutes}m {rest:.2f}s"


def stats_start(stats: Dict[str, float], key: str) -> None:
    stats[f"_{key}_start"] = time.perf_counter()


def stats_end(stats: Dict[str, float], key: str) -> None:
    start = stats.pop(f"_{key}_start", None)
    if start is not None:
        stats[key] = stats.get(key, 0.0) + (time.perf_counter() - start)


def print_run_stats(stats: Dict[str, float], total_verses: int, selected_verses: int, blocks_count: int, clips_generated: int, clips_reused: int, output_path: Path) -> None:
    total_elapsed = time.perf_counter() - stats.get("_run_start", time.perf_counter())

    log("\n[stats]")
    log(f"  total elapsed        : {fmt_duration(total_elapsed)}")
    log(f"  verses total/selected: {total_verses}/{selected_verses}")
    log(f"  timeline blocks      : {blocks_count}")
    log(f"  clips generated/reused: {clips_generated}/{clips_reused}")

    ordered = [
        ("parse_alignment", "parse alignment"),
        ("song_context", "song context"),
        ("prepare_audio", "prepare audio"),
        ("timeline", "timeline build"),
        ("render_audio", "render audio"),
        ("subtitles", "subtitles"),
        ("video_generation", "video generation"),
        ("concat", "concat video"),
        ("final_mux", "final mux"),
    ]

    for key, label in ordered:
        if key in stats:
            log(f"  {label:<20}: {fmt_duration(stats[key])}")

    log(f"  final output         : {output_path}")


def read_text(path: Path, required: bool = True) -> str:
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Required file not found: {path}")
        return ""
    return path.read_text(encoding="utf-8-sig").strip()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def invalidate_timeline_derived_artifacts(output_root: Path) -> None:
    """Invalidate artifacts derived from matched semantic/timing data only."""
    work = output_root / "work"
    for path in [
        work / "clips",
        work / "subs" / "karaoke.ass",
        work / "subs" / "preview_karaoke.ass",
        work / "subs" / "preview_debug.ass",
        output_root / "subtitle_preview.mp4",
        output_root / "final_video.mp4",
        output_root / "manifest.json",
    ]:
        remove_path(path)

    debug = work / "debug"
    for pattern in [
        "parsed_verses_all.json",
        "timeline_blocks.json",
        "preview_timeline_blocks.json",
        "timing_report.json",
        "preview_timing_report.json",
        "clip_scaling_report.json",
        "clip_validation_report.json",
    ]:
        for path in debug.glob(pattern):
            remove_path(path)


def format_lrc_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    total_centiseconds = int(round(seconds * 100.0))
    minutes = total_centiseconds // 6000
    rem = total_centiseconds % 6000
    whole_seconds = rem // 100
    centiseconds = rem % 100
    return f"[{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}]"


def write_line_level_lrc_from_matched_verses(verses: List[Dict[str, Any]], out_path: Path) -> None:
    """Write standard line-level LRC from matched lyric line timings.

    This is a human-readable alignment artifact. It uses lyrics.txt text from the
    matched timeline and one timestamp per sung line. Word-level/enhanced LRC is
    intentionally not emitted here; the goal is a simple standard synced-lyrics
    file that can be inspected or imported by common tools.
    """
    rows: List[Tuple[float, str]] = []
    for verse in verses:
        verse_start = verse.get("start")
        for line in verse.get("lines") or []:
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            start = line.get("start", verse_start)
            try:
                start_f = float(start)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(start_f):
                continue
            rows.append((start_f, text))

        # Some imported/cached alignments may only have a verse-level text block.
        if not verse.get("lines"):
            text_block = str(verse.get("text") or "").strip()
            try:
                start_f = float(verse_start)
            except (TypeError, ValueError):
                continue
            if text_block and math.isfinite(start_f):
                for offset, text in enumerate(t.strip() for t in text_block.splitlines() if t.strip()):
                    # Keep a deterministic order without inventing meaningful line timings.
                    rows.append((start_f + offset * 0.01, text))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(f"{format_lrc_timestamp(start)}{text}" for start, text in rows) + ("\n" if rows else ""), encoding="utf-8")


def ensure_line_level_lrc_from_matched_verses(verses: List[Dict[str, Any]], alignment_dir: Path) -> Path:
    lrc_path = alignment_dir / "alignment.lrc"
    if lrc_path.exists():
        return lrc_path
    write_line_level_lrc_from_matched_verses(verses, lrc_path)
    log(f"[stage] write line-level LRC: {lrc_path}")
    return lrc_path


REQUIRED_RULE_FILES = [
    "song_context_system.txt",
    "song_context_user.txt",
    "block_planner_system.txt",
    "block_planner_intro.txt",
    "block_planner_verse.txt",
    "block_planner_instrumental.txt",
    "block_planner_outro.txt",
    "literal_scene_rules.txt",
]


def load_rules(rules_dir: Path) -> Dict[str, str]:
    rules: Dict[str, str] = {}
    missing: List[str] = []

    for name in REQUIRED_RULE_FILES:
        path = rules_dir / name
        if not path.exists():
            missing.append(str(path))
            continue
        rules[name] = path.read_text(encoding="utf-8-sig").strip()

    if missing:
        raise FileNotFoundError("Missing rules file(s):\n" + "\n".join(missing))

    return rules


def render_template(template: str, values: Dict[str, Any], template_name: str) -> str:
    rendered = template

    for key, value in values.items():
        if isinstance(value, (dict, list)):
            text_value = json.dumps(value, ensure_ascii=False, indent=2)
        else:
            text_value = str(value)
        rendered = rendered.replace("{{" + key + "}}", text_value)

    unresolved = sorted(set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", rendered)))
    if unresolved:
        raise RuntimeError(
            f"Unresolved placeholder(s) in {template_name}: "
            + ", ".join("{{" + x + "}}" for x in unresolved)
        )

    return rendered


def save_prompt_debug(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def make_run_id() -> str:
    return time.strftime("run_%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:8]


def random_seed() -> int:
    return random.SystemRandom().randint(1, 2**31 - 1)


def ffprobe_duration(path: Path, ffprobe_bin: str) -> float:
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    return float(out)


def run_cmd(cmd: List[str]) -> None:
    log("  " + " ".join(f'"{x}"' if " " in str(x) else str(x) for x in cmd))
    subprocess.run(cmd, check=True)


APOSTROPHE_CHARS = "'â€™â€˜Ê¼`Â´"
WORD_JOIN_CHARS = "-" + APOSTROPHE_CHARS


def norm_word(s: str) -> str:
    s = s.casefold().replace("\u0451", "\u0435").replace("\u0401", "\u0435")
    for ch in APOSTROPHE_CHARS:
        s = s.replace(ch, "'")
    s = re.sub(r"[^\w]+", "", s, flags=re.U)
    return s


def lyric_words(text: str) -> List[str]:
    # Keep contractions as a single display token. Stable-ts often returns
    # words such as shouldâ€™ve / shouldnâ€™t as one word; splitting lyrics on
    # curly apostrophes made subtitles render them as "should ve".
    joiners = re.escape(WORD_JOIN_CHARS)
    pattern = rf"\w+(?:[{joiners}]\w+)*"
    return [w for w in re.findall(pattern, text, flags=re.U) if norm_word(w)]


LYRICS_SEPARATOR_SPLIT_RE = re.compile(
    r"(?m)(^[ \t]*(?:\*{3}|-{3})(?:[ \t]+(?:@|#(?:<|>)?)[ \t]+(?:\d+:)?\d+:\d{2}\.\d{3})?[ \t]*$)"
)
LYRICS_SEPARATOR_RE = re.compile(
    r"^(?P<separator>\*{3}|-{3})(?:\s+(?P<mode>@|#(?:<|>)?)\s+"
    r"(?P<time>(?:\d+:)?\d+:\d{2}\.\d{3}))?$"
)


def parse_manual_timestamp(value: str) -> float:
    parts = str(value).split(":")
    if len(parts) == 2:
        hours = 0
        minutes_text, seconds_text = parts
        minutes_must_fit_hour = False
    elif len(parts) == 3:
        hours = int(parts[0])
        minutes_text, seconds_text = parts[1:]
        minutes_must_fit_hour = True
    else:
        raise ValueError(f"Invalid manual boundary timestamp: {value}")
    minutes = int(minutes_text)
    seconds = float(seconds_text)
    if minutes < 0 or (minutes_must_fit_hour and minutes >= 60) or seconds < 0.0 or seconds >= 60.0:
        raise ValueError(f"Invalid manual boundary timestamp: {value}")
    return hours * 3600.0 + minutes * 60.0 + seconds


def parse_lyrics_separator(line: str) -> Optional[Dict[str, Any]]:
    stripped = str(line).strip()
    match = LYRICS_SEPARATOR_RE.fullmatch(stripped)
    if not match:
        return None
    mode_token = match.group("mode")
    time_text = match.group("time")
    if mode_token == "@":
        mode = "exact"
    elif mode_token in {"#", "#<"}:
        # Bare # is retained as a backward-compatible alias for #<.
        mode = "snap_previous"
    elif mode_token == "#>":
        mode = "snap_next"
    else:
        mode = "automatic"
    return {
        "separator": match.group("separator"),
        "mode": mode,
        "mode_token": mode_token,
        "requested_time": parse_manual_timestamp(time_text) if time_text else None,
        "timestamp": time_text,
        "source": stripped,
    }


def is_subrange_divider_line(line: str) -> bool:
    parsed = parse_lyrics_separator(line)
    return bool(parsed and parsed["separator"] == "---")


def is_alignment_meta_token(text: str) -> bool:
    stripped = str(text).strip()
    separator = parse_lyrics_separator(stripped)
    return (
        bool(separator)
        or (len(stripped) >= 2 and stripped.startswith("[") and stripped.endswith("]"))
        or stripped.startswith("[")
        or stripped.endswith("]")
    )


def clean_lyrics_for_alignment_text(lyrics_text: str) -> str:
    """Return lyrics containing only sung lines.

    Bracket directive lines and *** range separators are excluded before
    stable-ts alignment.
    """
    out: List[str] = []
    for raw_line in lyrics_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        separator = parse_lyrics_separator(line)
        if separator and separator["separator"] == "***":
            out.append("")
            continue
        if separator and separator["separator"] == "---":
            continue
        if is_bracket_directive_line(line):
            continue
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text + "\n" if text else ""


def write_clean_alignment_lyrics(input_dir: Path, output_dir: Path, debug_dir: Path) -> Path:
    lyrics_path = input_dir / "lyrics.txt"
    if not lyrics_path.exists():
        raise FileNotFoundError(f"Cannot auto-align without lyrics.txt: {lyrics_path}")

    clean_text = clean_lyrics_for_alignment_text(read_text(lyrics_path))
    if not clean_text.strip():
        raise RuntimeError(f"No sung lyric lines found in {lyrics_path}")

    out_path = output_dir / "alignment_lyrics_clean.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(clean_text, encoding="utf-8")
    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "alignment_lyrics_clean.txt").write_text(clean_text, encoding="utf-8")
    return out_path


def word_similarity(a: str, b: str) -> float:
    aa = norm_word(a)
    bb = norm_word(b)
    if not aa or not bb:
        return 0.0
    if aa == bb:
        return 1.0
    return difflib.SequenceMatcher(None, aa, bb).ratio()


def word_match_threshold(a: str, b: str, base_threshold: float) -> float:
    """Return a conservative fuzzy threshold for lyric/alignment tokens.

    Short words are dangerous anchors: e.g. ``one`` vs ``stone`` or
    ``and`` vs ``stand`` can score around 0.75 with SequenceMatcher.  Those
    false positives are especially destructive in repeated choruses because
    they advance the monotonic cursor.  Require much stronger similarity for
    short tokens while keeping the configured threshold for longer words.
    """
    aa = norm_word(a)
    bb = norm_word(b)
    min_len = min(len(aa), len(bb))
    max_len = max(len(aa), len(bb))
    if max_len <= 2:
        return 1.0
    if min_len <= 3:
        return max(0.90, base_threshold)
    if max_len <= 5:
        return max(0.84, base_threshold)
    if max_len <= 7:
        return max(0.80, base_threshold)
    return base_threshold


def words_are_match(a: str, b: str, base_threshold: float) -> Tuple[bool, float, str]:
    sim = word_similarity(a, b)
    threshold = word_match_threshold(a, b, base_threshold)
    if sim >= 1.0:
        return True, sim, "match"
    if sim >= threshold:
        return True, sim, "fuzzy_match"
    return False, sim, "mismatch"


def next_lyric_line_words(verse_line_words: List[List[List[str]]], verse_index: int, line_index: int) -> List[str]:
    """Return the next actual lyric line after a block/line, skipping non-lyrical blocks."""
    current_lines = verse_line_words[verse_index] if 0 <= verse_index < len(verse_line_words) else []
    if line_index + 1 < len(current_lines):
        return current_lines[line_index + 1]
    for vi in range(verse_index + 1, len(verse_line_words)):
        if verse_line_words[vi]:
            return verse_line_words[vi][0]
    return []


def block_has_lyric_text(block: Dict[str, Any]) -> bool:
    """Return True only when a block contains actual sung lyric text.

    A semantic block is non-lyrical when, after removing blank lines,
    bracket metadata and separator/control lines, no display/sung text remains.
    This deliberately treats both metadata-only blocks and completely empty
    blocks as musical/non-lyrical sections.
    """
    def is_sung(value: Any) -> bool:
        text = str(value or "").strip()
        if not text:
            return False
        if parse_lyrics_separator(text):
            return False
        if is_bracket_directive_line(text):
            return False
        return bool(lyric_words(text))

    lines = block.get("lines_text")
    if isinstance(lines, list):
        return any(is_sung(line) for line in lines)

    lines = block.get("lines")
    if isinstance(lines, list):
        for line in lines:
            value = line.get("text", "") if isinstance(line, dict) else line
            if is_sung(value):
                return True
        return False

    text = str(block.get("text", "") or "")
    return any(is_sung(line) for line in text.splitlines())

def synthesize_word_timing(
    expected: str,
    previous_end: Optional[float],
    next_start: Optional[float],
) -> Dict[str, Any]:
    if previous_end is None and next_start is None:
        start = 0.0
        end = 0.25
    elif previous_end is None:
        end = float(next_start)
        start = max(0.0, end - 0.25)
    elif next_start is None:
        start = float(previous_end)
        end = start + 0.25
    else:
        start = float(previous_end)
        end = max(start + 0.05, min(float(next_start), start + max(0.05, (float(next_start) - start) / 2.0)))

    return {
        "text": expected,
        "aligned_text": None,
        "start": start,
        "end": end,
        "probability": None,
        "match_status": "missing_expected",
        "synthetic_timing": True,
        "similarity": 0.0,
    }


def analyze_matched_line_timing(
    line_text: str,
    line_words: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    expected = lyric_words(line_text)
    expected_count = len(expected)
    matched_count = sum(1 for w in line_words if not w.get("synthetic_timing"))
    missing_indices = [i for i, w in enumerate(line_words) if w.get("synthetic_timing")]
    mismatch_count = sum(1 for w in line_words if str(w.get("match_status", "")) == "mismatch")
    fuzzy_count = sum(1 for w in line_words if str(w.get("match_status", "")) == "fuzzy_match")

    starts = [float(w.get("start", 0.0)) for w in line_words if w.get("start") is not None]
    ends = [float(w.get("end", 0.0)) for w in line_words if w.get("end") is not None]
    start = min(starts) if starts else 0.0
    end = max(ends) if ends else start
    duration = max(0.0, end - start)

    durations = [
        max(0.0, float(w.get("end", w.get("start", 0.0))) - float(w.get("start", 0.0)))
        for w in line_words
    ]
    zeroish_count = sum(1 for d in durations if d <= 0.015)
    zeroish_ratio = zeroish_count / max(1, len(durations))
    probabilities = [
        float(w["probability"])
        for w in line_words
        if w.get("probability") is not None and not w.get("synthetic_timing")
    ]
    mean_probability = sum(probabilities) / len(probabilities) if probabilities else None
    very_low_probability_ratio = (
        sum(1 for p in probabilities if p <= 0.05) / len(probabilities)
        if probabilities else 0.0
    )
    low_probability_ratio = (
        sum(1 for p in probabilities if p <= 0.15) / len(probabilities)
        if probabilities else 0.0
    )

    real_words = [w for w in line_words if not w.get("synthetic_timing")]
    internal_gaps: List[float] = []
    for previous, current in zip(real_words, real_words[1:]):
        try:
            previous_end = float(previous.get("end", previous.get("start", 0.0)))
            current_start = float(current.get("start", previous_end))
            internal_gaps.append(max(0.0, current_start - previous_end))
        except Exception:
            pass
    max_internal_gap = max(internal_gaps) if internal_gaps else 0.0

    words_per_second = expected_count / max(0.01, duration) if expected_count else 0.0
    min_plausible_duration = max(0.18, expected_count * 0.09)
    missing_ratio = len(missing_indices) / max(1, expected_count)
    mismatch_ratio = mismatch_count / max(1, expected_count)

    issues: List[str] = []
    status = "GOOD"
    reliable = True

    if expected_count == 0:
        status = "EMPTY"
        reliable = False
    elif matched_count == 0:
        status = "MISSING"
        reliable = False
        issues.append("no reliable matched words")
    else:
        prefix_missing = bool(missing_indices and missing_indices == list(range(0, len(missing_indices))))
        suffix_missing = bool(missing_indices and missing_indices == list(range(expected_count - len(missing_indices), expected_count)))
        internal_missing = bool(missing_indices and not prefix_missing and not suffix_missing)

        if missing_indices:
            if prefix_missing:
                status = "PARTIAL_PREFIX_MISSING"
            elif suffix_missing:
                status = "PARTIAL_SUFFIX_MISSING"
            elif internal_missing:
                status = "PARTIAL_INTERNAL_GAP"
            else:
                status = "PARTIAL_MISSING"
            issues.append(f"missing expected words: {len(missing_indices)}")
            if missing_ratio > float(config.get("alignment_line_reliable_max_missing_ratio", 0.40)):
                reliable = False
                issues.append(f"too many missing words for timing anchor: missing_ratio={missing_ratio:.2f}")

        # A line can also be structurally collapsed even when its overall
        # duration looks plausible: stable-ts sometimes pins several consecutive
        # words to exactly the same timestamp and leaves the final one with a
        # normal duration.  That is unusable for karaoke and, more importantly,
        # makes the line a bad anchor for neighboring timing repair.
        zeroish_cluster_collapsed = (
            expected_count >= 3
            and zeroish_ratio >= float(config.get("alignment_line_zeroish_cluster_ratio", 0.60))
            and duration < max(1.80, expected_count * 0.45)
        )
        collapsed = (
            (expected_count == 1 and (duration <= 0.04 or zeroish_ratio >= 1.0))
            or (
                expected_count >= 2
                and (
                    duration < min_plausible_duration
                    or words_per_second > 14.0
                    or (zeroish_ratio >= 0.65 and duration < max(2.00, expected_count * 0.35))
                    or zeroish_cluster_collapsed
                )
            )
        )
        if collapsed:
            status = "COLLAPSED" if status == "GOOD" else f"{status}_COLLAPSED"
            reliable = False
            issues.append(
                f"collapsed timing: duration={duration:.2f}s, zeroish_ratio={zeroish_ratio:.2f}, words_per_second={words_per_second:.2f}"
            )

        if mean_probability is not None and mean_probability < 0.10:
            issues.append(f"low mean probability: {mean_probability:.3f}")
            if status == "GOOD":
                status = "LOW_CONFIDENCE"
            if mean_probability < 0.05 and (zeroish_ratio >= 0.50 or duration < max(1.50, expected_count * 0.25)):
                reliable = False
                issues.append("low-confidence timing is not usable as an anchor")


        # Forced alignment can preserve the exact lyric token sequence even
        # when the singer omits/holds/replaces material.  In that situation the
        # failure signal is often temporal rather than textual: one low-
        # confidence word is placed many seconds after its neighbors.  Such a
        # sparse line must not become a hard timing anchor for the rest of the
        # song.
        sparse_gap_limit = float(config.get("alignment_line_sparse_gap_seconds", 3.5))
        sparse_extreme_limit = float(config.get("alignment_line_extreme_gap_seconds", 8.0))
        sparse_low_confidence = float(config.get("alignment_line_sparse_max_mean_probability", 0.20))
        sparse = max_internal_gap >= sparse_gap_limit and (
            max_internal_gap >= sparse_extreme_limit
            or mean_probability is None
            or mean_probability <= sparse_low_confidence
        )
        if sparse:
            reliable = False
            if status == "GOOD":
                status = "SPARSE_TIMING"
            elif "SPARSE" not in status:
                status = f"{status}_SPARSE"
            issues.append(
                f"sparse timing: max_internal_gap={max_internal_gap:.2f}s"
                + (f", mean_probability={mean_probability:.3f}" if mean_probability is not None else "")
            )

        if mismatch_count:
            issues.append(f"mismatched words: {mismatch_count}")
            if mismatch_ratio > float(config.get("alignment_line_reliable_max_mismatch_ratio", 0.25)):
                reliable = False
                issues.append(f"too many mismatched words for timing anchor: mismatch_ratio={mismatch_ratio:.2f}")
            if status == "GOOD":
                status = "HAS_MISMATCH"

    return {
        "status": status,
        "timing_reliable": reliable,
        "expected_words": expected_count,
        "matched_words": matched_count,
        "missing_words": len(missing_indices),
        "fuzzy_words": fuzzy_count,
        "mismatch_words": mismatch_count,
        "missing_ratio": missing_ratio,
        "mismatch_ratio": mismatch_ratio,
        "start": start,
        "end": end,
        "duration": duration,
        "zeroish_word_ratio": zeroish_ratio,
        "mean_probability": mean_probability,
        "very_low_probability_ratio": very_low_probability_ratio,
        "low_probability_ratio": low_probability_ratio,
        "words_per_second": words_per_second,
        "max_internal_gap": max_internal_gap,
        "issues": issues,
    }


def redistribute_line_word_timings(line: Dict[str, Any], start: float, end: float, estimated: bool) -> None:
    line["start"] = float(start)
    line["end"] = max(float(start) + 0.01, float(end))
    line["timing_estimated"] = bool(estimated)
    words = line.get("words", []) or []
    if not words:
        return

    duration = max(0.01, float(line["end"]) - float(line["start"]))
    slot = duration / max(1, len(words))
    for i, w in enumerate(words):
        # Preserve the original forced-alignment evidence before replacing it.
        # Later diagnostics/repair passes may need to know which side of a large
        # sparse gap contained the real sung phrase.
        if "raw_start" not in w:
            try:
                w["raw_start"] = float(w.get("start", line["start"]))
            except Exception:
                w["raw_start"] = float(line["start"])
        if "raw_end" not in w:
            try:
                w["raw_end"] = float(w.get("end", w["raw_start"]))
            except Exception:
                w["raw_end"] = float(w["raw_start"])

        ws = float(line["start"]) + slot * i
        we = float(line["start"]) + slot * (i + 1)
        w["start"] = ws
        w["end"] = max(ws + 0.001, we)
        if estimated:
            w["timing_estimated"] = True
            w["timing_source"] = "estimated_line_window"


def estimate_unreliable_line_timings(lines: List[Dict[str, Any]], config: Dict[str, Any]) -> None:
    """Repair unreliable lyric lines while respecting local semantic order.

    The forced aligner can return the correct token sequence with bad temporal
    evidence: one word may be thrown many seconds into a musical gap, or a short
    repeated chant may collapse to zero duration.  Repair is deliberately local
    to the current lyric block.  Once a line has been estimated, later global
    reconciliation passes leave it alone instead of pulling it toward a distant
    neighboring section.
    """
    if not lines:
        return

    cluster_gap_seconds = max(1.0, float(config.get("alignment_repair_cluster_gap_seconds", 4.0)))
    cluster_anchor_slack = max(0.25, float(config.get("alignment_repair_anchor_slack_seconds", 1.25)))

    def needs_repair(line: Dict[str, Any]) -> bool:
        return (not bool(line.get("timing_reliable", False))) and (not bool(line.get("timing_estimated", False)))

    def expected_word_count(line: Dict[str, Any]) -> int:
        return max(
            1,
            int(
                line.get("diagnostics", {}).get("expected_words", 0)
                or len(lyric_words(str(line.get("text", ""))))
                or 1
            ),
        )

    def plausible_duration(line: Dict[str, Any]) -> float:
        count = expected_word_count(line)
        chars = sum(len(norm_word(w)) for w in lyric_words(str(line.get("text", ""))))
        # One-word chants need enough visible time to be distinguishable in
        # karaoke. Multi-word lines scale mostly with lexical size.
        minimum = 0.65 if count == 1 else 0.50
        base = max(minimum, min(5.00, count * 0.34 + chars * 0.035))
        raw_start = float(line.get("raw_start", line.get("start", 0.0)))
        raw_end = float(line.get("raw_end", line.get("end", raw_start)))
        raw_span = max(0.0, raw_end - raw_start)
        status = str(line.get("diagnostics", {}).get("status", ""))
        if "SPARSE" not in status and 0.20 <= raw_span <= max(5.0, base * 2.2):
            base = max(base, min(raw_span, base * 1.65))
        return base

    def raw_word_evidence(run: List[Dict[str, Any]]) -> List[Dict[str, float]]:
        evidence: List[Dict[str, float]] = []
        ordinal = 0
        for line in run:
            for word in line.get("words", []) or []:
                try:
                    ws = float(word.get("raw_start", word.get("start", 0.0)))
                    we = float(word.get("raw_end", word.get("end", ws)))
                except Exception:
                    ordinal += 1
                    continue
                if not math.isfinite(ws) or not math.isfinite(we):
                    ordinal += 1
                    continue
                if we < ws:
                    we = ws
                probability = word.get("probability")
                try:
                    p = float(probability) if probability is not None else 0.0
                except Exception:
                    p = 0.0
                evidence.append({
                    "ordinal": float(ordinal),
                    "start": ws,
                    "end": we,
                    "probability": p,
                })
                ordinal += 1
        return evidence

    def evidence_clusters(evidence: List[Dict[str, float]]) -> List[List[Dict[str, float]]]:
        if not evidence:
            return []
        clusters: List[List[Dict[str, float]]] = [[evidence[0]]]
        previous_end = float(evidence[0]["end"])
        for item in evidence[1:]:
            gap = float(item["start"]) - previous_end
            if gap > cluster_gap_seconds:
                clusters.append([item])
            else:
                clusters[-1].append(item)
            previous_end = max(previous_end, float(item["end"]))
        return clusters

    def average_missing_word_duration(run: List[Dict[str, Any]], desired_total: float) -> float:
        total_words = sum(expected_word_count(line) for line in run)
        return max(0.22, min(0.75, desired_total / max(1, total_words)))

    i = 0
    while i < len(lines):
        if not needs_repair(lines[i]):
            i += 1
            continue

        run_start = i
        while i < len(lines) and needs_repair(lines[i]):
            i += 1
        run_end = i

        prev_line = lines[run_start - 1] if run_start > 0 else None
        next_line = lines[run_end] if run_end < len(lines) else None
        run = lines[run_start:run_end]
        desired = [plausible_duration(line) for line in run]
        desired_total = max(0.10, sum(desired))

        raw_starts = [float(line.get("raw_start", line.get("start", 0.0))) for line in run]
        raw_ends = [float(line.get("raw_end", line.get("end", raw_starts[k]))) for k, line in enumerate(run)]
        raw_start = min(raw_starts) if raw_starts else 0.0
        raw_end = max(raw_ends) if raw_ends else raw_start

        prev_end = float(prev_line["end"]) if prev_line is not None else None
        next_start = float(next_line["start"]) if next_line is not None else None

        evidence = raw_word_evidence(run)
        clusters = evidence_clusters(evidence)
        avg_missing = average_missing_word_duration(run, desired_total)
        expected_total_words = sum(expected_word_count(line) for line in run)

        if prev_end is not None and next_start is not None:
            window_start = prev_end
            window_end = max(window_start + 0.01, next_start)
            available = max(0.01, window_end - window_start)
            use_total = min(desired_total, available)

            run_sparse = any("SPARSE" in str(line.get("diagnostics", {}).get("status", "")) for line in run)
            raw_span = max(0.0, raw_end - raw_start)
            extreme_span = run_sparse and raw_span > max(3.0, desired_total * 2.0)

            edge_slack = max(0.55, use_total * 0.45)
            near_left = raw_start <= window_start + edge_slack
            near_right = raw_end >= window_end - edge_slack

            # Sparse evidence is often a good prefix/suffix plus one outlier.
            # Decide which side owns the phrase from the cluster nearest that
            # local anchor instead of always choosing the right edge.
            if extreme_span and clusters:
                first_cluster = clusters[0]
                last_cluster = clusters[-1]
                first_start = float(first_cluster[0]["start"])
                first_end = max(float(x["end"]) for x in first_cluster)
                last_start = min(float(x["start"]) for x in last_cluster)
                last_end = max(float(x["end"]) for x in last_cluster)
                left_distance = abs(first_start - window_start)
                right_distance = abs(window_end - last_end)
                left_words = len(first_cluster)
                right_words = len(last_cluster)

                if left_distance <= right_distance + cluster_anchor_slack and left_words >= right_words:
                    missing_after = max(0, expected_total_words - left_words)
                    placed_start = window_start
                    placed_end = min(
                        window_end,
                        max(window_start + use_total, first_end + missing_after * avg_missing),
                    )
                elif right_distance < left_distance + cluster_anchor_slack:
                    missing_before = max(0, expected_total_words - right_words)
                    placed_end = window_end
                    placed_start = max(
                        window_start,
                        min(window_end - use_total, last_start - missing_before * avg_missing),
                    )
                else:
                    raw_center = (raw_start + raw_end) / 2.0
                    placed_start = raw_center - use_total / 2.0
                    placed_start = min(max(window_start, placed_start), window_end - use_total)
                    placed_end = placed_start + use_total
            elif available <= desired_total * 1.25:
                placed_start, placed_end = window_start, window_end
            elif near_right and not near_left:
                placed_end = window_end
                placed_start = max(window_start, placed_end - use_total)
            elif near_left and not near_right:
                placed_start = window_start
                placed_end = min(window_end, placed_start + use_total)
            else:
                raw_center = (raw_start + raw_end) / 2.0
                placed_start = raw_center - use_total / 2.0
                placed_start = min(max(window_start, placed_start), window_end - use_total)
                placed_end = placed_start + use_total

        elif prev_end is not None:
            # Trailing unreliable line: prefer the raw prefix attached to the
            # previous lyric anchor. A distant suffix after a large gap is an
            # outlier, not evidence that the lyric lasts through the gap.
            placed_start = prev_end
            placed_end = placed_start + desired_total
            if clusters:
                prefix = clusters[0]
                prefix_start = float(prefix[0]["start"])
                prefix_end = max(float(x["end"]) for x in prefix)
                if prefix_start <= prev_end + cluster_anchor_slack:
                    missing_after = max(0, expected_total_words - len(prefix))
                    evidence_end = prefix_end + missing_after * avg_missing
                    placed_end = max(placed_end, evidence_end)
                    # Do not let a dubious sung tail consume an arbitrary amount
                    # of music even if the raw cluster itself is unusually long.
                    max_tail = max(8.0, desired_total * 3.5)
                    placed_end = min(placed_end, placed_start + max_tail)

        elif next_start is not None:
            # Leading unreliable line: mirror the rule above and use the suffix
            # cluster attached to the following reliable lyric anchor. This is
            # especially important after an instrumental/vocalise gap.
            placed_end = next_start
            placed_start = max(0.0, placed_end - desired_total)
            if clusters:
                suffix = clusters[-1]
                suffix_start = min(float(x["start"]) for x in suffix)
                suffix_end = max(float(x["end"]) for x in suffix)
                missing_before = max(0, expected_total_words - len(suffix))
                if suffix_end >= next_start - cluster_anchor_slack:
                    placed_start = max(0.0, suffix_start - missing_before * avg_missing)
                    placed_end = next_start
                else:
                    # No evidence touches the next anchor. Preserve the compact
                    # raw region instead of stretching the lyric over silence.
                    placed_start = max(0.0, min(raw_start, next_start - desired_total))
                    placed_end = min(next_start, max(raw_end, placed_start + desired_total))
        else:
            placed_start = raw_start
            placed_end = max(placed_start + desired_total, raw_end)

        if placed_end <= placed_start + 0.01:
            placed_end = placed_start + max(0.10, desired_total)

        available_for_run = max(0.01, placed_end - placed_start)
        weight_sum = max(0.01, sum(desired))
        cursor = placed_start
        for offset, line in enumerate(run):
            weight = desired[offset]
            part = available_for_run * (weight / weight_sum)
            line_start = cursor
            line_end = placed_end if offset == len(run) - 1 else min(placed_end, cursor + part)
            redistribute_line_word_timings(line, line_start, line_end, estimated=True)
            line["timing_reliable"] = False
            line["timing_estimated"] = True
            repair_issue = "timing estimated from local raw-cluster anchors"
            issues = line.setdefault("diagnostics", {}).setdefault("issues", [])
            if repair_issue not in issues:
                issues.append(repair_issue)
            line["diagnostics"]["repair_window"] = {
                "start": placed_start,
                "end": placed_end,
                "raw_start": raw_start,
                "raw_end": raw_end,
                "previous_reliable_end": prev_end,
                "next_reliable_start": next_start,
                "raw_cluster_count": len(clusters),
            }
            cursor = line_end


def _raw_word_start(word: Dict[str, Any]) -> float:
    try:
        return float(word.get("raw_start", word.get("start", 0.0)))
    except Exception:
        return float(word.get("start", 0.0) or 0.0)


def _raw_word_end(word: Dict[str, Any]) -> float:
    start = _raw_word_start(word)
    try:
        return max(start, float(word.get("raw_end", word.get("end", start))))
    except Exception:
        return max(start, float(word.get("end", start) or start))


def _word_probability(word: Dict[str, Any]) -> float:
    try:
        value = word.get("probability")
        return float(value) if value is not None else 0.0
    except Exception:
        return 0.0


def _lexical_word_duration(text: str) -> float:
    letters = max(1, len(norm_word(text)))
    return max(0.22, min(1.25, 0.18 + letters * 0.085))


def _line_weight(line: Dict[str, Any]) -> float:
    words = lyric_words(str(line.get("text", "")))
    if not words:
        return 1.0
    return max(0.45, sum(_lexical_word_duration(w) for w in words))


def _raw_line_was_unreliable(line: Dict[str, Any]) -> bool:
    diag = line.get("diagnostics", {}) or {}
    raw = diag.get("raw_diagnostics") if isinstance(diag.get("raw_diagnostics"), dict) else diag
    if not bool(raw.get("timing_reliable", line.get("timing_reliable", False))):
        return True
    status = str(raw.get("status", ""))
    return any(token in status for token in ("COLLAPSED", "SPARSE", "LOW_CONFIDENCE", "MISSING", "PARTIAL"))


def repair_repeated_single_word_lines(lines: List[Dict[str, Any]], config: Dict[str, Any]) -> None:
    """Repair consecutive identical one-word lyric lines without losing repeats.

    There are two common forced-alignment failures:
      * the first repeat has a usable onset while the last repeat collapses to
        near-zero duration (keep the distinct onsets and infer the last duration
        from the preceding repeat);
      * the whole repeated chant is collapsed at the *right* edge of the space
        between surrounding reliable lines (spread the chant backwards over the
        available vocal window).

    The rule is lexical/temporal and applies to any repeated one-word lines.
    """
    if len(lines) < 2:
        return

    min_visible = max(0.10, float(config.get("alignment_repeat_min_visible_seconds", 0.18)))
    normal_last = max(min_visible, float(config.get("alignment_repeat_last_word_seconds", 0.65)))
    late_onset = max(0.40, float(config.get("alignment_repeat_late_onset_seconds", 0.75)))
    compressed_ratio = max(0.10, min(0.80, float(config.get("alignment_repeat_compressed_ratio", 0.45))))

    def single_norm(line: Dict[str, Any]) -> Optional[str]:
        words = lyric_words(str(line.get("text", "")))
        if len(words) != 1:
            return None
        return norm_word(words[0]) or None

    i = 0
    while i < len(lines) - 1:
        token = single_norm(lines[i])
        if not token:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and single_norm(lines[j]) == token:
            j += 1
        if j - i < 2:
            i = j
            continue

        run = lines[i:j]
        if not any(_raw_line_was_unreliable(line) or bool(line.get("timing_estimated", False)) for line in run):
            i = j
            continue

        raw_starts: List[float] = []
        raw_ends: List[float] = []
        raw_probs: List[float] = []
        for line in run:
            words = line.get("words", []) or []
            if words:
                raw_starts.append(_raw_word_start(words[0]))
                raw_ends.append(_raw_word_end(words[-1]))
                raw_probs.append(_word_probability(words[0]))
            else:
                raw_starts.append(float(line.get("raw_start", line.get("start", 0.0))))
                raw_ends.append(float(line.get("raw_end", line.get("end", raw_starts[-1]))))
                raw_probs.append(0.0)

        if any(not math.isfinite(x) for x in raw_starts + raw_ends):
            i = j
            continue
        if any(raw_starts[k + 1] < raw_starts[k] - 1e-6 for k in range(len(raw_starts) - 1)):
            i = j
            continue

        prev_end = float(lines[i - 1]["end"]) if i > 0 else max(0.0, raw_starts[0])
        next_start = float(lines[j]["start"]) if j < len(lines) else None
        first_start = max(prev_end, raw_starts[0])
        raw_group_end = max(raw_ends)
        raw_span = max(0.0, raw_group_end - raw_starts[0])
        available = (next_start - prev_end) if next_start is not None else None
        low_conf_ratio = sum(1 for p in raw_probs if p <= 0.15) / max(1, len(raw_probs))

        # A chant whose raw onsets arrive very late and occupy only a tiny part
        # of the space before the next reliable line was usually collapsed onto
        # the next phrase. Spread it over that local window. This fixes cases
        # like two short repeated calls that are sung before the timestamps.
        use_full_window = (
            next_start is not None
            and available is not None
            and available >= min_visible * len(run)
            and raw_starts[0] - prev_end >= late_onset
            and raw_span <= available * compressed_ratio
            and low_conf_ratio >= 0.50
        )

        if use_full_window:
            boundaries = [prev_end + available * k / len(run) for k in range(len(run) + 1)]
            mode = "spread_collapsed_repeat_over_neighbor_window"
        else:
            boundaries = [first_start]
            for k in range(1, len(run)):
                candidate = max(boundaries[-1] + min_visible, raw_starts[k])
                if next_start is not None:
                    remaining = len(run) - k
                    candidate = min(candidate, next_start - min_visible * remaining)
                boundaries.append(max(boundaries[-1] + 0.01, candidate))

            # If the last repeat collapsed, infer its hold from earlier repeats
            # rather than giving it a fixed 0.65 s and making it disappear halfway.
            reference_durations = [
                max(0.0, raw_ends[k] - raw_starts[k])
                for k in range(max(0, len(run) - 1))
                if raw_ends[k] - raw_starts[k] >= min_visible
            ]
            reference = max(reference_durations) if reference_durations else normal_last
            final_duration = max(normal_last, reference)
            final_end = max(boundaries[-1] + min_visible, raw_ends[-1], boundaries[-1] + final_duration)
            if next_start is not None:
                final_end = min(final_end, next_start)

                # When the last item of an unreliable repeat group collapsed, a
                # short residual gap before the next lyric onset is ambiguous:
                # it can be the missing tail of the repeated sung word rather
                # than actual silence.  Bridge only a small, locally-scaled gap
                # and only for a genuinely unreliable final repeat.  This is
                # token-agnostic and applies to any repeated one-word chant.
                residual_gap = max(0.0, next_start - final_end)
                bridge_cap = max(
                    min_visible,
                    float(config.get("alignment_repeat_bridge_gap_seconds", 1.25)),
                )
                bridge_fraction = min(1.0, max(0.25, float(config.get("alignment_repeat_bridge_reference_fraction", 0.75))))
                reference_scaled_cap = max(bridge_cap, reference * 1.25)
                final_repeat_unreliable = (
                    _raw_line_was_unreliable(run[-1])
                    or raw_probs[-1] <= 0.15
                    or max(0.0, raw_ends[-1] - raw_starts[-1]) < min_visible
                )
                if (
                    final_repeat_unreliable
                    and low_conf_ratio >= 0.50
                    and 0.0 < residual_gap <= reference_scaled_cap
                ):
                    # Do not blindly fill the entire silence. Extend by a
                    # fraction of the duration demonstrated by the preceding
                    # repeat, capped by the actual residual gap. This keeps the
                    # subtitle alive through a likely sustained repeat while
                    # preserving a real short pause when one exists.
                    bridge = min(residual_gap, max(min_visible, reference * bridge_fraction))
                    final_end += bridge
                    mode = "preserve_repeat_onsets_and_bridge_short_residual_gap"
                else:
                    mode = "preserve_repeat_onsets_and_infer_final_hold"
            else:
                mode = "preserve_repeat_onsets_and_infer_final_hold"
            if final_end <= boundaries[-1] + 0.01:
                final_end = boundaries[-1] + min_visible
            boundaries.append(final_end)

        for offset, line in enumerate(run):
            line_start = float(boundaries[offset])
            line_end = max(line_start + 0.01, float(boundaries[offset + 1]))
            redistribute_line_word_timings(line, line_start, line_end, estimated=True)
            line["timing_reliable"] = False
            line["timing_estimated"] = True
            issues = line.setdefault("diagnostics", {}).setdefault("issues", [])
            issue = "repeated single-word timing repaired from local chant window"
            if issue not in issues:
                issues.append(issue)
            line["diagnostics"]["repeat_repair"] = {
                "token": token,
                "group_size": len(run),
                "raw_starts": raw_starts,
                "raw_ends": raw_ends,
                "previous_line_end": prev_end,
                "next_line_start": next_start,
                "mode": mode,
                "start": line_start,
                "end": line_end,
            }

        i = j


def repair_adjacent_line_boundary_holds(lines: List[Dict[str, Any]], config: Dict[str, Any]) -> None:
    """Repair ambiguous sustained-word boundaries between adjacent lyric lines.

    Forced alignment often assigns a sung hold to the wrong side of a line
    boundary. Two generic signatures are handled:
      1) previous final word is low-confidence, next first word is zero-duration
         and low-confidence, and a later word in the next line starts much later;
      2) next line's first word consumes an implausibly long interval while the
         previous line ends exactly at that word's onset.

    The result moves the *line boundary*; it never allows a semantic range to
    split a word later in the pipeline.
    """
    if len(lines) < 2:
        return

    delayed_gap = max(0.60, float(config.get("alignment_boundary_delayed_prefix_gap_seconds", 1.20)))
    low_conf = max(0.01, float(config.get("alignment_boundary_low_probability", 0.08)))
    zeroish = max(0.01, float(config.get("alignment_boundary_zeroish_seconds", 0.06)))
    long_first_min = max(1.0, float(config.get("alignment_boundary_long_first_word_seconds", 2.20)))
    long_first_ratio = max(1.5, float(config.get("alignment_boundary_long_first_word_ratio", 2.8)))

    for idx in range(len(lines) - 1):
        prev_line = lines[idx]
        next_line = lines[idx + 1]
        prev_words = prev_line.get("words", []) or []
        next_words = next_line.get("words", []) or []
        if not prev_words or not next_words:
            continue

        pw = prev_words[-1]
        nw = next_words[0]
        prev_p = _word_probability(pw)
        next_p = _word_probability(nw)
        prev_end = float(prev_line.get("end", _raw_word_end(pw)))
        next_start = float(next_line.get("start", _raw_word_start(nw)))
        raw_next_first_start = _raw_word_start(nw)
        raw_next_first_end = _raw_word_end(nw)
        raw_next_first_duration = max(0.0, raw_next_first_end - raw_next_first_start)

        # Pattern 1: the next line begins with a zero-ish weak token, then has a
        # delayed non-zero token. Put the weak prefix immediately before that
        # delayed token and extend the previous held word to the new boundary.
        delayed_index: Optional[int] = None
        for k in range(1, len(next_words)):
            ws = _raw_word_start(next_words[k])
            we = _raw_word_end(next_words[k])
            if ws - raw_next_first_end >= delayed_gap and we - ws >= zeroish:
                delayed_index = k
                break

        if (
            delayed_index is not None
            and prev_p <= low_conf
            and next_p <= low_conf
            and raw_next_first_duration <= zeroish
        ):
            delayed_start = _raw_word_start(next_words[delayed_index])
            prefix_words = next_words[:delayed_index]
            prefix_duration = sum(_lexical_word_duration(str(w.get("text", ""))) for w in prefix_words)
            new_boundary = max(prev_end, delayed_start - prefix_duration)
            new_boundary = min(delayed_start - 0.02, new_boundary)
            if new_boundary > prev_end + 0.10:
                pw["end"] = new_boundary
                pw["timing_estimated"] = True
                pw["timing_source"] = "boundary_hold_extended_previous_word"
                prev_line["end"] = new_boundary
                prev_line["timing_estimated"] = True

                # Prefix occupies the short interval immediately before the first
                # delayed token. The rest of the next line is distributed only
                # inside its existing line end, so distant outliers stay ignored.
                cursor = new_boundary
                prefix_total = max(0.02, delayed_start - new_boundary)
                prefix_weights = [_lexical_word_duration(str(w.get("text", ""))) for w in prefix_words]
                weight_sum = max(0.01, sum(prefix_weights))
                for w, weight in zip(prefix_words, prefix_weights):
                    ws = cursor
                    we = delayed_start if w is prefix_words[-1] else cursor + prefix_total * weight / weight_sum
                    w["start"] = ws
                    w["end"] = max(ws + 0.01, we)
                    w["timing_estimated"] = True
                    w["timing_source"] = "boundary_hold_delayed_prefix"
                    cursor = w["end"]

                suffix = next_words[delayed_index:]
                suffix_start = delayed_start
                suffix_end = max(suffix_start + 0.01, float(next_line.get("end", suffix_start + 0.01)))
                suffix_weights = [_lexical_word_duration(str(w.get("text", ""))) for w in suffix]
                suffix_sum = max(0.01, sum(suffix_weights))
                cursor = suffix_start
                for n, (w, weight) in enumerate(zip(suffix, suffix_weights)):
                    ws = cursor
                    we = suffix_end if n == len(suffix) - 1 else cursor + (suffix_end - suffix_start) * weight / suffix_sum
                    w["start"] = ws
                    w["end"] = max(ws + 0.01, we)
                    w["timing_estimated"] = True
                    w["timing_source"] = "boundary_hold_suffix_redistributed"
                    cursor = w["end"]

                next_line["start"] = new_boundary
                next_line["timing_estimated"] = True
                issue = "adjacent line boundary moved after low-confidence held word"
                for line in (prev_line, next_line):
                    issues = line.setdefault("diagnostics", {}).setdefault("issues", [])
                    if issue not in issues:
                        issues.append(issue)
                    line["diagnostics"]["boundary_hold_repair"] = {
                        "mode": "delayed_weak_prefix",
                        "old_boundary": next_start,
                        "new_boundary": new_boundary,
                        "delayed_word_start": delayed_start,
                    }
                continue

        # Pattern 2: the first word of the next line is an extreme duration
        # outlier. A line transition inside such a hold is acoustically ambiguous;
        # split the combined two-word hold by lexical weight instead of trusting
        # the forced onset blindly.
        expected_next = _lexical_word_duration(str(nw.get("text", "")))
        if (
            abs(prev_end - next_start) <= 0.08
            and raw_next_first_duration >= long_first_min
            and raw_next_first_duration >= expected_next * long_first_ratio
        ):
            prev_word_start = float(pw.get("start", _raw_word_start(pw)))
            combined_end = float(nw.get("end", raw_next_first_end))
            if combined_end - prev_word_start >= 0.80:
                prev_weight = _lexical_word_duration(str(pw.get("text", "")))
                next_weight = expected_next
                split = prev_word_start + (combined_end - prev_word_start) * prev_weight / max(0.01, prev_weight + next_weight)
                split = min(combined_end - 0.10, max(prev_word_start + 0.10, split))
                if split > prev_end + 0.10:
                    pw["end"] = split
                    pw["timing_estimated"] = True
                    pw["timing_source"] = "boundary_hold_balanced_long_next_word"
                    nw["start"] = split
                    nw["timing_estimated"] = True
                    nw["timing_source"] = "boundary_hold_balanced_long_next_word"
                    prev_line["end"] = split
                    next_line["start"] = split
                    prev_line["timing_estimated"] = True
                    next_line["timing_estimated"] = True
                    issue = "adjacent line boundary balanced across implausibly long first word"
                    for line in (prev_line, next_line):
                        issues = line.setdefault("diagnostics", {}).setdefault("issues", [])
                        if issue not in issues:
                            issues.append(issue)
                        line["diagnostics"]["boundary_hold_repair"] = {
                            "mode": "balance_long_first_word",
                            "old_boundary": next_start,
                            "new_boundary": split,
                            "combined_end": combined_end,
                        }


def repair_uncertain_terminal_lines_before_nonlyrical_blocks(
    verses: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> None:
    """Give highly uncertain terminal lyrics a conservative sung-tail allowance.

    Forced alignment has no reliable right-hand lyric anchor when a lyrical
    section is followed by a metadata-only/empty musical block.  If almost the
    entire final lyric line has very low word confidence, its synthesized final
    word can end slightly before the singer actually releases it.

    This repair is deliberately narrow and content-agnostic:
      * current block must contain lyrics;
      * the immediately following block must contain no lyrics;
      * the final line must already be estimated/unreliable;
      * a large majority of its raw words must have very low confidence.

    Under those conditions only, extend the final word by a small amount derived
    from lexical duration and uncertainty.  The semantic timeline is rebuilt
    from the resulting word edge later, so no R boundary can cut through it.
    """
    if len(verses) < 2:
        return

    low_probability = max(0.001, float(config.get("alignment_terminal_low_probability", 0.10)))
    low_ratio_required = min(1.0, max(0.50, float(config.get("alignment_terminal_low_confidence_ratio", 0.67))))
    min_tail = max(0.05, float(config.get("alignment_terminal_uncertainty_min_seconds", 0.30)))
    max_tail = max(min_tail, float(config.get("alignment_terminal_uncertainty_max_seconds", 0.90)))

    for idx in range(len(verses) - 1):
        current = verses[idx]
        following = verses[idx + 1]
        if not block_has_lyric_text(current) or block_has_lyric_text(following):
            continue

        lines = current.get("lines", []) or []
        if not lines:
            continue
        line = lines[-1]
        words = line.get("words", []) or []
        if not words:
            continue

        # Do not alter a normally aligned terminal phrase merely because a
        # musical block follows it.  This pass is for severe forced-alignment
        # uncertainty only.
        if not (bool(line.get("timing_estimated", False)) or _raw_line_was_unreliable(line)):
            continue

        probs = [_word_probability(w) for w in words]
        low_count = sum(1 for p in probs if p <= low_probability)
        low_ratio = low_count / max(1, len(probs))
        if low_ratio < low_ratio_required:
            continue

        last = words[-1]
        last_start = float(last.get("start", line.get("start", 0.0)))
        last_end = float(last.get("end", last_start))
        current_duration = max(0.01, last_end - last_start)
        lexical = _lexical_word_duration(str(last.get("text", "")))

        # More uncertainty permits a somewhat larger release tail, but it is
        # always tightly capped.  This avoids swallowing a true instrumental
        # pause while still preventing an estimated terminal word from being
        # visibly cut off just as the next semantic range begins.
        uncertainty = min(1.0, max(0.0, low_ratio))
        tail = lexical * (1.0 + 0.50 * uncertainty)
        tail = min(max_tail, max(min_tail, tail))

        new_end = last_end + tail
        last["end"] = new_end
        last["timing_estimated"] = True
        last["timing_source"] = "terminal_low_confidence_release_tail"
        line["end"] = new_end
        line["timing_estimated"] = True
        line.setdefault("diagnostics", {}).setdefault("issues", []).append(
            "terminal low-confidence lyric given conservative release tail before non-lyrical block"
        )
        line["diagnostics"]["terminal_release_tail"] = {
            "low_confidence_ratio": low_ratio,
            "low_probability_threshold": low_probability,
            "previous_end": last_end,
            "new_end": new_end,
            "tail_seconds": tail,
            "previous_word_duration": current_duration,
        }


def repair_final_song_word_release_before_terminal_nonlyrical_tail(
    verses: List[Dict[str, Any]],
) -> None:
    """Give the final sung word a small universal release hold.

    This is intentionally content-agnostic.  The rule applies only when the
    final lyrical block is followed exclusively by one or more non-lyrical
    blocks (for example a terminal metadata marker such as [End] or an outro
    tail).  With no following sung word, forced alignment has no right-hand
    lexical anchor and frequently ends a sustained final vowel too tightly.

    The adjustment is conservative:
      * only the last word of the last lyrical line can change;
      * an already-long word is left untouched;
      * the extra time is capped;
      * an earlier low-confidence terminal-tail repair is not extended again.

    Timeline construction later uses the repaired lyric end, so the following
    R boundary moves with the word and can never cut through it.
    """
    if len(verses) < 2:
        return

    lyrical_indices = [i for i, verse in enumerate(verses) if block_has_lyric_text(verse)]
    if not lyrical_indices:
        return
    last_lyric_index = lyrical_indices[-1]
    if last_lyric_index >= len(verses) - 1:
        return
    if any(block_has_lyric_text(verse) for verse in verses[last_lyric_index + 1:]):
        return

    verse = verses[last_lyric_index]
    lines = verse.get("lines", []) or []
    if not lines:
        return
    line = lines[-1]
    words = line.get("words", []) or []
    if not words:
        return

    last = words[-1]
    if str(last.get("timing_source", "")) == "terminal_low_confidence_release_tail":
        return

    try:
        start = float(last.get("start", line.get("start", 0.0)))
        end = float(last.get("end", start))
    except Exception:
        return
    duration = max(0.0, end - start)
    desired_extra = max(0.0, TERMINAL_SONG_WORD_MIN_KARAOKE_SECONDS - duration)
    extra = min(TERMINAL_SONG_WORD_MAX_EXTRA_SECONDS, desired_extra)
    if extra <= 1e-6:
        return

    new_end = end + extra
    last["end"] = new_end
    last["timing_estimated"] = True
    last["timing_source"] = "terminal_song_word_release_hold"
    line["end"] = new_end
    line["timing_estimated"] = True
    diagnostics = line.setdefault("diagnostics", {})
    issues = diagnostics.setdefault("issues", [])
    issue = "final song word given conservative release hold before terminal non-lyrical tail"
    if issue not in issues:
        issues.append(issue)
    diagnostics["terminal_song_word_release_hold"] = {
        "previous_end": end,
        "new_end": new_end,
        "previous_duration": duration,
        "extra_seconds": extra,
        "minimum_target_duration": TERMINAL_SONG_WORD_MIN_KARAOKE_SECONDS,
        "maximum_extra_seconds": TERMINAL_SONG_WORD_MAX_EXTRA_SECONDS,
    }


def repair_leading_unreliable_lines_across_lyric_blocks(
    verses: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> None:
    """Use the previous lyric block as a left anchor for collapsed leading lines.

    Per-block repair cannot see the preceding section. If a new lyrical block
    begins with one or more collapsed lines, forced alignment may pin all of them
    to the first reliable line on the right. When there is *no explicit
    metadata-only musical block between sections*, the previous lyric end is a
    valid local anchor. Spread only the unreliable leading run over that local
    window. Explicit instrumental/empty blocks remain hard barriers.
    """
    previous_lyric: Optional[Dict[str, Any]] = None
    max_window = max(2.0, float(config.get("alignment_cross_block_leading_max_seconds", 6.0)))

    for verse in verses:
        if not block_has_lyric_text(verse):
            previous_lyric = None
            continue
        lines = verse.get("lines", []) or []
        if not lines:
            previous_lyric = verse
            continue

        if previous_lyric is not None:
            def _leading_collapsed_candidate(line: Dict[str, Any]) -> bool:
                diag = line.get("diagnostics", {}) or {}
                raw = diag.get("raw_diagnostics") if isinstance(diag.get("raw_diagnostics"), dict) else diag
                status = str(raw.get("status", ""))
                if any(token in status for token in ("COLLAPSED", "MISSING", "PARTIAL")):
                    return True
                try:
                    raw_span = float(line.get("raw_end", line.get("end", 0.0))) - float(line.get("raw_start", line.get("start", 0.0)))
                except Exception:
                    raw_span = 999.0
                expected_words = max(1, len(lyric_words(str(line.get("text", "")))))
                return raw_span <= max(0.10, expected_words * 0.07) and not bool(raw.get("timing_reliable", False))

            run_end = 0
            while run_end < len(lines) and _leading_collapsed_candidate(lines[run_end]):
                run_end += 1
            if 0 < run_end < len(lines):
                prev_lines = previous_lyric.get("lines", []) or []
                if prev_lines:
                    left = float(prev_lines[-1].get("end", previous_lyric.get("end", 0.0)))
                    right = float(lines[run_end].get("start", left))
                    available = right - left
                    run = lines[:run_end]
                    weights = [_line_weight(line) for line in run]
                    desired = sum(weights)
                    raw_starts = [float(line.get("raw_start", line.get("start", right))) for line in run]
                    raw_ends = [float(line.get("raw_end", line.get("end", right))) for line in run]
                    raw_span = max(raw_ends) - min(raw_starts) if raw_starts else 0.0

                    # Keep this local. A very large unexplained pause should be a
                    # musical gap, not silently filled with lyrics; modest gaps or
                    # visibly collapsed evidence are safe to reconstruct.
                    if (
                        available > 0.10
                        and available <= max(max_window, desired * 2.25)
                        and (raw_span < desired * 0.75 or available <= desired * 1.8)
                    ):
                        cursor = left
                        total_weight = max(0.01, sum(weights))
                        for n, (line, weight) in enumerate(zip(run, weights)):
                            line_start = cursor
                            line_end = right if n == len(run) - 1 else cursor + available * weight / total_weight
                            redistribute_line_word_timings(line, line_start, line_end, estimated=True)
                            line["timing_reliable"] = False
                            line["timing_estimated"] = True
                            issues = line.setdefault("diagnostics", {}).setdefault("issues", [])
                            issue = "leading collapsed line repaired from adjacent lyric-block anchors"
                            if issue not in issues:
                                issues.append(issue)
                            line["diagnostics"]["cross_block_leading_repair"] = {
                                "left_anchor": left,
                                "right_anchor": right,
                                "run_size": len(run),
                                "raw_span": raw_span,
                            }
                            cursor = line_end

        previous_lyric = verse


def refresh_line_and_verse_bounds(verses: List[Dict[str, Any]]) -> None:
    """Recompute line/verse bounds after timing repair without changing words."""
    for verse in verses:
        if not block_has_lyric_text(verse):
            continue
        lines = verse.get("lines", []) or []
        for line in lines:
            words = line.get("words", []) or []
            if words:
                line["start"] = min(float(w.get("start", line.get("start", 0.0))) for w in words)
                line["end"] = max(float(w.get("end", line.get("end", line["start"]))) for w in words)
                line["end"] = max(line["start"] + 0.01, line["end"])
        if lines:
            verse["start"] = min(float(line["start"]) for line in lines)
            verse["end"] = max(float(line["end"]) for line in lines)
            verse["duration"] = max(0.01, verse["end"] - verse["start"])


def actual_word_duration(word: Dict[str, Any]) -> float:
    return max(0.0, float(word.get("end", 0.0)) - float(word.get("start", 0.0)))


def line_timing_bounds_from_words(words: List[Dict[str, Any]], default_start: float = 0.0) -> Tuple[float, float]:
    starts = [float(w["start"]) for w in words if w.get("start") is not None]
    ends = [float(w["end"]) for w in words if w.get("end") is not None]
    if starts and ends:
        start = min(starts)
        end = max(ends)
        return start, max(start + 0.01, end)
    return default_start, default_start + 0.01


def sanitize_matched_line_word_timings(line_text: str, words: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Repair unusable word anchors without changing normal stable-ts timings.

    stable-ts remains the timing source. This pass only rewrites words that are
    structurally unusable for karaoke: a later matched word inside the same lyric
    line has a huge gap after the previous word, zero/near-zero duration, and low
    alignment confidence. Such anchors usually come from a reverb/echo/end-tail
    match and would otherwise make one subtitle line stretch across unrelated
    song blocks.
    """
    report = {
        "line_text": line_text,
        "repaired_words": [],
        "checked_words": len(words),
    }
    if len(words) < 2:
        return report

    far_gap_seconds = 2.75
    far_gap_without_probability_seconds = 7.50
    zeroish_seconds = 0.04
    low_probability = 0.15

    previous_end = None
    for i, word in enumerate(words):
        try:
            start = float(word.get("start", 0.0))
            end = float(word.get("end", start))
        except Exception:
            previous_end = previous_end if previous_end is not None else 0.0
            continue

        if previous_end is None:
            previous_end = max(start, end)
            continue

        duration = max(0.0, end - start)
        gap = start - previous_end
        probability = word.get("probability")
        try:
            probability_value = float(probability) if probability is not None else None
        except Exception:
            probability_value = None

        confidence_bad = (probability_value is not None and probability_value <= low_probability)
        confidence_unknown_but_gap_extreme = probability_value is None and gap >= far_gap_without_probability_seconds
        very_low_probability = probability_value is not None and probability_value <= 0.05
        extreme_low_confidence_jump = (
            gap >= 3.0
            and duration <= 1.50
            and very_low_probability
        )
        should_repair = (
            (
                gap >= far_gap_seconds
                and duration <= zeroish_seconds
                and (confidence_bad or confidence_unknown_but_gap_extreme)
            )
            or extreme_low_confidence_jump
        )

        if should_repair:
            new_start = previous_end
            # For an extreme low-confidence jump, the gap is evidence of an
            # alignment failure, not evidence that the word itself lasts for
            # several seconds.  Use lexical duration only; otherwise a 20s bad
            # jump can turn into an artificial 4.5s subtitle word.
            duration_gap_hint = 0.0 if extreme_low_confidence_jump else gap
            new_duration = estimate_sanitized_word_duration(str(word.get("text", "")), duration_gap_hint)
            new_end = min(start, new_start + new_duration) if start > new_start + 0.05 else new_start + new_duration
            new_end = max(new_start + 0.05, new_end)
            word["original_start"] = start
            word["original_end"] = end
            word["original_probability"] = probability
            word["start"] = new_start
            word["end"] = new_end
            word["timing_sanitized"] = True
            word["timing_source"] = "sanitized_far_gap_zero_duration_word"
            word["timing_sanitizer_reason"] = (
                f"gap={gap:.3f}s duration={duration:.3f}s probability={probability_value}"
            )
            report["repaired_words"].append({
                "word_index": i,
                "text": word.get("text"),
                "original_start": start,
                "original_end": end,
                "new_start": new_start,
                "new_end": new_end,
                "gap": gap,
                "duration": duration,
                "probability": probability_value,
            })
            previous_end = new_end
        else:
            previous_end = max(previous_end, end)

    return report


def estimate_sanitized_word_duration(word_text: str, gap_to_original: float) -> float:
    letters = max(1, len(norm_word(word_text)))
    base = max(0.25, min(1.60, letters * 0.11))
    if gap_to_original >= 3.0:
        base = max(base, min(4.50, gap_to_original * 0.22))
    return max(0.05, min(4.50, base))

def build_line_candidate_from_start(
    expected_line_words: List[str],
    words: List[Dict[str, Any]],
    actual_start: int,
    expected_offset: int,
    config: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Build one monotonic candidate line match.

    The candidate contains only real word matches. Missing lyric words are
    added later as synthetic subtitle words. No mismatch pair is allowed here:
    a bad actual span should lose to a later good candidate instead of being
    used as timing evidence.
    """
    if not expected_line_words or actual_start >= len(words):
        return None

    threshold = float(config.get("alignment_match_similarity_threshold", 0.72))
    lookahead = max(3, int(config.get("alignment_line_candidate_lookahead_words", config.get("alignment_match_lookahead_words", 5))))
    max_extra = max(4, int(config.get("alignment_line_candidate_max_extra_words", len(expected_line_words) + lookahead)))
    max_actual_end = min(len(words), actual_start + len(expected_line_words) + max_extra)

    actual_index = actual_start
    pairs: List[Dict[str, Any]] = []
    fuzzy = 0
    for expected_index in range(expected_offset, len(expected_line_words)):
        found: Optional[Tuple[int, float, str]] = None
        search_end = min(max_actual_end, actual_index + lookahead + 1)
        for j in range(actual_index, search_end):
            ok, sim, status = words_are_match(expected_line_words[expected_index], str(words[j].get("text", "")), threshold)
            if ok:
                found = (j, sim, status)
                break
        if found is None:
            continue
        j, sim, status = found
        pairs.append({
            "expected_index": expected_index,
            "actual_index": j,
            "expected": expected_line_words[expected_index],
            "actual": words[j],
            "similarity": sim,
            "match_status": status,
        })
        if status == "fuzzy_match":
            fuzzy += 1
        actual_index = j + 1

    if not pairs:
        return None

    expected_count = len(expected_line_words)
    matched_count = len(pairs)
    first_actual = int(pairs[0]["actual_index"])
    last_actual = int(pairs[-1]["actual_index"])
    starts = [float(p["actual"].get("start", 0.0)) for p in pairs]
    ends = [float(p["actual"].get("end", 0.0)) for p in pairs]
    duration = max(0.0, max(ends) - min(starts)) if starts and ends else 0.0
    zeroish = sum(1 for p in pairs if actual_word_duration(p["actual"]) <= 0.035)
    zeroish_ratio = zeroish / max(1, matched_count)
    probabilities = [float(p["actual"].get("probability")) for p in pairs if p["actual"].get("probability") is not None]
    mean_probability = (sum(probabilities) / len(probabilities)) if probabilities else None
    matched_ratio = matched_count / max(1, expected_count)
    missing_count = expected_count - matched_count

    # Collapsed clusters can contain many exact words, but they are not useful
    # timing anchors. Penalize them hard so a later plausible candidate wins.
    words_per_second = matched_count / max(0.01, duration)
    collapsed = False
    if matched_count >= 2:
        if duration < max(0.20, 0.08 * matched_count):
            collapsed = True
        if words_per_second > 14.0:
            collapsed = True
        if zeroish_ratio >= 0.80 and duration < max(1.00, 0.14 * matched_count):
            collapsed = True

    # Prefer complete, plausible, local matches, but allow partial lines.
    score = 100.0 * matched_ratio
    score -= 4.0 * expected_offset
    score -= 1.5 * max(0, first_actual - actual_start)
    score -= 3.0 * missing_count
    if fuzzy:
        score -= 1.0 * fuzzy
    if mean_probability is not None and mean_probability < 0.04:
        score -= 8.0
    elif mean_probability is not None and mean_probability < 0.10:
        score -= 3.0
    if duration >= max(0.40, 0.12 * matched_count):
        score += 10.0
    if collapsed:
        score -= 90.0

    return {
        "score": score,
        "expected_count": expected_count,
        "matched_count": matched_count,
        "missing_count": missing_count,
        "matched_ratio": matched_ratio,
        "expected_offset": expected_offset,
        "first_actual": first_actual,
        "last_actual": last_actual,
        "new_cursor": last_actual + 1,
        "duration": duration,
        "zeroish_word_ratio": zeroish_ratio,
        "mean_probability": mean_probability,
        "collapsed_candidate": collapsed,
        "pairs": pairs,
    }


def find_best_line_candidate(
    expected_line_words: List[str],
    words: List[Dict[str, Any]],
    cursor: int,
    config: Dict[str, Any],
    allow_long_start_gap: bool = False,
) -> Optional[Dict[str, Any]]:
    if not expected_line_words or cursor >= len(words):
        return None

    scan_words = max(20, int(config.get("alignment_line_scan_words", 120)))
    min_words_default = 1 if len(expected_line_words) <= 2 else 2
    min_words = max(1, int(config.get("alignment_line_match_min_words", min_words_default)))
    min_ratio = float(config.get("alignment_line_match_min_ratio", 0.45))
    scan_end = min(len(words), cursor + scan_words)

    best: Optional[Dict[str, Any]] = None
    for actual_start in range(cursor, scan_end):
        for expected_offset in range(0, len(expected_line_words)):
            # Large skipped lyric prefixes are allowed only when they produce a
            # strong suffix match. This covers quiet/missing lyric prefixes
            # without letting one common word steal a future line.
            candidate = build_line_candidate_from_start(expected_line_words, words, actual_start, expected_offset, config)
            if candidate is None:
                continue

            # Locality must be measured from the caller's monotonic cursor, not
            # from actual_start (the old score accidentally subtracted
            # first_actual-actual_start, which is normally zero).  Without this
            # penalty a repeated chorus ten seconds later can beat the correct
            # occurrence merely because Whisper assigned it a slightly higher
            # probability.  A few true extra words can still be skipped, but a
            # distant repeat no longer wins by default.
            skipped_actual_words = max(0, int(candidate["first_actual"]) - int(cursor))
            skip_penalty = float(config.get("alignment_line_candidate_skip_word_penalty", 2.0))
            candidate["score"] -= skip_penalty * skipped_actual_words
            candidate["skipped_actual_words"] = skipped_actual_words

            if not allow_long_start_gap and cursor < len(words):
                cursor_time = float(words[cursor].get("start", 0.0))
                first_time = float(words[int(candidate["first_actual"])].get("start", cursor_time))
                max_start_gap = float(config.get("alignment_line_max_start_gap_seconds", 18.0))
                if first_time - cursor_time > max_start_gap:
                    continue
            if candidate["matched_count"] < min(min_words, len(expected_line_words)):
                continue
            if candidate["matched_ratio"] < min_ratio:
                continue
            if (1.0 - candidate["matched_ratio"]) > float(config.get("alignment_line_candidate_max_missing_ratio", 0.55)):
                continue
            if candidate["collapsed_candidate"] and candidate["score"] < 35.0:
                continue
            if best is None or candidate["score"] > best["score"]:
                best = candidate

    if best is None:
        return None

    # A weak candidate is worse than an explicit missing line: it would advance
    # the cursor into unrelated/collapsed words and damage following lines.
    if best["score"] < float(config.get("alignment_line_candidate_min_score", 35.0)):
        return None
    return best


def materialize_line_candidate(
    expected_line_words: List[str],
    words: List[Dict[str, Any]],
    cursor: int,
    candidate: Optional[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    extras: List[Dict[str, Any]] = []
    stats = {
        "expected": len(expected_line_words),
        "matched": 0,
        "fuzzy": 0,
        "mismatch": 0,
        "missing": 0,
        "extra": 0,
        "start_cursor": cursor,
        "end_cursor": cursor,
        "events": events,
        "extra_actual": extras,
        "boundary_reason": "line_candidate",
        "candidate_score": None,
    }

    if candidate is None:
        out: List[Dict[str, Any]] = []
        previous_end: Optional[float] = None
        next_start = float(words[cursor]["start"]) if cursor < len(words) else None
        for ew in expected_line_words:
            item = synthesize_word_timing(ew, previous_end, next_start)
            item["timing_source"] = "missing_line_no_candidate"
            out.append(item)
            previous_end = float(item["end"])
            stats["missing"] += 1
            events.append({"status": "missing_expected", "expected": ew, "reason": "no_line_candidate"})
        stats["boundary_reason"] = "no_line_candidate"
        return out, cursor, stats

    pairs_by_expected = {int(p["expected_index"]): p for p in candidate["pairs"]}
    out = []
    previous_end: Optional[float] = None
    for expected_index, ew in enumerate(expected_line_words):
        pair = pairs_by_expected.get(expected_index)
        if pair is not None:
            aw = pair["actual"]
            item = {
                "text": ew,
                "aligned_text": aw.get("text"),
                "start": aw.get("start"),
                "end": aw.get("end"),
                "probability": aw.get("probability"),
                "match_status": pair.get("match_status", "match"),
                "synthetic_timing": False,
                "similarity": pair.get("similarity", 1.0),
            }
            out.append(item)
            stats["matched"] += 1
            if item["match_status"] == "fuzzy_match":
                stats["fuzzy"] += 1
            events.append({
                "status": item["match_status"],
                "expected": ew,
                "actual": aw.get("text"),
                "start": aw.get("start"),
                "end": aw.get("end"),
                "similarity": item["similarity"],
            })
            previous_end = float(item["end"])
            continue

        next_pair = next((pairs_by_expected[i] for i in range(expected_index + 1, len(expected_line_words)) if i in pairs_by_expected), None)
        next_start = float(next_pair["actual"].get("start")) if next_pair is not None else None
        item = synthesize_word_timing(ew, previous_end, next_start)
        item["timing_source"] = "missing_word_in_line_candidate"
        out.append(item)
        stats["missing"] += 1
        events.append({"status": "missing_expected", "expected": ew, "reason": "not_in_best_line_candidate"})
        previous_end = float(item["end"])

    first_actual = int(candidate["first_actual"])
    if first_actual > cursor:
        for j in range(cursor, first_actual):
            extra = words[j]
            extra_event = {"status": "extra_actual_before_line", "actual": extra.get("text"), "start": extra.get("start"), "end": extra.get("end")}
            extras.append(extra_event)
            events.append(extra_event)
            stats["extra"] += 1

    stats["end_cursor"] = int(candidate["new_cursor"])
    stats["candidate_score"] = float(candidate["score"])
    stats["candidate_matched_ratio"] = float(candidate["matched_ratio"])
    stats["candidate_collapsed"] = bool(candidate["collapsed_candidate"])
    stats["candidate_duration"] = float(candidate["duration"])
    stats["candidate_first_actual"] = int(candidate["first_actual"])
    stats["candidate_last_actual"] = int(candidate["last_actual"])
    return out, int(candidate["new_cursor"]), stats

def normalized_expected_lyric_stream(lyrics_verses: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for verse in lyrics_verses:
        for line in verse.get("lines_text", []) or []:
            out.extend(norm_word(word) for word in lyric_words(str(line)) if norm_word(word))
    return out


def normalized_alignment_stream(words: List[Dict[str, Any]]) -> List[str]:
    return [norm_word(str(word.get("text", ""))) for word in words if norm_word(str(word.get("text", "")))]


def materialize_exact_forced_line(
    expected_line_words: List[str],
    words: List[Dict[str, Any]],
    cursor: int,
) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    """Map a forced-alignment line by token index when the full streams match.

    stable-ts ``--align`` is a forced aligner: when its normalized output token
    stream is identical to the normalized lyrics stream, searching for each
    line again is both unnecessary and unsafe around repeated phrases.  Token
    identity is taken from lyrics; stable-ts contributes timing/probability only.
    """
    count = len(expected_line_words)
    actual_slice = words[cursor:cursor + count]
    out: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    if len(actual_slice) != count:
        raise RuntimeError(
            "forced_sequence_exact invariant violated: "
            f"expected {count} tokens at cursor {cursor}, got {len(actual_slice)}"
        )

    for offset, (expected, actual) in enumerate(zip(expected_line_words, actual_slice)):
        actual_text = str(actual.get("text", ""))
        if norm_word(expected) != norm_word(actual_text):
            raise RuntimeError(
                "forced_sequence_exact invariant violated: "
                f"cursor={cursor}, offset={offset}, "
                f"expected={expected!r}, actual={actual_text!r}"
            )
        item = {
            "text": expected,
            "aligned_text": actual.get("text"),
            "start": actual.get("start"),
            "end": actual.get("end"),
            "probability": actual.get("probability"),
            "match_status": "match",
            "synthetic_timing": False,
            "similarity": 1.0,
            "timing_source": "forced_sequence_exact",
        }
        out.append(item)
        events.append({
            "status": "match",
            "expected": expected,
            "actual": actual.get("text"),
            "start": actual.get("start"),
            "end": actual.get("end"),
            "similarity": 1.0,
        })

    new_cursor = cursor + len(actual_slice)
    report = {
        "expected": count,
        "matched": len(actual_slice),
        "fuzzy": 0,
        "mismatch": 0,
        "missing": max(0, count - len(actual_slice)),
        "extra": 0,
        "start_cursor": cursor,
        "end_cursor": new_cursor,
        "line_start_cursor": cursor,
        "line_end_cursor": new_cursor,
        "events": events,
        "extra_actual": [],
        "boundary_reason": "forced_sequence_exact",
        "candidate_score": 100.0 if len(actual_slice) == count else None,
        "candidate_matched_ratio": (len(actual_slice) / max(1, count)),
        "candidate_collapsed": False,
        "candidate_duration": (
            max(0.0, float(actual_slice[-1].get("end", 0.0)) - float(actual_slice[0].get("start", 0.0)))
            if actual_slice else 0.0
        ),
        "candidate_first_actual": cursor if actual_slice else None,
        "candidate_last_actual": new_cursor - 1 if actual_slice else None,
    }
    return out, new_cursor, report


def match_lyrics_line_words(
    expected_line_words: List[str],
    words: List[Dict[str, Any]],
    cursor: int,
    next_expected_words: List[str],
    config: Dict[str, Any],
    allow_long_start_gap: bool = False,
) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    del next_expected_words  # Candidate scoring replaces greedy boundary guards.
    candidate = find_best_line_candidate(expected_line_words, words, cursor, config, allow_long_start_gap=allow_long_start_gap)
    matched_words, new_cursor, report = materialize_line_candidate(expected_line_words, words, cursor, candidate)
    report["line_start_cursor"] = cursor
    report["line_end_cursor"] = new_cursor
    return matched_words, new_cursor, report


def build_line_aware_verses_from_json_words(
    words: List[Dict[str, Any]],
    lyrics_verses: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], Dict[str, Any]]:
    """Match lyrics line-by-line against the alignment word stream.

    lyrics.txt remains the text truth. Stable-ts words are timing evidence.
    Missing or collapsed words stay in subtitle output, but unreliable timing is
    estimated from neighboring reliable lines and reported explicitly.
    """
    config = config or {}

    ignored_meta_words: List[Dict[str, Any]] = []
    ignored_nonlexical_words: List[Dict[str, Any]] = []
    clean_words: List[Dict[str, Any]] = []
    for w in words:
        text = str(w.get("text", ""))
        if is_alignment_meta_token(text):
            ignored_meta_words.append(w)
            continue
        # Keep the cursor/materialization token space identical to the
        # normalized stream used to decide whether forced_sequence_exact is
        # safe.  stable-ts may emit standalone punctuation (notably an em
        # dash) as a timed word; norm_word() intentionally removes such
        # tokens, so leaving them in clean_words shifts every later cursor.
        if not norm_word(text):
            ignored_nonlexical_words.append(w)
            continue
        clean_words.append(w)

    if not lyrics_verses:
        ly = {
            "index": 1,
            "text": " ".join(w["text"] for w in clean_words),
            "lines_text": [" ".join(w["text"] for w in clean_words)],
            "bracket_directives": [],
            "subrange_divider_after_lines": [],
        }
        lyrics_verses = [ly]

    report: Dict[str, Any] = {
        "mode": "lyrics_driven_line_aware",
        "alignment_words_total": len(words),
        "alignment_words_clean": len(clean_words),
        "ignored_meta_words": ignored_meta_words,
        "ignored_nonlexical_words": ignored_nonlexical_words,
        "ranges": [],
        "trailing_extra_actual": [],
    }
    diagnostics: Dict[str, Any] = {
        "mode": "line_aware_alignment_diagnostics",
        "summary": {
            "ranges": len(lyrics_verses),
            "lines": 0,
            "good_lines": 0,
            "warning_lines": 0,
            "estimated_lines": 0,
            "collapsed_lines": 0,
            "missing_lines": 0,
            "partial_lines": 0,
        },
        "ranges": [],
    }

    cursor = 0
    verses: List[Dict[str, Any]] = []

    # Flatten the following line lookup so each line can use the next lyric line
    # prefix as a local boundary guard.
    verse_line_words: List[List[List[str]]] = [
        [lyric_words(line) for line in ly.get("lines_text", [])]
        for ly in lyrics_verses
    ]

    expected_stream = normalized_expected_lyric_stream(lyrics_verses)
    actual_stream = normalized_alignment_stream(clean_words)
    exact_forced_sequence = (
        bool(expected_stream)
        and len(expected_stream) == len(actual_stream)
        and expected_stream == actual_stream
    )
    report["forced_sequence_exact"] = exact_forced_sequence
    report["expected_words_normalized"] = len(expected_stream)
    report["alignment_words_normalized"] = len(actual_stream)
    report["mapping_strategy"] = (
        "forced_sequence_exact" if exact_forced_sequence else "local_monotonic_fallback"
    )
    diagnostics["mapping_strategy"] = report["mapping_strategy"]

    for vi, ly in enumerate(lyrics_verses):
        out_lines: List[Dict[str, Any]] = []
        range_events: List[Dict[str, Any]] = []
        range_extra: List[Dict[str, Any]] = []
        range_stats = {
            "expected": 0,
            "matched": 0,
            "fuzzy": 0,
            "mismatch": 0,
            "missing": 0,
            "extra": 0,
            "start_cursor": cursor,
            "end_cursor": cursor,
            "events": range_events,
            "extra_actual": range_extra,
            "boundary_reason": (
                "forced_sequence_exact" if exact_forced_sequence else "line_aware_expected_exhausted"
            ),
            "line_statuses": [],
        }

        if not block_has_lyric_text(ly):
            verse_report = {
                "range_index": vi + 1,
                "lyric_index": ly.get("index", vi + 1),
                "text_preview": "",
                **range_stats,
                "boundary_reason": "explicit_non_lyrical_block",
                "start": 0.0,
                "end": 0.01,
                "duration": 0.01,
                "lines": [],
            }
            report["ranges"].append(verse_report)
            diagnostics["ranges"].append({
                "range_index": vi + 1,
                "lyric_index": ly.get("index", vi + 1),
                "text_preview": "",
                "start": 0.0,
                "end": 0.01,
                "duration": 0.01,
                "status": "NON_LYRICAL",
                "lines": [],
            })
            verses.append({
                "index": vi + 1,
                "start": 0.0,
                "end": 0.01,
                "duration": 0.01,
                "text": "",
                "lines": [],
                "alignment_mode": "explicit_gap_fill",
                "bracket_directives": list(ly.get("bracket_directives", [])),
                "subrange_divider_after_lines": list(ly.get("subrange_divider_after_lines", [])),
                "timed_subrange_boundaries": list(ly.get("timed_subrange_boundaries", [])),
                "semantic_boundary_before": ly.get("semantic_boundary_before"),
                "alignment_match": verse_report,
            })
            continue

        line_reports: List[Dict[str, Any]] = []
        for li, line_text in enumerate(ly.get("lines_text", []), 1):
            expected_line_words = verse_line_words[vi][li - 1]
            next_expected = next_lyric_line_words(verse_line_words, vi, li - 1)

            if exact_forced_sequence:
                matched_words, cursor, line_report = materialize_exact_forced_line(
                    expected_line_words,
                    clean_words,
                    cursor,
                )
            else:
                matched_words, cursor, line_report = match_lyrics_line_words(
                    expected_line_words,
                    clean_words,
                    cursor,
                    next_expected,
                    config,
                    allow_long_start_gap=(li == 1),
                )

            if out_lines:
                default_line_start = float(out_lines[-1]["end"])
            else:
                default_line_start = 0.0

            raw_line_start, raw_line_end = line_timing_bounds_from_words(matched_words, default_line_start)
            raw_diag = analyze_matched_line_timing(line_text, matched_words, config)

            # Preserve whole-line evidence for sparse/collapsed/low-support lines.
            # Sanitizing an isolated word is useful for cases like a single
            # low-confidence "fight" jumping several seconds forward, but doing
            # that to an already-unreliable line destroys the evidence needed to
            # place the whole phrase near the correct neighboring anchor.
            sanitizer_report = {"line_text": line_text, "repaired_words": [], "checked_words": len(matched_words)}
            if bool(raw_diag.get("timing_reliable", False)):
                sanitizer_report = sanitize_matched_line_word_timings(line_text, matched_words)

            line_start, line_end = line_timing_bounds_from_words(matched_words, raw_line_start)
            diag = analyze_matched_line_timing(line_text, matched_words, config)
            if not bool(raw_diag.get("timing_reliable", False)):
                # Do not let a later post-sanitizer analysis accidentally promote
                # a line whose raw forced timing was structurally untrustworthy.
                diag["timing_reliable"] = False
                raw_status = str(raw_diag.get("status", "UNRELIABLE"))
                if str(diag.get("status", "GOOD")) == "GOOD":
                    diag["status"] = raw_status
                for issue in raw_diag.get("issues", []):
                    if issue not in diag.setdefault("issues", []):
                        diag["issues"].append(issue)
                diag["raw_diagnostics"] = raw_diag
            if sanitizer_report.get("repaired_words"):
                diag.setdefault("issues", []).append(
                    f"sanitized isolated word timings: {len(sanitizer_report.get('repaired_words', []))}"
                )
                diag["timing_sanitizer"] = sanitizer_report
            line = {
                "index": li,
                "text": line_text,
                "start": line_start,
                "end": max(line_start + 0.01, line_end),
                "raw_start": raw_line_start,
                "raw_end": raw_line_end,
                "words": matched_words,
                "timing_reliable": bool(diag.get("timing_reliable", False)),
                "timing_estimated": False,
                "diagnostics": diag,
                "alignment_match": line_report,
            }
            out_lines.append(line)
            line_reports.append({
                "line_index": li,
                "text": line_text,
                **line_report,
                "diagnostics": diag,
            })

            for key in ("expected", "matched", "fuzzy", "mismatch", "missing", "extra"):
                range_stats[key] += int(line_report.get(key, 0))
            range_events.extend(line_report.get("events", []))
            range_extra.extend(line_report.get("extra_actual", []))

        estimate_unreliable_line_timings(out_lines, config)
        repair_repeated_single_word_lines(out_lines, config)
        repair_adjacent_line_boundary_holds(out_lines, config)

        # Recompute diagnostics after estimating timings so reports reflect the
        # final timing used by subtitles/ranges while preserving original issues.
        diagnostic_lines: List[Dict[str, Any]] = []
        for line in out_lines:
            diag = dict(line.get("diagnostics", {}))
            diag["final_start"] = float(line.get("start", 0.0))
            diag["final_end"] = float(line.get("end", 0.0))
            diag["final_duration"] = max(0.0, diag["final_end"] - diag["final_start"])
            diag["timing_estimated"] = bool(line.get("timing_estimated", False))
            line["diagnostics"] = diag
            range_stats["line_statuses"].append(diag.get("status"))

            diagnostics["summary"]["lines"] += 1
            status = str(diag.get("status", ""))
            if status == "GOOD":
                diagnostics["summary"]["good_lines"] += 1
            else:
                diagnostics["summary"]["warning_lines"] += 1
            if line.get("timing_estimated"):
                diagnostics["summary"]["estimated_lines"] += 1
            if "COLLAPSED" in status:
                diagnostics["summary"]["collapsed_lines"] += 1
            if status == "MISSING":
                diagnostics["summary"]["missing_lines"] += 1
            if status.startswith("PARTIAL"):
                diagnostics["summary"]["partial_lines"] += 1

            diagnostic_lines.append({
                "line_index": line.get("index"),
                "text": line.get("text"),
                **diag,
            })

        starts = [float(line["start"]) for line in out_lines]
        ends = [float(line["end"]) for line in out_lines]
        start = min(starts) if starts else 0.0
        end = max(ends) if ends else start + 0.01

        verse_report = {
            "range_index": vi + 1,
            "lyric_index": ly.get("index", vi + 1),
            "text_preview": str(ly.get("text", "")).splitlines()[0] if str(ly.get("text", "")).splitlines() else "",
            **range_stats,
            "end_cursor": cursor,
            "start": start,
            "end": end,
            "duration": max(0.01, end - start),
            "lines": line_reports,
        }
        report["ranges"].append(verse_report)
        diagnostics["ranges"].append({
            "range_index": vi + 1,
            "lyric_index": ly.get("index", vi + 1),
            "text_preview": verse_report["text_preview"],
            "start": start,
            "end": end,
            "duration": max(0.01, end - start),
            "status": "WARN" if any(str(x.get("status")) != "GOOD" for x in diagnostic_lines) else "OK",
            "lines": diagnostic_lines,
        })

        verses.append({
            "index": vi + 1,
            "start": start,
            "end": end,
            "duration": max(0.01, end - start),
            "text": ly["text"],
            "lines": out_lines,
            "alignment_mode": "word_json_line_aware",
            "bracket_directives": list(ly.get("bracket_directives", [])),
            "subrange_divider_after_lines": list(ly.get("subrange_divider_after_lines", [])),
            "timed_subrange_boundaries": list(ly.get("timed_subrange_boundaries", [])),
            "semantic_boundary_before": ly.get("semantic_boundary_before"),
            "alignment_match": verse_report,
        })

    # Reconcile starts that could not be repaired inside an isolated lyric block.
    # This may cross a plain *** lyric-to-lyric boundary, but never an explicit
    # metadata-only/empty musical block.
    repair_leading_unreliable_lines_across_lyric_blocks(verses, config)
    for _verse in verses:
        if block_has_lyric_text(_verse):
            _lines = _verse.get("lines", []) or []
            repair_repeated_single_word_lines(_lines, config)
            repair_adjacent_line_boundary_holds(_lines, config)
    refresh_line_and_verse_bounds(verses)

    # Final timing estimation across contiguous lyrical range boundaries.
    # Adjacent vocal sections may share anchors, but an empty/metadata-only block
    # is an explicit musical-gap barrier and must stop interpolation.
    contiguous_lines: List[Dict[str, Any]] = []
    for verse in verses:
        if block_has_lyric_text(verse):
            contiguous_lines.extend(verse.get("lines", []) or [])
            continue
        if contiguous_lines:
            estimate_unreliable_line_timings(contiguous_lines, config)
            contiguous_lines = []
    if contiguous_lines:
        estimate_unreliable_line_timings(contiguous_lines, config)

    # A terminal lyric immediately before a non-lyrical block has no trustworthy
    # right lyric anchor.  Apply a small confidence-gated release tail before
    # final verse/timeline bounds are calculated.
    repair_uncertain_terminal_lines_before_nonlyrical_blocks(verses, config)
    # The very last sung word has no following lexical anchor even when its
    # confidence is high. Give it a small release hold before a terminal
    # metadata/non-lyrical tail so karaoke does not disappear on the release.
    repair_final_song_word_release_before_terminal_nonlyrical_tail(verses)
    refresh_line_and_verse_bounds(verses)

    diagnostics["summary"] = {
        "ranges": len(lyrics_verses),
        "lines": 0,
        "good_lines": 0,
        "warning_lines": 0,
        "estimated_lines": 0,
        "collapsed_lines": 0,
        "missing_lines": 0,
        "partial_lines": 0,
    }

    for vi, verse in enumerate(verses):
        out_lines = verse.get("lines", []) or []
        if not block_has_lyric_text(verse):
            start = float(verse.get("start", 0.0))
            end = max(start + 0.01, float(verse.get("end", start + 0.01)))
        else:
            starts = [float(line["start"]) for line in out_lines]
            ends = [float(line["end"]) for line in out_lines]
            start = min(starts) if starts else 0.0
            end = max(ends) if ends else start + 0.01
        verse["start"] = start
        verse["end"] = end
        verse["duration"] = max(0.01, end - start)
        if vi < len(report.get("ranges", [])):
            report["ranges"][vi]["start"] = start
            report["ranges"][vi]["end"] = end
            report["ranges"][vi]["duration"] = max(0.01, end - start)
        if vi < len(diagnostics.get("ranges", [])):
            range_diag = diagnostics["ranges"][vi]
            range_diag["start"] = start
            range_diag["end"] = end
            range_diag["duration"] = max(0.01, end - start)
            diagnostic_lines: List[Dict[str, Any]] = []
            range_has_warning = False
            for line in out_lines:
                diag = dict(line.get("diagnostics", {}))
                diag["final_start"] = float(line.get("start", 0.0))
                diag["final_end"] = float(line.get("end", 0.0))
                diag["final_duration"] = max(0.0, diag["final_end"] - diag["final_start"])
                diag["timing_estimated"] = bool(line.get("timing_estimated", False))
                line["diagnostics"] = diag

                diagnostics["summary"]["lines"] += 1
                status = str(diag.get("status", ""))
                if status == "GOOD":
                    diagnostics["summary"]["good_lines"] += 1
                else:
                    diagnostics["summary"]["warning_lines"] += 1
                    range_has_warning = True
                if line.get("timing_estimated"):
                    diagnostics["summary"]["estimated_lines"] += 1
                if "COLLAPSED" in status:
                    diagnostics["summary"]["collapsed_lines"] += 1
                if status == "MISSING":
                    diagnostics["summary"]["missing_lines"] += 1
                if status.startswith("PARTIAL"):
                    diagnostics["summary"]["partial_lines"] += 1

                diagnostic_lines.append({
                    "line_index": line.get("index"),
                    "text": line.get("text"),
                    **diag,
                })
            range_diag["status"] = "WARN" if range_has_warning else "OK"
            range_diag["lines"] = diagnostic_lines
    while cursor < len(clean_words):
        w = clean_words[cursor]
        report["trailing_extra_actual"].append({
            "actual": w.get("text"),
            "start": w.get("start"),
            "end": w.get("end"),
        })
        cursor += 1

    return verses, report, diagnostics


def load_config(input_dir: Path, data_dir: Path) -> Dict[str, Any]:
    default_path = data_dir / "config.json"
    if not default_path.exists():
        raise FileNotFoundError(f"Default config not found: {default_path}")

    config = load_json(default_path)
    override_path = input_dir / "config.json"
    source = str(default_path)

    if override_path.exists():
        override = load_json(override_path)
        if not isinstance(override, dict):
            raise RuntimeError(f"Config override must be a JSON object: {override_path}")
        config = merge_config(config, override)
        source = str(override_path)

    required = {

        "video_width": int,
        "video_height": int,
        "video_fps": int,
        "clip_duration_tolerance_ratio": (int, float),
        "min_workflow_seconds": (int, float),
        "recommended_workflow_seconds": (int, float),
        "max_workflow_seconds": (int, float),
        "manual_boundary_snap_max_seconds": (int, float),
        "local_context_radius": int,
        "range_visual_preroll_seconds": (int, float),
        "subtitle_line_preroll_seconds": (int, float),
        "min_karaoke_unit_seconds": (int, float),
        "alignment_match_lookahead_words": int,
        "alignment_match_similarity_threshold": (int, float),
        "alignment_match_warn_ratio": (int, float),
        "alignment_match_max_extra_ratio": (int, float),
        "llm_generation": dict,
        "image_generation": dict,
        "video_generation": dict,
    }

    for key, expected_type in required.items():
        if key not in config:
            raise RuntimeError(f"Missing config key: {key}")
        if not isinstance(config[key], expected_type):
            raise RuntimeError(f"Bad config key {key}: expected {expected_type}, got {type(config[key]).__name__}")

    for key in ("image_generation", "video_generation", "llm_generation"):
        template = config[key].get("template")
        if not isinstance(template, str) or not template:
            raise RuntimeError(f"{key}.template must name a generation template")

    config["video_width"] = int(config["video_width"])
    config["video_height"] = int(config["video_height"])
    config["video_fps"] = int(config["video_fps"])
    config["clip_duration_tolerance_ratio"] = float(config["clip_duration_tolerance_ratio"])
    config["min_workflow_seconds"] = float(config["min_workflow_seconds"])
    config["recommended_workflow_seconds"] = float(config["recommended_workflow_seconds"])
    config["max_workflow_seconds"] = float(config["max_workflow_seconds"])
    config["manual_boundary_snap_max_seconds"] = max(0.0, float(config["manual_boundary_snap_max_seconds"]))
    config["local_context_radius"] = int(config["local_context_radius"])
    config["range_visual_preroll_seconds"] = float(config["range_visual_preroll_seconds"])
    config["subtitle_line_preroll_seconds"] = float(config["subtitle_line_preroll_seconds"])
    config["min_karaoke_unit_seconds"] = float(config["min_karaoke_unit_seconds"])
    config["alignment_match_lookahead_words"] = int(config["alignment_match_lookahead_words"])
    config["alignment_match_similarity_threshold"] = float(config["alignment_match_similarity_threshold"])
    config["alignment_match_warn_ratio"] = float(config["alignment_match_warn_ratio"])
    config["alignment_match_max_extra_ratio"] = float(config["alignment_match_max_extra_ratio"])
    config["_source"] = source
    return config


def scan_numbered_input_files(input_dir: Path, prefix: str, suffix: str) -> Dict[int, Path]:
    pattern = re.compile(re.escape(prefix) + r"_(\d+)" + re.escape(suffix) + r"$", re.IGNORECASE)
    out: Dict[int, Path] = {}

    for path in sorted(input_dir.glob(f"{prefix}_*{suffix}")):
        match = pattern.fullmatch(path.name)
        if not match:
            continue

        index = int(match.group(1))
        if index in out:
            raise RuntimeError(
                f"Duplicate numeric override for {prefix} block {index}: "
                f"{out[index]} and {path}"
            )
        out[index] = path

    return out


def scan_block_start_images(input_dir: Path) -> Dict[int, Path]:
    """Find optional range first frames, using the same zero-based override ids."""
    pattern = re.compile(r"start_image_(\d+)\.(png|jpe?g|webp)$", re.IGNORECASE)
    images: Dict[int, Path] = {}
    for path in sorted(input_dir.iterdir()):
        match = pattern.fullmatch(path.name)
        if not path.is_file() or not match:
            continue
        index = int(match.group(1))
        if index in images:
            raise RuntimeError(f"Duplicate start image for range {index}: {images[index]} and {path}")
        images[index] = path
    return images


def load_block_video_styles(input_dir: Path, default_video_style: str, debug_dir: Path) -> Tuple[Dict[int, str], Dict[str, Any]]:
    overrides = scan_numbered_input_files(input_dir, "video_style", ".txt")
    styles: Dict[int, str] = {}
    report: Dict[str, Any] = {
        "default": {
            "source": str(input_dir / "video_style.txt"),
        },
        "blocks": {},
    }

    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "video_style_default_used.txt").write_text(default_video_style, encoding="utf-8")

    for index, path in overrides.items():
        text = read_text(path)
        styles[index] = text
        report["blocks"][str(index)] = {"source": str(path)}
        (debug_dir / f"video_style_{index}_used.txt").write_text(text, encoding="utf-8")

    write_json(debug_dir / "video_style_map.json", report)
    return styles, report


def effective_video_style(block_index: int, default_video_style: str, block_video_styles: Dict[int, str]) -> str:
    return block_video_styles.get(block_index, default_video_style)


def is_bracket_directive_line(line: str) -> bool:
    stripped = line.strip()
    return len(stripped) >= 2 and stripped.startswith("[") and stripped.endswith("]")


def strip_bracket_directive(line: str) -> str:
    return line.strip()[1:-1].strip()


def parse_lyrics_txt(text: str) -> List[Dict[str, Any]]:
    """Parse lyrics.txt into ordered song blocks.

    *** separates semantic ranges. [metadata] lines are range directives.
    --- marks a preferred subrange divider inside the current semantic range.
    Timed separators support @ for an exact boundary, # / #< for snapping to
    the previous lyric boundary, and #> for snapping to the next lyric boundary.
    Dividers are stored as positions after lyric lines and never become lyric
    text, alignment input, subtitles, or prompt text.
    """
    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    tokens = LYRICS_SEPARATOR_SPLIT_RE.split(normalized)
    blocks: List[Dict[str, Any]] = []
    lyric_lines: List[str] = []
    directives: List[str] = []
    divider_after_lines: List[int] = []
    timed_dividers: List[Dict[str, Any]] = []
    raw_parts: List[str] = []
    boundary_before: Optional[Dict[str, Any]] = None

    def finish_block() -> None:
        nonlocal lyric_lines, directives, divider_after_lines, timed_dividers, raw_parts, boundary_before
        valid_dividers: List[int] = []
        for pos in divider_after_lines:
            if 0 < pos < len(lyric_lines) and pos not in valid_dividers:
                valid_dividers.append(pos)
        blocks.append({
            "index": len(blocks) + 1,
            "block_index": len(blocks),
            "text": "\n".join(lyric_lines),
            "lines_text": list(lyric_lines),
            "bracket_directives": list(directives),
            "subrange_divider_after_lines": valid_dividers,
            "timed_subrange_boundaries": list(timed_dividers),
            "semantic_boundary_before": dict(boundary_before) if boundary_before else None,
            "raw_block_text": "".join(raw_parts),
        })
        lyric_lines = []
        directives = []
        divider_after_lines = []
        timed_dividers = []
        raw_parts = []
        boundary_before = None

    for token in tokens:
        if token is None or token == "":
            continue
        separator = parse_lyrics_separator(token)
        if separator:
            if separator["separator"] == "***":
                finish_block()
                boundary_before = separator if separator["requested_time"] is not None else None
            elif separator["requested_time"] is None:
                divider_after_lines.append(len(lyric_lines))
            else:
                timed_dividers.append(separator)
            continue

        raw_parts.append(token)
        raw = token
        raw_lines = raw.splitlines()
        for line in raw_lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("***") or stripped.startswith("---"):
                raise RuntimeError(
                    f"Invalid lyrics separator syntax: {stripped!r}. Expected ***/--- optionally followed by "
                    "@, #, #<, or #> plus MM:SS.mmm."
                )
            if is_bracket_directive_line(stripped):
                directive = strip_bracket_directive(stripped)
                if directive:
                    directives.append(directive)
                continue
            lyric_lines.append(stripped)
    finish_block()

    # Preserve terminal metadata-only blocks such as [End].  The timeline
    # builder gives them the real audio tail after the last sung lyric.  If the
    # tail is shorter than min_workflow_seconds, the generic non-lyrical block
    # coalescer folds it into the previous visual range safely.

    for i, block in enumerate(blocks):
        block["index"] = i + 1
        block["block_index"] = i
    return blocks


def lrc_time_to_seconds(ts: str) -> float:
    # mm:ss.xx or hh:mm:ss.xx
    parts = ts.split(":")
    if len(parts) == 2:
        m = int(parts[0])
        s = float(parts[1])
        return m * 60 + s
    if len(parts) == 3:
        h = int(parts[0])
        m = int(parts[1])
        s = float(parts[2])
        return h * 3600 + m * 60 + s
    raise ValueError(f"Bad LRC timestamp: {ts}")


def parse_lrc(path: Path) -> List[Dict[str, Any]]:
    lines: List[Dict[str, Any]] = []
    pat = re.compile(r"\[([0-9:.]+)\](.*)")
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = pat.match(raw)
        if not m:
            continue
        t = lrc_time_to_seconds(m.group(1))
        text = m.group(2).strip()
        lines.append({"start": t, "text": text})
    lines.sort(key=lambda x: x["start"])

    for i, line in enumerate(lines):
        if i + 1 < len(lines):
            line["end"] = max(line["start"], lines[i + 1]["start"])
        else:
            line["end"] = line["start"] + 2.0
    return lines


def extract_json_words(data: Any) -> List[Dict[str, Any]]:
    words: List[Dict[str, Any]] = []
    for seg in data.get("segments", []):
        for w in seg.get("words", []):
            txt = str(w.get("word", w.get("text", ""))).strip()
            if not txt:
                continue
            try:
                start = float(w["start"])
                end = float(w.get("end", start))
            except Exception:
                continue
            words.append({
                "text": txt,
                "start": start,
                "end": max(start, end),
                "probability": w.get("probability"),
            })
    words.sort(key=lambda x: (x["start"], x["end"]))
    return words


def run_stable_ts_alignment(
    input_dir: Path,
    out_dir: Path,
    debug_dir: Path,
    stable_ts_cmd: str,
    language: str,
) -> Path:
    """Run stable-ts using vocals and a cleaned sung-only lyric file."""
    source_kind, align_audio, _ = detect_alignment_source(input_dir)
    if source_kind != "vocals":
        raise RuntimeError("stable-ts alignment requires input/vocals.*")

    align_dir = out_dir / "alignment"
    clean_lyrics = write_clean_alignment_lyrics(input_dir, align_dir, debug_dir)
    out_json = align_dir / "alignment.json"

    cmd = [
        stable_ts_cmd,
        str(align_audio),
        "--align", str(clean_lyrics),
        "--language", str(language),
        "-o", str(out_json),
    ]

    log("[stage] align vocals with stable-ts")
    log(f"  [align] audio : {align_audio}")
    log(f"  [align] lyrics: {clean_lyrics}")
    log(f"  [align] lang  : {language}")
    log(f"  [align] out   : {out_json}")
    write_json(debug_dir / "stable_ts_alignment_command.json", {
        "command": cmd,
        "audio_mode": source_kind,
        "audio": str(align_audio),
        "lyrics": str(clean_lyrics),
        "language": language,
        "output": str(out_json),
    })

    run_cmd(cmd)
    if not out_json.exists():
        raise RuntimeError(f"stable-ts did not create alignment JSON: {out_json}")
    return out_json


def build_verses_from_lrc_and_lyrics(
    lrc_lines: List[Dict[str, Any]],
    lyrics_verses: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Match LRC sung lines to lyrics.txt semantic ranges.

    lyrics.txt defines semantic ranges and bracket metadata. LRC provides
    line-level timing and may contain [metadata] and *** separator lines, which
    are ignored for subtitles and used only as structure/boundary hints.
    """
    clean_lines: List[Dict[str, Any]] = []
    ignored_lines: List[Dict[str, Any]] = []

    for line in lrc_lines:
        text = str(line.get("text", "")).strip()
        if text == "***":
            ignored_lines.append({**line, "reason": "separator"})
            continue
        if is_subrange_divider_line(text):
            ignored_lines.append({**line, "reason": "subrange_divider"})
            continue
        if is_bracket_directive_line(text):
            ignored_lines.append({**line, "reason": "bracket_directive"})
            continue
        if not text:
            ignored_lines.append({**line, "reason": "empty"})
            continue
        clean_lines.append(line)

    report: Dict[str, Any] = {
        "mode": "lyrics_driven_lrc_line_matching",
        "lrc_lines_total": len(lrc_lines),
        "lrc_lines_clean": len(clean_lines),
        "ignored_lrc_lines": ignored_lines,
        "ranges": [],
        "trailing_extra_lrc_lines": [],
    }

    if not lyrics_verses:
        raise RuntimeError("lyrics.txt is required for LRC semantic range matching")

    verses: List[Dict[str, Any]] = []
    cursor = 0

    for vi, ly in enumerate(lyrics_verses):
        expected_lines = list(ly.get("lines_text", []))
        if not block_has_lyric_text(ly):
            range_report = {
                "range_index": vi + 1,
                "lyric_index": ly.get("index", vi + 1),
                "expected_lines": 0,
                "matched_lines": 0,
                "missing_lines": 0,
                "line_mismatches": 0,
                "start": 0.0,
                "end": 0.01,
                "duration": 0.01,
                "boundary_reason": "explicit_non_lyrical_block",
            }
            report["ranges"].append(range_report)
            verses.append({
                "index": vi + 1,
                "start": 0.0,
                "end": 0.01,
                "duration": 0.01,
                "text": "",
                "lines": [],
                "alignment_mode": "explicit_gap_fill",
                "bracket_directives": list(ly.get("bracket_directives", [])),
                "subrange_divider_after_lines": list(ly.get("subrange_divider_after_lines", [])),
                "timed_subrange_boundaries": list(ly.get("timed_subrange_boundaries", [])),
                "semantic_boundary_before": ly.get("semantic_boundary_before"),
                "alignment_match": range_report,
            })
            continue

        matched_lines = clean_lines[cursor:cursor + len(expected_lines)]
        cursor += len(expected_lines)

        out_lines: List[Dict[str, Any]] = []
        for li, lyric_line in enumerate(expected_lines, 1):
            if li - 1 < len(matched_lines):
                lrc_line = matched_lines[li - 1]
                start = float(lrc_line["start"])
                end = float(lrc_line.get("end", start + 2.0))
                out_lines.append({
                    "index": li,
                    "text": lyric_line,
                    "aligned_text": lrc_line.get("text", ""),
                    "start": start,
                    "end": max(start + 0.01, end),
                    "words": [],
                })
            else:
                if out_lines:
                    start = float(out_lines[-1]["end"])
                elif verses:
                    start = float(verses[-1]["end"])
                else:
                    start = 0.0
                out_lines.append({
                    "index": li,
                    "text": lyric_line,
                    "aligned_text": None,
                    "start": start,
                    "end": start + 2.0,
                    "words": [],
                    "synthetic_timing": True,
                })

        if out_lines:
            start = float(out_lines[0]["start"])
            end = float(out_lines[-1]["end"])
        else:
            start = float(verses[-1]["end"]) if verses else 0.0
            end = start + 0.01

        line_mismatches = 0
        for line in out_lines:
            aligned_text = str(line.get("aligned_text") or "")
            if aligned_text:
                expected_norm = [norm_word(x) for x in lyric_words(line["text"])]
                actual_norm = [norm_word(x) for x in lyric_words(aligned_text)]
                if expected_norm != actual_norm:
                    line_mismatches += 1

        range_report = {
            "range_index": vi + 1,
            "lyric_index": ly.get("index", vi + 1),
            "expected_lines": len(expected_lines),
            "matched_lines": len(matched_lines),
            "missing_lines": max(0, len(expected_lines) - len(matched_lines)),
            "line_mismatches": line_mismatches,
            "start": start,
            "end": end,
            "duration": max(0.01, end - start),
        }
        report["ranges"].append(range_report)

        verses.append({
            "index": vi + 1,
            "start": start,
            "end": end,
            "duration": max(0.01, end - start),
            "text": ly.get("text", "\n".join(expected_lines)),
            "lines": out_lines,
            "alignment_mode": "line_lrc",
            "bracket_directives": list(ly.get("bracket_directives", [])),
            "subrange_divider_after_lines": list(ly.get("subrange_divider_after_lines", [])),
            "timed_subrange_boundaries": list(ly.get("timed_subrange_boundaries", [])),
            "semantic_boundary_before": ly.get("semantic_boundary_before"),
            "alignment_match": range_report,
        })

    for line in clean_lines[cursor:]:
        report["trailing_extra_lrc_lines"].append({
            "text": line.get("text"),
            "start": line.get("start"),
            "end": line.get("end"),
        })

    return verses, report


def write_lrc_match_report(report: Dict[str, Any], out_path: Path) -> None:
    lines: List[str] = []
    lines.append(f"mode             : {report.get('mode')}")
    lines.append(f"lrc lines total  : {report.get('lrc_lines_total')}")
    lines.append(f"lrc lines clean  : {report.get('lrc_lines_clean')}")
    lines.append(f"ignored lines    : {len(report.get('ignored_lrc_lines', []))}")
    lines.append(f"trailing extra   : {len(report.get('trailing_extra_lrc_lines', []))}")
    for r in report.get("ranges", []):
        status = "OK" if not r.get("missing_lines") and not r.get("line_mismatches") else "WARN"
        lines.append(
            f"range {int(r.get('range_index', 0)):03d}: {status}; "
            f"expected_lines={r.get('expected_lines')}; matched_lines={r.get('matched_lines')}; "
            f"missing_lines={r.get('missing_lines')}; line_mismatches={r.get('line_mismatches')}; "
            f"duration={float(r.get('duration', 0)):.2f}s"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_alignment(
    input_dir: Path,
    alignment_dir: Path,
    debug_dir: Path,
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], str]:
    """Parse raw alignment, reusing matched_verses.json when present.

    Cache lifetime is explicit: changing lyrics.txt does not automatically
    invalidate alignment artifacts. Remove the cache/work directory or use
    --refresh-alignment when lyrics or alignment inputs change.
    """
    matched_cache_path = alignment_dir / "matched_verses.json"
    lyrics_text = read_text(input_dir / "lyrics.txt", required=False)
    lyrics_verses = parse_lyrics_txt(lyrics_text) if lyrics_text else []

    if matched_cache_path.exists():
        cached = load_json(matched_cache_path)
        if isinstance(cached, dict):
            verses = cached.get("verses")
            mode = str(cached.get("alignment_mode") or cached.get("mode") or "cached")
        else:
            verses = cached
            mode = "cached"

        cache_ok = (
            isinstance(verses, list)
            and bool(verses)
            and (not lyrics_verses or len(verses) == len(lyrics_verses))
            and all(isinstance(v, dict) for v in verses)
        )
        if cache_ok:
            ensure_line_level_lrc_from_matched_verses(verses, alignment_dir)
            log(f"[stage] use cached matched alignment: {matched_cache_path}")
            return verses, mode

        # Structural corruption/incompatibility is still detected; normal lyric
        # edits are intentionally not fingerprinted here.
        log(f"[stage] ignore invalid matched alignment cache: {matched_cache_path}")

    json_path = alignment_dir / "alignment.json"
    lrc_path = alignment_dir / "alignment.lrc"

    if json_path.exists():
        data = load_json(json_path)
        words = extract_json_words(data)
        if not words:
            raise RuntimeError(f"No word timestamps found in {json_path}")
        if not lyrics_verses:
            raise RuntimeError("lyrics.txt is required for generated alignment.json parsing")

        verses, match_report, diagnostics = build_line_aware_verses_from_json_words(words, lyrics_verses, config)
        write_json(debug_dir / "json_words.json", words[:200])
        write_json(debug_dir / "alignment_match_report.json", match_report)
        write_json(debug_dir / "alignment_diagnostics.json", diagnostics)
        write_json(debug_dir / "alignment_ignored_meta_words.json", match_report.get("ignored_meta_words", []))
        write_alignment_diagnostics_report(diagnostics, debug_dir / "alignment_diagnostics.txt")
        write_json(matched_cache_path, {"alignment_mode": "json", "verses": verses})
        write_line_level_lrc_from_matched_verses(verses, alignment_dir / "alignment.lrc")
        return verses, "json"

    if lrc_path.exists():
        lrc_lines = parse_lrc(lrc_path)
        if not lrc_lines:
            raise RuntimeError(f"No LRC lines found in {lrc_path}")
        if not lyrics_verses:
            raise RuntimeError("lyrics.txt is required for LRC semantic range matching")

        verses, lrc_report = build_verses_from_lrc_and_lyrics(lrc_lines, lyrics_verses)
        write_json(debug_dir / "lrc_match_report.json", lrc_report)
        write_lrc_match_report(lrc_report, debug_dir / "lrc_match_report.txt")
        write_json(matched_cache_path, {"alignment_mode": "lrc", "verses": verses})
        return verses, "lrc"

    raise FileNotFoundError(
        f"No generated alignment found. Expected {alignment_dir / 'alignment.json'} "
        f"or {alignment_dir / 'alignment.lrc'}. Run a normal fresh generation first."
    )

def resolve_command(candidates: List[Path], fallback: str) -> str:
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)

    found = shutil.which(fallback)
    return found or fallback


def resolve_config_path(path_value: str, script_dir: Path) -> Path:
    raw = str(path_value).strip()
    if not raw:
        raise RuntimeError("Configured path is empty")
    # Config files commonly use Windows-style separators. Normalize them so
    # relative sibling paths work consistently in tests and on non-Windows hosts.
    normalized = raw.replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute():
        return path.resolve()
    return (script_dir / path).resolve()
def resolve_stable_ts_command(script_dir: Path) -> str:
    parent = script_dir.parent
    candidates = [
        parent / "stable-ts" / ".venv" / "Scripts" / "stable-ts.exe",
        parent / "stable-ts" / ".venv" / "bin" / "stable-ts",
    ]
    return resolve_command(candidates, "stable-ts")


def resolve_ffmpeg_command(script_dir: Path) -> str:
    parent = script_dir.parent
    candidates = [
        parent / "ffmpeg" / "bin" / "ffmpeg.exe",
        parent / "ffmpeg" / "bin" / "ffmpeg",
    ]
    return resolve_command(candidates, "ffmpeg")


def resolve_ffprobe_command(script_dir: Path) -> str:
    parent = script_dir.parent
    candidates = [
        parent / "ffmpeg" / "bin" / "ffprobe.exe",
        parent / "ffmpeg" / "bin" / "ffprobe",
    ]
    return resolve_command(candidates, "ffprobe")


def find_first_existing(input_dir: Path, stem: str, extensions: Tuple[str, ...] = (".mp3", ".wav", ".m4a", ".flac")) -> Optional[Path]:
    for ext in extensions:
        p = input_dir / f"{stem}{ext}"
        if p.exists():
            return p
    return None


def detect_alignment_source(input_dir: Path) -> Tuple[str, Path, Optional[Path]]:
    vocals = find_first_existing(input_dir, "vocals")
    if vocals:
        return "vocals", vocals, None

    lrc = input_dir / "alignment.lrc"
    if lrc.exists():
        return "lrc", lrc, None

    raise FileNotFoundError(
        "No alignment source found. Use input/vocals.* for stable-ts word alignment, "
        "or input/alignment.lrc for line-level timing when only full audio is available."
    )


def detect_audio(input_dir: Path) -> Tuple[str, Path, Optional[Path]]:
    """Detect audio used for final video.

    Prefer input/audio.* if present. Use vocals+instrumental mix only when full
    audio is absent.
    """
    full = find_first_existing(input_dir, "audio")
    if full:
        return "full", full, None

    vocals = find_first_existing(input_dir, "vocals")
    instrumental = find_first_existing(input_dir, "instrumental")
    if vocals and instrumental:
        return "stems", vocals, instrumental

    raise FileNotFoundError("No final audio found. Use input/audio.* or input/vocals.* + input/instrumental.*")


def ass_timestamp(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    sec -= h * 3600
    m = int(sec // 60)
    sec -= m * 60
    s = int(sec)
    cs = int(round((sec - s) * 100))
    if cs == 100:
        s += 1
        cs = 0
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def centiseconds(duration: float) -> int:
    return max(1, int(round(duration * 100)))


def build_karaoke_delay(duration: float) -> str:
    """Consume karaoke time without highlighting the next visible syllable.

    ASS karaoke timing must be attached to text. A bare "{\\kN}" before a
    visible word can be rendered as part of that next word, making preroll or
    inter-word gaps highlight too early. Use a transparent zero-width syllable
    to consume the gap while the full line remains visible in its unsung state.
    """
    if duration <= 0:
        return ""
    return r"{\alpha&HFF&\k" + str(centiseconds(duration)) + "}\u200b" + r"{\alpha&H00&}"


def is_karaoke_timed_char(ch: str) -> bool:
    """Return True for characters that should receive their own karaoke duration."""
    return ch.isalnum()


def split_centiseconds_evenly(total_cs: int, parts: int) -> List[int]:
    if parts <= 0:
        return []
    total_cs = max(parts, int(total_cs))
    base = total_cs // parts
    rem = total_cs % parts
    return [base + 1 if i < rem else base for i in range(parts)]


def build_char_karaoke_text(text: str, total_duration: float, min_unit: float = 0.01) -> str:
    """Build char-level ASS karaoke tags over a known time range.

    Letters and digits receive timing tags. Spaces and punctuation are kept in
    the text but do not receive their own duration; they attach visually to the
    surrounding timed characters.
    """
    escaped = ass_escape(text)
    timed_indices = [i for i, ch in enumerate(escaped) if is_karaoke_timed_char(ch)]
    total_cs = centiseconds(max(min_unit, total_duration))

    if not timed_indices:
        return r"{\k" + str(total_cs) + "}" + escaped

    durations = split_centiseconds_evenly(total_cs, len(timed_indices))
    duration_by_index = dict(zip(timed_indices, durations))

    out: List[str] = []
    for i, ch in enumerate(escaped):
        if i in duration_by_index:
            out.append(r"{\k" + str(duration_by_index[i]) + "}" + ch)
        else:
            out.append(ch)

    return "".join(out)


def build_word_karaoke_line(
    line: Dict[str, Any],
    shift: float,
    event_start: Optional[float] = None,
    min_unit: float = 0.01,
) -> str:
    """Build word-level karaoke while preserving gaps between word timestamps.

    event_start is the ASS Dialogue start after shift. Silent karaoke gaps are
    inserted before words so pauses do not make the next word highlight early.
    """
    words = line.get("words", []) or []
    if not words:
        line_start = float(line["start"]) - shift
        line_end = float(line["end"]) - shift
        if event_start is None:
            event_start = line_start
        lead = max(0.0, line_start - event_start)
        body = build_char_karaoke_text(str(line["text"]), max(min_unit, line_end - line_start), min_unit)
        return build_karaoke_delay(lead) + body

    if event_start is None:
        event_start = min(float(w.get("start", line["start"])) for w in words) - shift

    pieces: List[str] = []
    current = event_start

    for i, w in enumerate(words):
        ws = float(w.get("start", line["start"])) - shift
        we = float(w.get("end", ws + min_unit)) - shift
        ws = max(event_start, ws)
        we = max(ws + min_unit, we)

        if i > 0:
            pieces.append(" ")

        gap = max(0.0, ws - current)
        if gap > 0:
            pieces.append(build_karaoke_delay(gap))

        pieces.append(build_char_karaoke_text(str(w.get("text", "")), max(min_unit, we - ws), min_unit))
        current = max(current, we)

    if pieces:
        return "".join(pieces)

    line_duration = max(min_unit, float(line["end"]) - float(line["start"]))
    return build_char_karaoke_text(str(line["text"]), line_duration, min_unit)


def parse_ass_styles_section(path: Path) -> Tuple[str, Dict[str, str]]:
    """Read an ASS style file and return the Format line and the required line style.

    The file must contain:
      [V4+ Styles]
      Format: ...
      Style: line,...

    This single style contains both colors:
      PrimaryColour   = unsung text
      SecondaryColour = sung karaoke highlight
    """
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()

    in_styles = False
    format_line = ""
    styles: Dict[str, str] = {}

    for raw in lines:
        line = raw.strip()
        if not line:
            continue

        if line.startswith("[") and line.endswith("]"):
            in_styles = line.lower() == "[v4+ styles]"
            continue

        if not in_styles:
            continue

        if line.lower().startswith("format:"):
            format_line = line
            continue

        if line.lower().startswith("style:"):
            payload = line.split(":", 1)[1].strip()
            name = payload.split(",", 1)[0].strip()
            styles[name] = line

    if not format_line:
        raise RuntimeError(f"Subtitle style file has no Format line: {path}")

    if "line" not in styles:
        raise RuntimeError(
            f"Subtitle style file must contain Style: line: {path}"
        )

    return format_line, {
        "line": styles["line"],
    }


def rename_ass_style(style_line: str, new_name: str) -> str:
    prefix, payload = style_line.split(":", 1)
    parts = payload.strip().split(",", 1)
    if len(parts) != 2:
        raise RuntimeError(f"Bad ASS Style line: {style_line}")
    return f"{prefix}: {new_name},{parts[1]}"


def scan_block_subtitle_style_overrides(input_dir: Path) -> Dict[int, Path]:
    """Scan input/subtitle_styles_N.ass overrides.

    The block number is parsed from filenames like:
      subtitle_styles_[number].ass

    Both unpadded and padded forms are accepted:
      subtitle_styles_1.ass
      subtitle_styles_001.ass

    Duplicate numeric block IDs are an error, regardless of padding.
    """
    overrides: Dict[int, Path] = {}
    pattern = re.compile(r"subtitle_styles_[number].ass", re.IGNORECASE)

    for path in sorted(input_dir.glob("subtitle_styles_*.ass")):
        match = pattern.fullmatch(path.name)
        if not match:
            continue

        block_index = int(match.group(1))
        if block_index in overrides:
            raise RuntimeError(
                f"Duplicate subtitle style override for block {block_index}: "
                f"{overrides[block_index]} and {path}"
            )

        overrides[block_index] = path

    return overrides


def resolve_default_subtitle_style(input_dir: Path, data_dir: Path) -> Path:
    song_style = input_dir / "subtitle_styles.ass"
    if song_style.exists():
        return song_style

    default_style = data_dir / "subtitle_styles.ass"
    if default_style.exists():
        return default_style

    raise FileNotFoundError(
        f"Subtitle style not found. Expected {song_style} or {default_style}"
    )


def build_subtitle_styles_for_blocks(
    blocks: List[Dict[str, Any]],
    input_dir: Path,
    data_dir: Path,
    debug_dir: Path,
) -> Tuple[str, Dict[int, Dict[str, str]], Dict[str, Any]]:
    """Build the global ASS style section and per-block style mapping.

    Always emits:
      default_line

    Emits clip_N styles only for blocks that have explicit override files:
      clip_3_line
    """
    default_source = resolve_default_subtitle_style(input_dir, data_dir)
    default_format, default_styles = parse_ass_styles_section(default_source)
    overrides = scan_block_subtitle_style_overrides(input_dir)

    style_lines: List[str] = [
        "[V4+ Styles]",
        default_format,
        rename_ass_style(default_styles["line"], "default_line"),
    ]

    mapping: Dict[int, Dict[str, str]] = {}
    report: Dict[str, Any] = {
        "default": {
            "source": str(default_source),
            "styles": ["default_line"],
        },
        "blocks": {},
    }

    debug_dir.mkdir(parents=True, exist_ok=True)
    (debug_dir / "subtitle_style_default_used.ass").write_text(
        default_source.read_text(encoding="utf-8-sig"), encoding="utf-8"
    )

    active_block_ids = {int(b["block_index"]) for b in blocks}

    for block in blocks:
        idx = int(block["block_index"])
        override = overrides.get(idx)

        if override is None:
            mapping[idx] = {
                "line": "default_line",
            }
            continue

        override_format, override_styles = parse_ass_styles_section(override)
        if override_format != default_format:
            raise RuntimeError(
                f"Subtitle style override Format differs from default. "
                f"Override={override}, default={default_source}"
            )

        line_name = f"clip_{idx}_line"
        style_lines.append(rename_ass_style(override_styles["line"], line_name))

        mapping[idx] = {
            "line": line_name,
        }

        report["blocks"][str(idx)] = {
            "source": str(override),
            "styles": [line_name],
        }

        (debug_dir / f"subtitle_style_{idx}_used.ass").write_text(
            override.read_text(encoding="utf-8-sig"), encoding="utf-8"
        )

    unused = sorted(k for k in overrides.keys() if k not in active_block_ids)
    if unused:
        report["unused_overrides"] = {
            str(k): str(overrides[k]) for k in unused
        }

    write_json(debug_dir / "subtitle_styles_map.json", report)

    return "\n".join(style_lines), mapping, report


def build_ass_subtitles(
    verses: List[Dict[str, Any]],
    blocks: List[Dict[str, Any]],
    shift: float,
    width: int,
    height: int,
    out_path: Path,
    mode: str,
    style_section: str,
    style_map: Dict[int, Dict[str, str]],
    config: Optional[Dict[str, Any]] = None,
    timing_report_path: Optional[Path] = None,
) -> None:
    config = config or {}
    subtitle_preroll = max(0.0, float(config.get("subtitle_line_preroll_seconds", 0.0)))
    min_unit = max(0.01, float(config.get("min_karaoke_unit_seconds", 0.01)))
    # Adjacent identical lyric lines are distinct sung repetitions, but ASS has no
    # visual discontinuity when one event ends at exactly the same instant the
    # next event with the same text starts.  To a viewer this can look like one
    # long karaoke event whose progress simply continues. Insert a tiny
    # rendering-only reset gap between such events. The gap is a code constant,
    # not a config.json parameter; it does not alter alignment, semantic ranges,
    # or word ownership.
    repeat_reset_seconds = KARAOKE_REPEAT_RESET_SECONDS

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

{style_section}

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    verse_to_block: Dict[int, int] = {}
    block_by_index: Dict[int, Dict[str, Any]] = {}
    for block in blocks:
        block_by_index[int(block["block_index"])] = block
        if block_has_lyric_text(block):
            verse_to_block[int(block["verse_index"])] = int(block["block_index"])

    events: List[str] = []
    timing_report: List[Dict[str, Any]] = []
    previous_event_end = 0.0
    previous_line_key = ""

    for verse in verses:
        verse_index = int(verse.get("index", 0))
        block_index = verse_to_block.get(verse_index, verse_index)
        block = block_by_index.get(block_index, {})
        block_visual_start = float(block.get("start", verse.get("start", 0.0))) - shift
        styles = style_map.get(block_index, {"line": "default_line"})

        for line in verse["lines"]:
            raw_line_start = float(line["start"]) - shift
            raw_line_end = float(line["end"]) - shift
            style = styles["line"]

            word_times = [
                float(w.get("start", line["start"])) - shift
                for w in (line.get("words", []) or [])
            ]
            karaoke_start = min(word_times) if word_times else raw_line_start
            display_start = max(
                0.0,
                block_visual_start,
                previous_event_end,
                karaoke_start - subtitle_preroll,
            )

            line_key = " ".join(norm_word(w) for w in lyric_words(str(line.get("text", ""))) if norm_word(w))
            repeat_reset_applied = 0.0
            if repeat_reset_seconds > 0.0 and line_key and line_key == previous_line_key:
                # Only add a reset where the two identical events would otherwise
                # visually touch.  Never consume the whole next event: preserve at
                # least 0.12 s of render time for extremely short lines.
                natural_gap = max(0.0, karaoke_start - previous_event_end)
                if natural_gap < repeat_reset_seconds:
                    available = max(0.0, raw_line_end - display_start - 0.12)
                    extra_gap = min(max(0.0, repeat_reset_seconds - natural_gap), available)
                    if extra_gap > 1e-6:
                        display_start += extra_gap
                        repeat_reset_applied = extra_gap

            end = max(display_start + 0.1, raw_line_end)

            if mode == "word" and line.get("words"):
                karaoke_text = build_word_karaoke_line(line, shift, display_start, min_unit)
            else:
                lead = max(0.0, raw_line_start - display_start)
                body = build_char_karaoke_text(str(line["text"]), max(min_unit, raw_line_end - raw_line_start), min_unit)
                karaoke_text = build_karaoke_delay(lead) + body
            events.append(f"Dialogue: 0,{ass_timestamp(display_start)},{ass_timestamp(end)},{style},,0,0,0,,{karaoke_text}")

            timing_report.append({
                "verse_index": verse_index,
                "block_index": block_index,
                "line_index": line.get("index"),
                "block_visual_start": block_visual_start,
                "line_display_start": display_start,
                "karaoke_start": karaoke_start,
                "line_end": end,
                "subtitle_leadin": max(0.0, karaoke_start - display_start),
                "repeat_reset_applied_seconds": repeat_reset_applied,
                "block_visual_preroll": float(block.get("visual_preroll", 0.0)),
                "text": str(line.get("text", "")),
            })
            previous_event_end = end
            previous_line_key = line_key

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")

    if timing_report_path is not None:
        write_json(timing_report_path, {
            "subtitle_line_preroll_seconds": subtitle_preroll,
            "min_karaoke_unit_seconds": min_unit,
            "karaoke_repeat_reset_constant_seconds": repeat_reset_seconds,
            "events": timing_report,
        })


def ass_draw_rect(x: int, y: int, w: int, h: int, color: str, alpha: str = "&H00&") -> str:
    """Return an ASS vector rectangle at absolute screen coordinates."""
    w = max(1, int(w))
    h = max(1, int(h))
    return (
        f"{{\\an7\\pos({int(x)},{int(y)})\\p1\\bord0\\shad0"
        f"\\c{color}\\alpha{alpha}}}m 0 0 l {w} 0 l {w} {h} l 0 {h}"
    )


def ass_draw_tick(x: int, y: int, h: int, color: str, alpha: str = "&H00&", width: int = 2) -> str:
    return ass_draw_rect(int(x), int(y), max(1, int(width)), int(h), color, alpha)


def clamp01(value: float) -> float:
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def format_progress_time(value: float) -> str:
    value = max(0.0, float(value))
    if value >= 60.0:
        minutes = int(value // 60)
        seconds = int(round(value - minutes * 60))
        if seconds >= 60:
            minutes += 1
            seconds -= 60
        return f"{minutes:02d}:{seconds:02d}"
    return f"{value:04.1f}s"


def build_preview_debug_ass(
    blocks: List[Dict[str, Any]],
    out_path: Path,
    width: int,
    height: int,
    config: Dict[str, Any],
    total_duration: float,
) -> None:
    """Write debug-only ASS progress bars for subtitle_preview.mp4.

    This file is never used for the release/final karaoke subtitles.
    It renders three vector progress bars: full song, current range, and current subrange.
    """
    total_duration = max(0.1, float(total_duration))
    range_count = len(blocks)
    step = max(0.1, float(config.get("preview_progress_step_seconds", 0.5)))

    label_x = 24
    bar_x = 150
    bar_w = max(240, int(width - 330))
    time_x = bar_x + bar_w + 18
    bar_h = 18
    row_gap = 34
    y0 = 24
    y_song = y0
    y_range = y0 + row_gap
    y_sub = y0 + row_gap * 2
    tick_h = bar_h + 8

    bg = "&H00242424&"
    border = "&H00787878&"
    fill_song = "&H00C28A35&"
    fill_range = "&H0065B86A&"
    fill_sub = "&H00D0B050&"
    range_tick = "&H00FFFFFF&"
    sub_tick = "&H0060E8FF&"

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: PreviewDebug,Consolas,24,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,0,0,0,0,100,100,0,0,1,2,0,7,24,24,24,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: List[str] = []

    def add_draw(layer: int, start: float, end: float, shape: str) -> None:
        if end <= start:
            return
        events.append(f"Dialogue: {layer},{ass_timestamp(start)},{ass_timestamp(end)},PreviewDebug,,0,0,0,,{shape}")

    def add_text(layer: int, start: float, end: float, x: int, y: int, text: str) -> None:
        if end <= start:
            return
        events.append(
            f"Dialogue: {layer},{ass_timestamp(start)},{ass_timestamp(end)},PreviewDebug,,0,0,0,,"
            f"{{\\an7\\pos({int(x)},{int(y)})}}{ass_escape(text)}"
        )

    def add_bar_background(start: float, end: float, y: int) -> None:
        add_draw(1, start, end, ass_draw_rect(bar_x, y, bar_w, bar_h, bg, "&H20&"))
        add_draw(4, start, end, ass_draw_rect(bar_x - 1, y - 1, bar_w + 2, 2, border, "&H20&"))
        add_draw(4, start, end, ass_draw_rect(bar_x - 1, y + bar_h, bar_w + 2, 2, border, "&H20&"))
        add_draw(4, start, end, ass_draw_rect(bar_x - 1, y - 1, 2, bar_h + 2, border, "&H20&"))
        add_draw(4, start, end, ass_draw_rect(bar_x + bar_w, y - 1, 2, bar_h + 2, border, "&H20&"))

    add_text(5, 0.0, total_duration, label_x, y_song - 4, "SONG")
    add_bar_background(0.0, total_duration, y_song)
    # Full-song progress fill and time label.
    t = 0.0
    while t < total_duration:
        nt = min(total_duration, t + step)
        mid = (t + nt) * 0.5
        ratio = clamp01(mid / total_duration)
        fill_w = int(round(bar_w * ratio))
        if fill_w > 0:
            add_draw(2, t, nt, ass_draw_rect(bar_x, y_song, fill_w, bar_h, fill_song, "&H20&"))
        add_text(5, t, nt, time_x, y_song - 4, f"{format_progress_time(mid)} / {format_progress_time(total_duration)}")
        t = nt

    # Always-visible full-song range and subrange boundary ticks.
    seen_sub_ticks = set()
    for block in blocks:
        start = max(0.0, float(block["start"]))
        end = min(total_duration, max(start, float(block["end"])))
        for boundary in (start, end):
            x = bar_x + int(round(bar_w * clamp01(boundary / total_duration)))
            add_draw(6, 0.0, total_duration, ass_draw_tick(x, y_song - 4, tick_h, range_tick, "&H00&", 2))
        for sub in build_subranges_for_block(block, config):
            for boundary in (float(sub["start"]), float(sub["end"])):
                key = round(boundary, 3)
                if key in seen_sub_ticks:
                    continue
                seen_sub_ticks.add(key)
                x = bar_x + int(round(bar_w * clamp01(boundary / total_duration)))
                add_draw(5, 0.0, total_duration, ass_draw_tick(x, y_song, bar_h, sub_tick, "&H10&", 1))

    # Per-range and per-subrange progress bars.
    for block in blocks:
        block_i = int(block["block_index"])
        start = max(0.0, float(block["start"]))
        end = min(total_duration, max(start + 0.1, float(block["end"])))
        duration = max(0.1, end - start)
        subranges = build_subranges_for_block(block, config)

        add_text(5, start, end, label_x, y_range - 4, f"R{block_i:03d}/{range_count:03d}")
        add_bar_background(start, end, y_range)
        for sub in subranges:
            boundary = max(start, min(end, float(sub["start"])))
            x = bar_x + int(round(bar_w * clamp01((boundary - start) / duration)))
            add_draw(6, start, end, ass_draw_tick(x, y_range - 4, tick_h, sub_tick, "&H00&", 2))
        add_draw(6, start, end, ass_draw_tick(bar_x + bar_w, y_range - 4, tick_h, sub_tick, "&H00&", 2))

        t = start
        while t < end:
            nt = min(end, t + step)
            mid = (t + nt) * 0.5
            elapsed = max(0.0, mid - start)
            ratio = clamp01(elapsed / duration)
            fill_w = int(round(bar_w * ratio))
            if fill_w > 0:
                add_draw(2, t, nt, ass_draw_rect(bar_x, y_range, fill_w, bar_h, fill_range, "&H20&"))
            add_text(5, t, nt, time_x, y_range - 4, f"{format_progress_time(elapsed)} / {format_progress_time(duration)}")
            t = nt

        sub_count = len(subranges)
        for sub_i, sub in enumerate(subranges):
            ss = max(start, float(sub["start"]))
            se = min(end, max(ss + 0.1, float(sub["end"])))
            sd = max(0.1, se - ss)
            add_text(5, ss, se, label_x, y_sub - 4, f"S{sub_i:03d}/{sub_count:03d}")
            add_bar_background(ss, se, y_sub)
            t = ss
            while t < se:
                nt = min(se, t + step)
                mid = (t + nt) * 0.5
                elapsed = max(0.0, mid - ss)
                ratio = clamp01(elapsed / sd)
                fill_w = int(round(bar_w * ratio))
                if fill_w > 0:
                    add_draw(2, t, nt, ass_draw_rect(bar_x, y_sub, fill_w, bar_h, fill_sub, "&H20&"))
                add_text(5, t, nt, time_x, y_sub - 4, f"{format_progress_time(elapsed)} / {format_progress_time(sd)}")
                t = nt

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")

def ffmpeg_sub_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def copy_video_only(video_in: Path, video_out: Path, ffmpeg: str) -> None:
    """Copy only the video stream without re-encoding."""
    video_out.parent.mkdir(parents=True, exist_ok=True)
    run_cmd([
        ffmpeg, "-y",
        "-i", str(video_in),
        "-map", "0:v:0",
        "-an",
        "-c:v", "copy",
        "-movflags", "+faststart",
        str(video_out),
    ])


def concat_videos(clips: List[Path], out: Path, ffmpeg: str) -> None:
    """Concatenate compatible video-only clips without re-encoding."""
    out.parent.mkdir(parents=True, exist_ok=True)
    concat_txt = out.parent / "concat.txt"
    concat_txt.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in clips), encoding="utf-8")
    run_cmd([
        ffmpeg, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_txt),
        "-map", "0:v:0",
        "-an",
        "-c:v", "copy",
        "-movflags", "+faststart",
        str(out),
    ])


def retime_video_copy(
    video_in: Path,
    target_duration: float,
    video_out: Path,
    ffmpeg: str,
    ffprobe: str,
    fps: int,
) -> Dict[str, Any]:
    """Fit a video-only range clip to the timeline by scaling timestamps only."""
    video_out.parent.mkdir(parents=True, exist_ok=True)
    source_duration = ffprobe_duration(video_in, ffprobe)
    if source_duration <= 0:
        raise RuntimeError(f"Cannot retime zero-duration video: {video_in}")
    target_duration = max(0.1, float(target_duration))
    scale = target_duration / source_duration
    run_cmd([
        ffmpeg, "-y",
        "-itsscale", f"{scale:.9f}",
        "-i", str(video_in),
        "-map", "0:v:0",
        "-an",
        "-c:v", "copy",
        "-movflags", "+faststart",
        str(video_out),
    ])
    verified_duration = ffprobe_duration(video_out, ffprobe)
    verify_epsilon = max(0.15, 3.0 / max(1, int(fps)))
    if abs(verified_duration - target_duration) > verify_epsilon:
        raise RuntimeError(
            "Retimed clip duration verification failed:\n"
            f"  source: {video_in} duration={source_duration:.3f}s\n"
            f"  target: {video_out} expected={target_duration:.3f}s actual={verified_duration:.3f}s\n"
            "Intermediate clips are timestamp-retimed with stream copy only; no silent re-encode fallback is used."
        )
    return {
        "source": str(video_in),
        "target": str(video_out),
        "source_duration": source_duration,
        "target_duration": target_duration,
        "scale": scale,
        "verified_duration": verified_duration,
        "codec_copy": True,
    }


def final_mux(video_in: Path, audio_in: Path, ass_path: Path, out: Path, ffmpeg: str, fps: int) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    sub_arg = ffmpeg_sub_path(ass_path)
    run_cmd([
        ffmpeg, "-y",
        "-i", str(video_in),
        "-i", str(audio_in),
        "-vf", f"fps={int(fps)},subtitles='{sub_arg}'",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ])


def render_subtitle_preview(
    audio_in: Path,
    ass_path: Path,
    out: Path,
    duration: float,
    width: int,
    height: int,
    fps: int,
    ffmpeg: str,
    debug_ass_path: Optional[Path] = None,
) -> None:
    """Render a quick black-screen karaoke preview with final audio and debug range markers."""
    out.parent.mkdir(parents=True, exist_ok=True)
    sub_arg = ffmpeg_sub_path(ass_path)
    vf = f"subtitles='{sub_arg}'"
    if debug_ass_path is not None:
        debug_sub_arg = ffmpeg_sub_path(debug_ass_path)
        vf += f",subtitles='{debug_sub_arg}'"
    run_cmd([
        ffmpeg, "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:s={width}x{height}:r={fps}:d={duration:.3f}",
        "-i", str(audio_in),
        "-vf", vf,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ])


def strip_llm_wrappers(text: str) -> str:
    """Remove common wrapper text around a JSON object without repairing JSON syntax."""
    cleaned = text.strip()
    cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.S | re.I).strip()

    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, flags=re.S | re.I)
    if fence:
        return fence.group(1).strip()
    return cleaned

def extract_json_object(text: str) -> Dict[str, Any]:
    """Extract exactly one JSON object from an LLM text response.

    This function intentionally does not repair malformed JSON. It only removes
    common non-JSON wrappers such as <think> blocks or markdown fences and then
    extracts the first top-level object. Syntax errors remain technical stage
    failures that are handled by the LLM quality loop.
    """
    cleaned = strip_llm_wrappers(text)
    try:
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("LLM JSON root must be an object")
        return data
    except json.JSONDecodeError:
        pass

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        snippet = cleaned[start:end + 1]
        data = json.loads(snippet)
        if not isinstance(data, dict):
            raise ValueError("LLM JSON root must be an object")
        return data
    raise ValueError("LLM response does not contain a JSON object")


def build_planner_user_prompt(
    rules: Dict[str, str],
    video_style: str,
    song_context: Dict[str, Any],
    local_context: str,
    current_block: str,
    continuity: List[Dict[str, str]],
    block_kind: str,
    block_index: int,
) -> Tuple[str, str]:
    continuity_text = "\n".join(f"- segment {x.get('segment')}: {x.get('scene_summary')}" for x in continuity[-5:]) or "- none"

    if block_kind == "intro":
        template_name = "block_planner_intro.txt"
    elif block_kind == "instrumental":
        template_name = "block_planner_instrumental.txt"
    elif block_kind == "outro":
        template_name = "block_planner_outro.txt"
    else:
        template_name = "block_planner_verse.txt"

    prompt = render_template(
        rules[template_name],
        {
            "VIDEO_STYLE": video_style,
            "GLOBAL_CONTEXT_JSON": song_context,
            "LOCAL_CONTEXT": local_context,
            "CURRENT_BLOCK": current_block,
            "BLOCK_KIND": block_kind,
            "BLOCK_INDEX": block_index,
            "CONTINUITY": continuity_text,
            "LITERAL_SCENE_RULES": rules["literal_scene_rules.txt"],
        },
        template_name,
    )
    return prompt, template_name


def run_visual_planner(
    llm_generator,
    rules: Dict[str, str],
    video_style: str,
    song_context: Dict[str, Any],
    local_context: str,
    current_block: str,
    index: int,
    block_kind: str,
    plans_dir: Path,
    continuity: List[Dict[str, str]],
    plan_suffix: str = "",
) -> Dict[str, str]:
    plans_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"plan_{index:03d}{plan_suffix}"
    raw_path = plans_dir / f"{base_name}_response.txt"
    clean_path = plans_dir / f"{base_name}.json"
    response_json_path = plans_dir / f"{base_name}_response.json"
    parsed_json_path = plans_dir / f"{base_name}_parsed.json"
    request_path = plans_dir / f"{base_name}_request.txt"
    request_json_path = plans_dir / f"{base_name}_request.json"

    if raw_path.exists():
        raw_path.unlink()

    request_json_path.write_text(json.dumps({
        "block_index": index,
        "block_kind": block_kind,
        "visual_style": video_style,
        "song_context": song_context,
        "local_context": local_context,
        "current_block": current_block,
        "continuity": continuity[-5:],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    log("  [stage] LLM planner")
    user_prompt, _ = build_planner_user_prompt(rules, video_style, song_context, local_context,
                                               current_block, continuity, block_kind, index)
    save_prompt_debug(request_path, user_prompt)
    result = llm_generator.generate(LlmRequest(
        system_prompt=rules["block_planner_system.txt"], prompt=user_prompt, seed=0,
        response_path=raw_path, sub_dir=base_name, debug_dir=plans_dir,
    ))
    raw_text = result.text
    plan = extract_json_object(raw_text)
    required = ["scene_summary", "image_prompt", "video_prompt", "negative_prompt"]
    missing = [k for k in required if not str(plan.get(k, "")).strip()]
    if missing:
        raise RuntimeError(f"Planner JSON missing keys: {missing}. Raw response saved to {raw_path}")

    # Do not modify visual prompts in code. Prompt policy belongs in rules/*.txt templates.
    response_json_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    clean_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    parsed = {k: str(plan[k]) for k in required}
    parsed_json_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
    return parsed


def build_song_context_prompt(rules: Dict[str, str], video_style: str, verses: List[Dict[str, Any]]) -> str:
    lyrics_text = "\n***\n".join(str(v.get("text", "")) for v in verses)
    return render_template(
        rules["song_context_user.txt"],
        {
            "VIDEO_STYLE": video_style,
            "ALL_LYRICS": lyrics_text,
        },
        "song_context_user.txt",
    )


def get_or_create_song_context(
    llm_generator,
    rules: Dict[str, str],
    video_style: str,
    verses: List[Dict[str, Any]],
    plans_dir: Path,
) -> Dict[str, Any]:
    """Load cached song_context.json or build it when visual generation needs it."""
    plans_dir.mkdir(parents=True, exist_ok=True)
    clean_path = plans_dir / "song_context.json"
    raw_path = plans_dir / "song_context_response.txt"
    response_json_path = plans_dir / "song_context_response.json"
    request_path = plans_dir / "song_context_request.txt"

    generator_path = plans_dir / "song_context_generator.json"
    same_generator = generator_path.exists() and load_json(generator_path).get("signature") == llm_generator.metadata()["signature"]
    if clean_path.exists() and same_generator:
        log(f"[stage] use cached song context: {clean_path}")
        return load_json(clean_path)

    log("[stage] build missing song context")
    if raw_path.exists():
        raw_path.unlink()

    prompt = build_song_context_prompt(rules, video_style, verses)
    save_prompt_debug(request_path, prompt)
    result = llm_generator.generate(LlmRequest(
        system_prompt=rules["song_context_system.txt"], prompt=prompt, seed=0,
        response_path=raw_path, sub_dir="song_context", debug_dir=plans_dir,
    ))
    raw_text = result.text
    ctx = extract_json_object(raw_text)
    defaults = {
        "song_summary": "",
        "main_characters": [],
        "recurring_locations": [],
        "recurring_props": [],
        "visual_motifs": [],
        "tone": "",
        "continuity_rules": [],
        "avoid": ["visible text", "letters", "captions", "subtitles", "signs", "logos", "watermarks"],
    }
    for k, v in defaults.items():
        ctx.setdefault(k, v)

    response_json_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    clean_path.write_text(json.dumps(ctx, ensure_ascii=False, indent=2), encoding="utf-8")
    return ctx


def format_verse_context(verse: Dict[str, Any], label: str) -> str:
    directives = verse.get("bracket_directives") or []
    directive_text = ""
    if directives:
        directive_text = "\nBracket directives:\n" + "\n".join(f"- {x}" for x in directives)
    return f"{label} {int(verse['index']):03d}:{directive_text}\nLyrics:\n{verse.get('text', '')}"


def build_local_context(verses: List[Dict[str, Any]], verse_index: int, radius: int) -> str:
    count = max(1, int(radius))
    if verse_index <= 0:
        early = verses[:count]
        return "Intro local context: early song setup and first verses.\n" + "\n\n".join(
            format_verse_context(v, "Verse") for v in early
        )

    total = len(verses)
    if verse_index > total:
        late = verses[max(0, total - count):]
        return "Outro/instrumental local context: final song resolution and nearby verses.\n" + "\n\n".join(
            format_verse_context(v, "Verse") for v in late
        )

    pos = verse_index - 1
    start = max(0, pos - count)
    end = min(total, pos + count + 1)
    parts: List[str] = []
    for i in range(start, end):
        label = "CURRENT VERSE" if i == pos else "previous/next context"
        parts.append(format_verse_context(verses[i], label))
    return "\n\n".join(parts)


def build_instrumental_local_context(verses: List[Dict[str, Any]], previous_verse_index: int, next_verse_index: Optional[int]) -> str:
    parts: List[str] = ["Instrumental local context: musical break between neighboring lyric sections."]
    if previous_verse_index and 1 <= previous_verse_index <= len(verses):
        parts.append(format_verse_context(verses[previous_verse_index - 1], "Previous verse"))
    if next_verse_index and 1 <= next_verse_index <= len(verses):
        parts.append(format_verse_context(verses[next_verse_index - 1], "Next verse"))
    return "\n\n".join(parts)


def generation_part_subdir(run_id: str, block_index: int, sub_index: int) -> str:
    return f"aligned_song/{run_id}/block_{block_index:03d}/part_{sub_index:03d}"


def extract_last_frame(video_path: Path, out_png: Path, ffmpeg: str) -> None:
    out_png.parent.mkdir(parents=True, exist_ok=True)
    run_cmd([
        ffmpeg,
        "-y",
        "-sseof", "-0.10",
        "-i", str(video_path),
        "-frames:v", "1",
        str(out_png),
    ])
    if not out_png.exists() or out_png.stat().st_size <= 0:
        raise RuntimeError(f"Failed to extract last frame: {out_png}")


def timed_line_segments_for_block(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not block_has_lyric_text(block):
        return []

    verse = block.get("verse") or {}
    start = float(block["start"])
    end = float(block["end"])
    segments: List[Dict[str, Any]] = []

    for line in verse.get("lines", []):
        raw_start = float(line.get("start", start))
        raw_end = float(line.get("end", end))
        ls = max(start, raw_start)
        le = min(end, raw_end)
        if le <= start or ls >= end or le <= ls:
            continue

        segments.append({
            "line_index": int(line.get("index", len(segments) + 1)),
            "start": ls,
            "end": le,
            "text": str(line.get("text", "")),
            "words": line.get("words", []),
        })

    return segments


def subrange_text_for_block(block: Dict[str, Any], sub_start: float, sub_end: float) -> str:
    if not block_has_lyric_text(block):
        return ""

    pieces: List[str] = []
    for seg in timed_line_segments_for_block(block):
        midpoint = (float(seg["start"]) + float(seg["end"])) / 2.0

        words = seg.get("words") or []
        if words and (float(seg["start"]) < sub_start or float(seg["end"]) > sub_end):
            selected_words = [
                str(w.get("text", ""))
                for w in words
                if sub_start <= ((float(w.get("start", sub_start)) + float(w.get("end", sub_end))) / 2.0) < sub_end
            ]
            text = " ".join(x for x in selected_words if x).strip()
            if text:
                pieces.append(text)
            continue

        if sub_start <= midpoint < sub_end:
            text = str(seg.get("text", "")).strip()
            if text:
                pieces.append(text)

    return "\n".join(pieces).strip()


def resolve_manual_boundary(
    descriptor: Dict[str, Any],
    candidates: List[float],
    allowed_start: float,
    allowed_end: float,
    config: Dict[str, Any],
    label: str,
) -> Dict[str, Any]:
    requested = float(descriptor["requested_time"])
    if not (allowed_start < requested < allowed_end):
        raise RuntimeError(
            f"{label} boundary {descriptor.get('source', requested)!r} is outside the allowed interval "
            f"{allowed_start:.3f}s..{allowed_end:.3f}s"
        )

    resolved = requested
    reason = "exact_requested_time"
    warning = False
    snap_mode = descriptor.get("mode")
    if snap_mode in {"snap_previous", "snap_next"}:
        radius = max(0.0, float(config.get("manual_boundary_snap_max_seconds", 10.0)))
        if snap_mode == "snap_previous":
            valid = sorted({
                float(value) for value in candidates
                if allowed_start < float(value) <= requested + 1e-6
                and requested - float(value) <= radius + 1e-6
            })
            if valid:
                resolved = valid[-1]
                reason = "previous_lyric_boundary"
            else:
                reason = "no_previous_candidate_within_radius"
                warning = True
                log(
                    f"WARNING: {label} {descriptor.get('source')} has no previous lyric boundary within "
                    f"{radius:.3f}s; using exact requested time {requested:.3f}s"
                )
        else:
            valid = sorted({
                float(value) for value in candidates
                if requested - 1e-6 <= float(value) < allowed_end
                and float(value) - requested <= radius + 1e-6
            })
            if valid:
                resolved = valid[0]
                reason = "next_lyric_boundary"
            else:
                reason = "no_next_candidate_within_radius"
                warning = True
                log(
                    f"WARNING: {label} {descriptor.get('source')} has no next lyric boundary within "
                    f"{radius:.3f}s; using exact requested time {requested:.3f}s"
                )

    result = dict(descriptor)
    result.update({
        "resolved_time": resolved,
        "shift_seconds": resolved - requested,
        "resolution_reason": reason,
        "warning": warning,
    })
    if descriptor.get("mode") in {"snap_previous", "snap_next"} and not warning:
        direction = "previous" if descriptor.get("mode") == "snap_previous" else "next"
        log(
            f"[boundary] {label}: {requested:.3f}s -> {resolved:.3f}s "
            f"({resolved - requested:+.3f}s, {direction} lyric boundary)"
        )
    return result


def split_boundaries_for_block(block: Dict[str, Any], config: Dict[str, Any]) -> List[float]:
    start = float(block["start"])
    end = float(block["end"])
    max_seconds = float(config["max_workflow_seconds"])
    recommended = float(config["recommended_workflow_seconds"])
    min_seconds = float(config["min_workflow_seconds"])

    # Keep a small safety margin below the workflow hard limit. Some aligned
    # word/line timestamps have millisecond rounding, and a visually harmless
    # 16.01s part would still exceed a 16.00s workflow cap. Subrange durations
    # are only planning windows for visual generation; the complete range clip
    # is retimed later to the exact lyric timeline.
    safe_max_seconds = max(0.5, max_seconds - 0.05)
    target_seconds = max(0.5, min(recommended, safe_max_seconds))
    min_seconds = max(0.01, min(min_seconds, safe_max_seconds))
    min_natural_piece_seconds = min(0.5, min_seconds)

    timed_lines = timed_line_segments_for_block(block)

    line_candidates = {start, end}
    word_candidates = {start, end}
    line_by_index: Dict[int, Dict[str, Any]] = {}
    if block_has_lyric_text(block):
        for seg in timed_lines:
            line_index = int(seg.get("line_index", len(line_by_index) + 1))
            line_by_index[line_index] = seg
            seg_end = float(seg["end"])
            if start < seg_end < end:
                line_candidates.add(seg_end)
                word_candidates.add(seg_end)
            for w in seg.get("words") or []:
                word_end = float(w.get("end", start))
                if start < word_end < end:
                    word_candidates.add(word_end)

    manual_candidates = {start, end}
    divider_positions = list(block.get("subrange_divider_after_lines", []))
    if not divider_positions and isinstance(block.get("verse"), dict):
        divider_positions = list((block.get("verse") or {}).get("subrange_divider_after_lines", []))
    for pos_raw in divider_positions:
        pos = int(pos_raw)
        seg = line_by_index.get(pos)
        if not seg:
            continue
        boundary = float(seg.get("end", start))
        if start < boundary < end:
            manual_candidates.add(boundary)

    timed_dividers = list(block.get("timed_subrange_boundaries", []))
    if not timed_dividers and isinstance(block.get("verse"), dict):
        timed_dividers = list((block.get("verse") or {}).get("timed_subrange_boundaries", []))
    lyric_end_candidates: List[float] = []
    for seg in timed_lines:
        lyric_end_candidates.append(float(seg.get("end", start)))
        lyric_end_candidates.extend(float(w.get("end", start)) for w in seg.get("words") or [])
    resolved_locked: List[Dict[str, Any]] = [
        resolve_manual_boundary(item, lyric_end_candidates, start, end, config, "subrange")
        for item in timed_dividers
    ]
    locked_candidates = sorted({start, end, *(float(item["resolved_time"]) for item in resolved_locked)})
    for left, right in zip(locked_candidates, locked_candidates[1:]):
        if right - left < min_seconds - 1e-6:
            raise RuntimeError(
                f"Locked subrange boundary in {format_range_id(int(block['block_index']))} creates a "
                f"{right - left:.3f}s part, below min_workflow_seconds={min_seconds:.3f}s"
            )
    if resolved_locked:
        block["resolved_timed_subrange_boundaries"] = resolved_locked

    def segment_duration(segment: Dict[str, Any]) -> float:
        return max(0.0, float(segment["end"]) - float(segment["start"]))

    def copy_segment(segment: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "start": float(segment["start"]),
            "end": float(segment["end"]),
            "start_boundary_kind": str(segment.get("start_boundary_kind", "range")),
        }

    def make_segments_from_boundaries(
        segment: Dict[str, Any],
        boundaries: List[float],
        internal_boundary_kind: str,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        cleaned: List[float] = []
        for value in boundaries:
            value = float(value)
            if not cleaned or value > cleaned[-1] + 0.01:
                cleaned.append(value)
        if len(cleaned) < 2:
            return [copy_segment(segment)]

        for i in range(len(cleaned) - 1):
            seg_start = cleaned[i]
            seg_end = cleaned[i + 1]
            if seg_end <= seg_start + 0.001:
                continue
            out.append({
                "start": seg_start,
                "end": seg_end,
                "start_boundary_kind": (
                    str(segment.get("start_boundary_kind", "range"))
                    if i == 0 else internal_boundary_kind
                ),
            })
        return out or [copy_segment(segment)]

    def split_at_all_candidates(
        segments: List[Dict[str, Any]],
        candidates: List[float],
        internal_boundary_kind: str,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for segment in segments:
            seg_start = float(segment["start"])
            seg_end = float(segment["end"])
            local = [
                float(c) for c in candidates
                if seg_start + 0.01 < float(c) < seg_end - 0.01
            ]
            if not local:
                out.append(copy_segment(segment))
                continue
            boundaries = [seg_start] + sorted(local) + [seg_end]
            out.extend(make_segments_from_boundaries(segment, boundaries, internal_boundary_kind))
        return out

    def split_using_candidates(
        segment: Dict[str, Any],
        candidates: List[float],
        internal_boundary_kind: str,
    ) -> List[Dict[str, Any]]:
        """Split a too-long segment using only supplied natural boundaries."""
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        if seg_end - seg_start <= safe_max_seconds + 1e-6:
            return [copy_segment(segment)]

        local_candidates = sorted(
            float(c) for c in candidates
            if seg_start + 0.01 < float(c) < seg_end - 0.01
        )
        if not local_candidates:
            return [copy_segment(segment)]

        boundaries = [seg_start]
        previous = seg_start
        while seg_end - previous > safe_max_seconds + 1e-6:
            earliest = previous + min_natural_piece_seconds
            latest = min(previous + safe_max_seconds, seg_end - min_natural_piece_seconds)
            if latest < earliest:
                break

            valid = [c for c in local_candidates if earliest <= c <= latest]
            if not valid:
                break

            remaining = seg_end - previous
            remaining_parts = max(2, int(math.ceil(remaining / safe_max_seconds)))
            desired = previous + remaining / remaining_parts
            desired = min(desired, previous + target_seconds)
            chosen = min(valid, key=lambda c: abs(c - desired))
            if chosen <= previous + 0.01:
                break
            boundaries.append(chosen)
            previous = chosen

        if boundaries[-1] < seg_end - 0.01:
            boundaries.append(seg_end)
        return make_segments_from_boundaries(segment, boundaries, internal_boundary_kind)

    def split_long_using_candidates_strategy(
        segments: List[Dict[str, Any]],
        candidates: List[float],
        internal_boundary_kind: str,
    ) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for segment in segments:
            out.extend(split_using_candidates(segment, candidates, internal_boundary_kind))
        return out

    def split_evenly(segment: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Final mechanical split when natural lyric boundaries are insufficient."""
        seg_start = float(segment["start"])
        seg_end = float(segment["end"])
        seg_duration = max(0.01, seg_end - seg_start)
        if seg_duration <= safe_max_seconds + 1e-6:
            return [copy_segment(segment)]

        pieces = max(2, int(round(seg_duration / target_seconds)))
        while seg_duration / pieces > safe_max_seconds:
            pieces += 1
        boundaries = [seg_start + seg_duration * i / pieces for i in range(pieces + 1)]
        return make_segments_from_boundaries(segment, boundaries, "even")

    def split_evenly_strategy(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for segment in segments:
            out.extend(split_evenly(segment))
        return out

    def merge_short_dp_strategy(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        n = len(segments)
        if n <= 1:
            return [copy_segment(x) for x in segments]

        durations = [segment_duration(x) for x in segments]
        prefix = [0.0]
        for d in durations:
            prefix.append(prefix[-1] + d)

        boundary_penalty = {
            "locked": 1_000_000_000_000.0,
            "manual": 30.0,
            "line": 10.0,
            "word": 4.0,
            "even": 1.0,
            "range": 1_000_000.0,
        }

        def group_duration(i: int, j: int) -> float:
            return prefix[j] - prefix[i]

        def contains_short_atom(i: int, j: int) -> bool:
            return any(durations[k] < min_seconds - 1e-6 for k in range(i, j))

        def removed_boundary_cost(i: int, j: int) -> float:
            cost = 0.0
            for k in range(i + 1, j):
                cost += boundary_penalty.get(str(segments[k].get("start_boundary_kind", "line")), 10.0)
            return cost

        def segment_cost(i: int, j: int, duration: float) -> float:
            cost = ((duration - target_seconds) / target_seconds) ** 2
            if duration < min_seconds - 1e-6:
                cost += 1_000_000.0
                cost += 1_000_000.0 * ((min_seconds - duration) / min_seconds) ** 2
            cost += removed_boundary_cost(i, j)
            return cost

        inf = float("inf")
        dp = [inf] * (n + 1)
        prev = [-1] * (n + 1)
        dp[0] = 0.0

        for j in range(1, n + 1):
            for i in range(j - 1, -1, -1):
                duration = group_duration(i, j)
                if duration > safe_max_seconds + 1e-6:
                    break
                if any(str(segments[k].get("start_boundary_kind")) == "locked" for k in range(i + 1, j)):
                    continue
                if j - i > 1 and not contains_short_atom(i, j):
                    continue
                cost = dp[i] + segment_cost(i, j, duration)
                if cost < dp[j] - 1e-9:
                    dp[j] = cost
                    prev[j] = i

        if prev[n] < 0:
            raise RuntimeError("Unable to build valid subranges after short-range merge DP")

        groups: List[Tuple[int, int]] = []
        cursor = n
        while cursor > 0:
            i = prev[cursor]
            if i < 0:
                raise RuntimeError("Broken subrange DP backtracking state")
            groups.append((i, cursor))
            cursor = i
        groups.reverse()

        out: List[Dict[str, Any]] = []
        for i, j in groups:
            out.append({
                "start": float(segments[i]["start"]),
                "end": float(segments[j - 1]["end"]),
                "start_boundary_kind": str(segments[i].get("start_boundary_kind", "range")),
            })
        return out

    segments: List[Dict[str, Any]] = [{
        "start": start,
        "end": end,
        "start_boundary_kind": "range",
    }]

    # Fixed strategy pipeline. Every strategy takes the current ordered subrange
    # list and returns a new ordered subrange list. Manual dividers are just the
    # first strategy; if a range has no --- markers it naturally returns the same
    # single subrange.
    segments = split_at_all_candidates(segments, locked_candidates, "locked")
    segments = split_at_all_candidates(segments, sorted(manual_candidates), "manual")
    segments = split_long_using_candidates_strategy(segments, sorted(line_candidates), "line")
    segments = split_long_using_candidates_strategy(segments, sorted(word_candidates), "word")
    segments = split_evenly_strategy(segments)
    segments = merge_short_dp_strategy(segments)

    for segment in segments:
        duration = segment_duration(segment)
        if duration > safe_max_seconds + 1e-6:
            raise RuntimeError(
                f"Unable to split {format_range_id(int(block['block_index']))}: "
                f"subrange duration {duration:.3f}s exceeds safe max {safe_max_seconds:.3f}s"
            )

    boundaries = [float(segments[0]["start"])]
    for segment in segments:
        end_value = float(segment["end"])
        if end_value > boundaries[-1] + 0.01:
            boundaries.append(end_value)
    if abs(boundaries[0] - start) > 0.01:
        boundaries.insert(0, start)
    else:
        boundaries[0] = start
    if abs(boundaries[-1] - end) > 0.01:
        boundaries.append(end)
    else:
        boundaries[-1] = end
    return boundaries

def build_subranges_for_block(block: Dict[str, Any], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    boundaries = split_boundaries_for_block(block, config)
    count = max(1, len(boundaries) - 1)
    out: List[Dict[str, Any]] = []

    for i in range(count):
        sub_start = float(boundaries[i])
        sub_end = float(boundaries[i + 1])
        text = "" if count == 1 else subrange_text_for_block(block, sub_start, sub_end)

        out.append({
            "block_index": int(block["block_index"]),
            "kind": str(block.get("kind", "verse")),
            "start": sub_start,
            "end": sub_end,
            "duration": max(0.01, sub_end - sub_start),
            "text": text,
            "text_mode": "whole_range" if count == 1 else "slice",
        })

    return out


def build_subrange_instruction(block: Dict[str, Any], subrange: Dict[str, Any], sub_index: int, sub_count: int) -> str:
    kind = str(block.get("kind", "verse"))
    directives = block.get("bracket_directives") or []
    directive_text = "\n".join(f"- {x}" for x in directives) if directives else "- none"
    full_text = str(block.get("text", "")).strip() or "(no sung lyrics in this semantic range)"
    sub_text = str(subrange.get("text", "")).strip()

    if subrange.get("text_mode") == "whole_range":
        subrange_section = (
            "This subrange covers the entire semantic range. "
            "Use FULL SEMANTIC RANGE LYRICS / RANGE TEXT as the current factual source."
        )
    elif sub_text:
        subrange_section = (
            "CURRENT SUBRANGE TEXT \u2014 HIGHEST FACTUAL PRIORITY:\n"
            + sub_text
            + "\n\nDepict this current subrange as the main action. "
              "Use the full semantic range only for continuity and meaning."
        )
    else:
        subrange_section = (
            "CURRENT SUBRANGE \u2014 HIGHEST FACTUAL PRIORITY:\n"
            "No lyrics are sung in this subrange. Continue the visual motion of this semantic range."
        )

    return (
        f"SEMANTIC RANGE:\n"
        f"Block index: {int(block['block_index']):03d}\n"
        f"Kind: {kind}\n"
        f"Subrange index: {int(sub_index)} (count {int(sub_count)})\n"
        f"Time: {float(subrange['start']):.3f}s..{float(subrange['end']):.3f}s\n\n"
        f"BRACKET DIRECTIVES, metadata for the whole semantic range:\n{directive_text}\n\n"
        f"FULL SEMANTIC RANGE LYRICS / RANGE TEXT:\n{full_text}\n\n"
        f"{subrange_section}\n\n"
        "Priority rules:\n"
        "- Always follow VISUAL STYLE for medium, look, palette, character design, camera and rendering.\n"
        "- For factual action, follow CURRENT SUBRANGE when it provides text.\n"
        "- If this is not the first subrange of the semantic range, treat it as a continuation shot unless the current subrange explicitly changes subject or location.\n"
        "- Bracket directives are metadata, not sung lyrics and never visible text.\n"
        "- Do not render captions, signs, section labels, lyric cards, or written words."
    )


def concat_or_copy_subclips(subclips: List[Path], out_path: Path, ffmpeg: str) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not subclips:
        raise RuntimeError(f"No subclips to concat into {out_path}")
    if len(subclips) == 1:
        shutil.copy2(subclips[0], out_path)
        return
    concat_videos(subclips, out_path, ffmpeg)


def get_audio_duration(path: Path, ffprobe: str) -> float:
    return ffprobe_duration(path, ffprobe)


def prepare_audio_full_mix(mode: str, a: Path, b: Optional[Path], out_dir: Path, ffmpeg: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    full_mix = out_dir / "full_mix.wav"

    if mode == "stems":
        assert b is not None
        vocals_wav = out_dir / "vocals_48k.wav"
        inst_wav = out_dir / "instrumental_48k.wav"
        run_cmd([ffmpeg, "-y", "-i", str(a), "-ar", "48000", "-ac", "2", str(vocals_wav)])
        run_cmd([ffmpeg, "-y", "-i", str(b), "-ar", "48000", "-ac", "2", str(inst_wav)])
        run_cmd([
            ffmpeg, "-y", "-i", str(inst_wav), "-i", str(vocals_wav),
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0,alimiter=limit=0.97",
            "-ar", "48000", "-ac", "2", str(full_mix),
        ])
    else:
        run_cmd([ffmpeg, "-y", "-i", str(a), "-ar", "48000", "-ac", "2", str(full_mix)])

    return full_mix


def render_audio_for_timeline(full_mix: Path, out_dir: Path, ffmpeg: str, audio_end: Optional[float]) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    render = out_dir / "render_audio.wav"

    if audio_end is None:
        run_cmd([ffmpeg, "-y", "-i", str(full_mix), "-ar", "48000", "-ac", "2", str(render)])
    else:
        run_cmd([
            ffmpeg, "-y",
            "-i", str(full_mix),
            "-t", f"{audio_end:.3f}",
            "-ar", "48000",
            "-ac", "2",
            str(render),
        ])
    return render

def make_nonlyrical_block_text(block: Dict[str, Any]) -> str:
    directives = block.get("bracket_directives") or []
    label = ", ".join(str(x) for x in directives) if directives else "empty"
    return (
        f"Non-lyrical song block ({label}). No sung words in this section; "
        "use the music, surrounding lyrics and bracket metadata for visual continuity."
    )


def last_effective_lyric_end_before_explicit_gap(block: Dict[str, Any], default_end: float, config: Dict[str, Any]) -> float:
    """Return a lyric end suitable for an explicit following non-lyrical block.

    Forced lyric structure wins over stretched alignment tails. When a lyric line
    ends with zero-duration words far after the last real word, keep the empty
    block's gap rather than letting the previous lyric block consume it.
    """
    min_tail = max(0.5, float(config.get("explicit_gap_min_tail_seconds", 2.0)))
    nonzero_ends: List[float] = []
    zeroish_ends: List[float] = []
    for line in block.get("lines", []) or []:
        for w in line.get("words", []) or []:
            try:
                ws = float(w.get("start", 0.0))
                we = float(w.get("end", ws))
            except Exception:
                continue
            if we - ws >= 0.08:
                nonzero_ends.append(we)
            else:
                zeroish_ends.append(we)
    if not nonzero_ends or not zeroish_ends:
        return default_end
    natural_end = max(nonzero_ends)
    stretched_end = max(zeroish_ends + [default_end])
    if stretched_end - natural_end >= min_tail:
        return natural_end
    return default_end


def coalesce_short_nonlyrical_blocks(
    blocks: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Fold unusably short musical/meta-only ranges into adjacent visual ranges.

    A block is considered non-lyrical when ``block_has_lyric_text()`` finds no
    sung text after metadata/control lines are ignored.  Explicit instrumental
    sections therefore remain standalone when they have a real time span, while
    zero/near-zero markers can never become an Rxxx that is shorter than
    ``min_workflow_seconds``.

    This is intentionally a timeline invariant, not a song-specific workaround:
    it applies to intro/interlude/outro/meta-only/empty blocks for any lyrics.
    """
    if not blocks:
        return []

    min_visual = max(0.01, float(config.get("min_workflow_seconds", 1.0)))
    normalized: List[Dict[str, Any]] = []
    pending_prefix: List[Dict[str, Any]] = []

    def marker_from(block: Dict[str, Any], duration: float) -> Dict[str, Any]:
        return {
            "kind": block.get("kind"),
            "start": float(block.get("start", 0.0)),
            "end": float(block.get("end", 0.0)),
            "duration": duration,
            "bracket_directives": list(block.get("bracket_directives", [])),
            "song_block_index": block.get("song_block_index"),
            "semantic_boundary_before": block.get("semantic_boundary_before"),
            "reason": "nonlyrical_range_shorter_than_min_workflow",
        }

    def refresh_owner(owner: Dict[str, Any]) -> None:
        owner["duration"] = max(0.01, float(owner["end"]) - float(owner["start"]))
        verse = owner.get("verse")
        if isinstance(verse, dict):
            verse["start"] = float(owner["start"])
            verse["end"] = float(owner["end"])
            verse["duration"] = float(owner["duration"])

    for block in blocks:
        start = float(block.get("start", 0.0))
        end = float(block.get("end", start))
        duration = max(0.0, end - start)

        if (not block_has_lyric_text(block)) and duration < min_visual - 1e-6:
            marker = marker_from(block, duration)
            if normalized:
                # Internal/trailing short musical markers belong to the previous
                # visual range.  This preserves continuity and prevents a
                # standalone sub-minimum workflow.
                owner = normalized[-1]
                owner.setdefault("embedded_nonlyrical_sections", []).append(marker)
                owner["end"] = max(float(owner["end"]), end)
                refresh_owner(owner)
            else:
                # A short prefix has no previous owner; defer it until the first
                # renderable range and expand that range backwards.
                pending_prefix.append(marker)

            log(
                f"[timeline] fold short non-lyrical marker {duration:.3f}s "
                f"(< min_workflow_seconds={min_visual:.3f}s) into neighboring range"
            )
            continue

        if pending_prefix:
            block.setdefault("embedded_nonlyrical_sections", []).extend(pending_prefix)
            block["start"] = min(
                float(block.get("start", 0.0)),
                min(float(item["start"]) for item in pending_prefix),
            )
            refresh_owner(block)
            pending_prefix = []

        normalized.append(block)

    if pending_prefix:
        if normalized:
            owner = normalized[-1]
            owner.setdefault("embedded_nonlyrical_sections", []).extend(pending_prefix)
            owner["end"] = max(
                float(owner["end"]),
                max(float(item["end"]) for item in pending_prefix),
            )
            refresh_owner(owner)
        else:
            # Degenerate all-metadata song: merge the markers into one range
            # rather than manufacturing several 0.010s ranges.
            first = blocks[0]
            start = min(float(item["start"]) for item in pending_prefix)
            end = max(float(item["end"]) for item in pending_prefix)
            merged = dict(first)
            merged["start"] = start
            merged["end"] = max(start + 0.01, end)
            merged["duration"] = max(0.01, merged["end"] - merged["start"])
            merged["embedded_nonlyrical_sections"] = list(pending_prefix)
            normalized.append(merged)

    for new_index, block in enumerate(normalized):
        block["block_index"] = new_index

    return normalized


def _block_first_word(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    verse = block.get("verse") if isinstance(block.get("verse"), dict) else block
    for line in verse.get("lines", []) or []:
        words = line.get("words", []) or []
        if words:
            return words[0]
    return None


def _block_last_word(block: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    verse = block.get("verse") if isinstance(block.get("verse"), dict) else block
    for line in reversed(verse.get("lines", []) or []):
        words = line.get("words", []) or []
        if words:
            return words[-1]
    return None


def _set_block_last_lyric_end(block: Dict[str, Any], boundary: float) -> None:
    verse = block.get("verse") if isinstance(block.get("verse"), dict) else block
    lines = verse.get("lines", []) or []
    for line in reversed(lines):
        words = line.get("words", []) or []
        if not words:
            continue
        word = words[-1]
        ws = float(word.get("start", boundary))
        word["end"] = max(ws + 0.01, boundary)
        word["timing_estimated"] = True
        word["timing_source"] = "semantic_boundary_word_edge_repair"
        line["end"] = float(word["end"])
        line["timing_estimated"] = True
        return


def _set_block_first_lyric_start(block: Dict[str, Any], boundary: float) -> None:
    verse = block.get("verse") if isinstance(block.get("verse"), dict) else block
    lines = verse.get("lines", []) or []
    for line in lines:
        words = line.get("words", []) or []
        if not words:
            continue
        word = words[0]
        we = float(word.get("end", boundary + 0.01))
        word["start"] = min(we - 0.01, boundary)
        word["timing_estimated"] = True
        word["timing_source"] = "semantic_boundary_word_edge_repair"
        line["start"] = float(word["start"])
        line["timing_estimated"] = True
        return


def enforce_word_safe_semantic_boundaries(
    blocks: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Make every R boundary a word edge, never the interior of a lyric word.

    Automatic boundaries are clamped into the safe gap between the final word
    of the left range and the first word of the right range. If bad alignment
    makes those word intervals overlap, repair the two edge words first and use
    one shared boundary. This keeps subtitles, planner ranges and video ranges
    on the same semantic edge instead of clipping only at render time.
    """
    if len(blocks) < 2:
        return blocks

    min_unit = max(0.01, float(config.get("min_karaoke_unit_seconds", 0.01)))
    for i in range(len(blocks) - 1):
        left = blocks[i]
        right = blocks[i + 1]
        current = float(left.get("end", right.get("start", 0.0)))
        left_word = _block_last_word(left) if block_has_lyric_text(left) else None
        right_word = _block_first_word(right) if block_has_lyric_text(right) else None
        lower = float(left_word.get("end", current)) if left_word is not None else float(left.get("start", current))
        upper = float(right_word.get("start", current)) if right_word is not None else float(right.get("end", current))

        if left_word is not None and right_word is not None and lower > upper + 1e-6:
            # Cross-range word overlap: choose the more credible anchor; if both
            # are similarly credible, split the overlap. Then make both words end
            # and start exactly at that repaired boundary.
            lp = _word_probability(left_word)
            rp = _word_probability(right_word)
            if lp + 0.20 < rp:
                boundary = upper
            elif rp + 0.20 < lp:
                boundary = lower
            else:
                boundary = (lower + upper) * 0.5

            left_start = float(left_word.get("start", boundary - min_unit))
            right_end = float(right_word.get("end", boundary + min_unit))
            boundary = max(left_start + min_unit, min(right_end - min_unit, boundary))
            _set_block_last_lyric_end(left, boundary)
            _set_block_first_lyric_start(right, boundary)
            lower = upper = boundary
        elif lower <= upper:
            boundary = min(max(current, lower), upper)
        else:
            boundary = current

        left["end"] = boundary
        left["duration"] = max(0.01, boundary - float(left["start"]))
        right["start"] = boundary
        right["duration"] = max(0.01, float(right["end"]) - boundary)
        left.setdefault("word_safe_boundary_after", {})["time"] = boundary
        right.setdefault("word_safe_boundary_before", {})["time"] = boundary

    # Reindexing is intentionally independent from timing; both R and S are
    # zero-based externally and internally.
    for idx, block in enumerate(blocks):
        block["block_index"] = idx
    return blocks


def validate_lyrics_inside_semantic_ranges(blocks: List[Dict[str, Any]]) -> None:
    """Fail loudly if a lyric word still crosses its owning semantic range.

    The only tolerated exception is a tiny overrun of the *final lyric word*
    beyond the final full-audio boundary.  Vocal stems produced by source
    separation can be a few frames / encoder samples longer than the original
    mix even when both files share the same musical timebase.  In that case we
    clamp the terminal word to the final timeline edge instead of rejecting an
    otherwise valid alignment.  Internal semantic boundaries remain strict.
    """
    eps = 0.012
    terminal_audio_tail_tolerance = 0.150
    last_block_index = len(blocks) - 1

    for block_pos, block in enumerate(blocks):
        if not block_has_lyric_text(block):
            continue
        start = float(block["start"])
        end = float(block["end"])
        verse = block.get("verse") if isinstance(block.get("verse"), dict) else block
        terminal_word = _block_last_word(block) if block_pos == last_block_index else None

        for line in verse.get("lines", []) or []:
            for word in line.get("words", []) or []:
                ws = float(word.get("start", start))
                we = float(word.get("end", ws))

                # Source-separated vocals may end a few milliseconds after the
                # full mix. Only forgive this at the actual song tail, and only
                # for the final lyric word. Never relax internal R boundaries.
                if (
                    word is terminal_word
                    and we > end + eps
                    and we - end <= terminal_audio_tail_tolerance
                    and ws >= end - terminal_audio_tail_tolerance
                ):
                    clamped_start = min(ws, end)
                    word["start"] = clamped_start
                    word["end"] = end
                    word["timing_estimated"] = True
                    word["timing_source"] = "terminal_audio_end_clamp"
                    line["end"] = end
                    if float(line.get("start", clamped_start)) > end:
                        line["start"] = end
                    line["timing_estimated"] = True
                    ws = float(word["start"])
                    we = float(word["end"])

                if ws < start - eps or we > end + eps:
                    raise RuntimeError(
                        f"Lyric word crosses semantic range {format_range_id(int(block['block_index']))}: "
                        f"{word.get('text')!r} {ws:.3f}..{we:.3f} outside {start:.3f}..{end:.3f}"
                    )


def make_timeline_blocks(
    all_verses: List[Dict[str, Any]],
    selected_verses: List[Dict[str, Any]],
    audio_duration: float,
    has_limit: bool,
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    """Create a continuous visual timeline from explicit lyrics.txt blocks.

    The range list follows lyrics.txt exactly: every *** segment becomes one
    timeline block. Blocks with lyrics use alignment timing; blocks without
    lyrics fill the gap between surrounding explicit blocks or the audio edge.

    The `kind` field drives planner template selection: lyric blocks are
    `verse`; non-lyrical blocks become intro/instrumental/outro according to
    their position.
    """
    source_blocks = selected_verses
    if not source_blocks:
        return [], None

    full_song = (len(selected_verses) >= len(all_verses)) and not has_limit
    timeline_end = float(audio_duration) if full_song else float(selected_verses[-1].get("end", audio_duration))
    audio_end = None if full_song else timeline_end

    blocks: List[Dict[str, Any]] = []
    n = len(source_blocks)

    def next_lyrical_index(pos: int) -> Optional[int]:
        for j in range(pos + 1, n):
            if block_has_lyric_text(source_blocks[j]):
                return j
        return None

    def prev_lyrical_index(pos: int) -> Optional[int]:
        for j in range(pos - 1, -1, -1):
            if block_has_lyric_text(source_blocks[j]):
                return j
        return None

    def nonlyrical_kind(prev_i: Optional[int], next_i: Optional[int]) -> str:
        if prev_i is None and next_i is not None:
            return "intro"
        if prev_i is not None and next_i is None:
            return "outro"
        return "instrumental"

    lyric_start: Dict[int, float] = {}
    lyric_end: Dict[int, float] = {}
    for i, src in enumerate(source_blocks):
        if not block_has_lyric_text(src):
            continue
        raw_start = max(0.0, float(src.get("start", 0.0)))
        raw_end = max(raw_start + 0.01, float(src.get("end", raw_start + 0.01)))
        if next_lyrical_index(i) is None and not any(not block_has_lyric_text(source_blocks[j]) for j in range(i + 1, n)):
            raw_end = max(raw_end, timeline_end)
        if i + 1 < n and not block_has_lyric_text(source_blocks[i + 1]):
            raw_end = last_effective_lyric_end_before_explicit_gap(src, raw_end, config)
        lyric_start[i] = raw_start
        lyric_end[i] = min(max(raw_start + 0.01, raw_end), timeline_end)

    i = 0
    while i < n:
        src = source_blocks[i]
        if block_has_lyric_text(src):
            start = 0.0 if i == 0 else float(blocks[-1]["end"])
            raw_start = lyric_start.get(i, start)
            if not blocks:
                start = 0.0 if raw_start > 0.0 else raw_start
            else:
                start = float(blocks[-1]["end"])
            j = next_lyrical_index(i)
            if i + 1 < n and not block_has_lyric_text(source_blocks[i + 1]):
                end = lyric_end[i]
            elif j is not None:
                end = lyric_start[j]
            else:
                end = timeline_end
            end = max(start + 0.01, min(float(end), timeline_end))
            src["start"] = start
            src["end"] = end
            src["duration"] = max(0.01, end - start)
            blocks.append({
                "block_index": len(blocks),
                "kind": "verse",
                "verse_index": int(src.get("index", len(blocks) + 1)),
                "song_block_index": int(src.get("index", len(blocks) + 1)),
                "start": start,
                "end": end,
                "duration": max(0.01, end - start),
                "text": src.get("text", ""),
                "verse": src,
                "lyric_start": lyric_start.get(i, start),
                "lyric_end": lyric_end.get(i, end),
                "visual_preroll": max(0.0, lyric_start.get(i, start) - start),
                "bracket_directives": list(src.get("bracket_directives", [])),
                "subrange_divider_after_lines": list(src.get("subrange_divider_after_lines", [])),
                "timed_subrange_boundaries": list(src.get("timed_subrange_boundaries", [])),
                "semantic_boundary_before": src.get("semantic_boundary_before"),
            })
            i += 1
            continue

        run_start_i = i
        while i < n and not block_has_lyric_text(source_blocks[i]):
            i += 1
        run_end_i = i
        prev_i = prev_lyrical_index(run_start_i)
        next_i = next_lyrical_index(run_end_i - 1)
        gap_start = float(blocks[-1]["end"]) if blocks else 0.0
        if prev_i is not None:
            gap_start = max(gap_start, lyric_end.get(prev_i, gap_start))
        gap_end = lyric_start[next_i] if next_i is not None else timeline_end
        gap_end = max(gap_start + 0.01, min(float(gap_end), timeline_end))
        count = run_end_i - run_start_i
        kind = nonlyrical_kind(prev_i, next_i)
        for k in range(count):
            src_empty = source_blocks[run_start_i + k]
            start = gap_start + (gap_end - gap_start) * k / count
            end = gap_start + (gap_end - gap_start) * (k + 1) / count
            src_empty["start"] = start
            src_empty["end"] = end
            src_empty["duration"] = max(0.01, end - start)
            blocks.append({
                "block_index": len(blocks),
                "kind": kind,
                "verse_index": int(src_empty.get("index", len(blocks) + 1)),
                "song_block_index": int(src_empty.get("index", len(blocks) + 1)),
                "previous_verse_index": int(source_blocks[prev_i].get("index")) if prev_i is not None else 0,
                "next_verse_index": int(source_blocks[next_i].get("index")) if next_i is not None else None,
                "start": start,
                "end": end,
                "duration": max(0.01, end - start),
                "text": "",
                "verse": src_empty,
                "bracket_directives": list(src_empty.get("bracket_directives", [])),
                "subrange_divider_after_lines": list(src_empty.get("subrange_divider_after_lines", [])),
                "timed_subrange_boundaries": list(src_empty.get("timed_subrange_boundaries", [])),
                "semantic_boundary_before": src_empty.get("semantic_boundary_before"),
                "section_text": make_nonlyrical_block_text(src_empty),
            })

    if blocks:
        blocks[-1]["end"] = max(float(blocks[-1]["end"]), timeline_end)
        blocks[-1]["duration"] = max(0.01, float(blocks[-1]["end"]) - float(blocks[-1]["start"]))

    if len(blocks) > 1:
        boundaries = [float(blocks[0]["start"])] + [float(block["end"]) for block in blocks]
        resolved_semantic: Dict[int, Dict[str, Any]] = {}
        for boundary_index in range(1, len(blocks)):
            descriptor = source_blocks[boundary_index].get("semantic_boundary_before")
            if not descriptor or descriptor.get("requested_time") is None:
                continue
            previous_candidates: List[float] = []
            for source in source_blocks[:boundary_index]:
                for line in source.get("lines", []) or []:
                    previous_candidates.append(float(line.get("end", 0.0)))
                    previous_candidates.extend(
                        float(word.get("end", 0.0)) for word in line.get("words", []) or []
                    )
            resolved = resolve_manual_boundary(
                descriptor,
                previous_candidates,
                0.0,
                timeline_end,
                config,
                f"semantic boundary before R{boundary_index:03d}",
            )
            boundaries[boundary_index] = float(resolved["resolved_time"])
            resolved_semantic[boundary_index] = resolved

        for boundary_index in range(1, len(boundaries)):
            if boundaries[boundary_index] <= boundaries[boundary_index - 1] + 0.001:
                raise RuntimeError(
                    f"Semantic boundaries are not monotonic near R{boundary_index:03d}: "
                    f"{boundaries[boundary_index - 1]:.3f}s then {boundaries[boundary_index]:.3f}s"
                )

        for block_index, block in enumerate(blocks):
            block["start"] = boundaries[block_index]
            block["end"] = boundaries[block_index + 1]
            block["duration"] = max(0.01, block["end"] - block["start"])
            if block_index in resolved_semantic:
                block["resolved_semantic_boundary_before"] = resolved_semantic[block_index]
            verse = block.get("verse")
            if isinstance(verse, dict):
                verse["start"] = block["start"]
                verse["end"] = block["end"]
                verse["duration"] = block["duration"]

    # Enforce the visual-timeline invariant *after* all automatic and manual
    # semantic-boundary resolution.  This catches both inferred zero-length
    # musical markers and an explicit boundary that leaves a metadata-only block
    # too short to render.
    blocks = coalesce_short_nonlyrical_blocks(blocks, config)
    blocks = enforce_word_safe_semantic_boundaries(blocks, config)
    validate_lyrics_inside_semantic_ranges(blocks)

    return blocks, audio_end


def clip_filename_for_block(block: Dict[str, Any]) -> str:
    idx = int(block["block_index"])
    return f"clip_{idx:03d}.mp4"


def block_clip_path(clips_dir: Path, block: Dict[str, Any]) -> Path:
    return clips_dir / clip_filename_for_block(block)


def relpath_or_abs(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def validate_unscaled_clip_for_timeline(
    block: Dict[str, Any],
    unscaled_clip: Path,
    output_root: Path,
    config: Dict[str, Any],
    ffprobe_cmd: str,
) -> Dict[str, Any]:
    block_i = int(block["block_index"])
    if not unscaled_clip.exists():
        raise FileNotFoundError(
            f"Unscaled clip not found for {format_range_id(block_i)}: {unscaled_clip}\n"
            f"Generate this range with --rework {block_i} or run clean generation."
        )

    target_duration = max(0.1, float(block.get("duration", 0.0)))
    source_duration = ffprobe_duration(unscaled_clip, ffprobe_cmd)
    tolerance = max(0.0, float(config.get("clip_duration_tolerance_ratio", 0.05)))
    ratio_delta = abs(source_duration - target_duration) / max(target_duration, 0.001)
    ok = ratio_delta <= tolerance
    info = {
        "range_id": block_i,
        "range_label": format_range_id(block_i),
        "clip": relpath_or_abs(unscaled_clip, output_root),
        "source_duration": source_duration,
        "target_duration": target_duration,
        "ratio_delta": ratio_delta,
        "tolerance": tolerance,
        "validated": ok,
    }
    if not ok:
        raise RuntimeError(
            f"Unscaled clip duration is not compatible with current timeline for {format_range_id(block_i)}:\n"
            f"  clip duration={source_duration:.3f}s current range duration={target_duration:.3f}s "
            f"ratio_delta={ratio_delta:.3f}\n"
            f"  tolerance={tolerance:.3f}. Add --rework {block_i}, run clean generation for this range, "
            f"or increase clip_duration_tolerance_ratio in input/config.json.\n"
            f"  clip={unscaled_clip}"
        )
    return info


def load_continuity_from_plans(plans_dir: Path, before_block_index: int) -> List[Dict[str, str]]:
    """Load previous saved scene summaries for visual continuity."""
    out: List[Dict[str, str]] = []
    seen: set[str] = set()
    if not plans_dir.exists():
        return out

    for p in sorted(plans_dir.glob("plan_*.json")):
        if p.name.endswith("_final_result.json"):
            continue
        m = re.match(r"plan_(\d+)(?:_part_(\d+))?\.json$", p.name)
        if not m:
            continue

        idx = int(m.group(1))
        if idx >= before_block_index:
            continue

        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue

        summary = str(data.get("scene_summary", "")).strip()
        if summary:
            segment = f"{idx}.{int(m.group(2))}" if m.group(2) else str(idx)
            if segment not in seen:
                out.append({"segment": segment, "scene_summary": summary})
                seen.add(segment)

    return out


def write_alignment_diagnostics_report(diagnostics: Dict[str, Any], out_path: Path) -> None:
    summary = diagnostics.get("summary", {})
    lines: List[str] = [
        "alignment diagnostics",
        f"ranges          : {summary.get('ranges', 0)}",
        f"lines           : {summary.get('lines', 0)}",
        f"good lines      : {summary.get('good_lines', 0)}",
        f"warning lines   : {summary.get('warning_lines', 0)}",
        f"estimated lines : {summary.get('estimated_lines', 0)}",
        f"collapsed lines : {summary.get('collapsed_lines', 0)}",
        f"missing lines   : {summary.get('missing_lines', 0)}",
        f"partial lines   : {summary.get('partial_lines', 0)}",
        "",
    ]

    for r in diagnostics.get("ranges", []):
        status = r.get("status", "")
        lines.append(
            f"range {int(r.get('range_index', 0)):03d}: {status}; "
            f"duration={float(r.get('duration', 0.0)):.2f}s; "
            f"{float(r.get('start', 0.0)):.2f}..{float(r.get('end', 0.0)):.2f}; "
            f"{r.get('text_preview', '')}"
        )
        for item in r.get("lines", []):
            st = str(item.get("status", ""))
            if st == "GOOD" and not item.get("timing_estimated"):
                continue
            issues = item.get("issues", []) or []
            issue_text = "; ".join(str(x) for x in issues[:4])
            lines.append(
                f"  line {int(item.get('line_index', 0)):02d}: {st}; "
                f"final={float(item.get('final_start', item.get('start', 0.0))):.2f}.."
                f"{float(item.get('final_end', item.get('end', 0.0))):.2f}; "
                f"matched={int(item.get('matched_words', 0))}/{int(item.get('expected_words', 0))}; "
                f"estimated={bool(item.get('timing_estimated', False))}; "
                f"{issue_text}"
            )
            text = str(item.get("text", "")).strip()
            if text:
                lines.append(f"    {text}")
        lines.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_alignment_match_report(verses: List[Dict[str, Any]], out_path: Path) -> None:
    lines: List[str] = []
    total_expected = 0
    total_matched = 0
    total_fuzzy = 0
    total_mismatch = 0
    total_missing = 0
    total_extra = 0
    warning_ranges = 0

    for verse in verses:
        m = verse.get("alignment_match", {})
        expected = int(m.get("expected", 0))
        matched = int(m.get("matched", 0))
        fuzzy = int(m.get("fuzzy", 0))
        mismatch = int(m.get("mismatch", 0))
        missing = int(m.get("missing", 0))
        extra = int(m.get("extra", 0))
        total_expected += expected
        total_matched += matched
        total_fuzzy += fuzzy
        total_mismatch += mismatch
        total_missing += missing
        total_extra += extra

        bad = mismatch + missing
        status = "OK"
        if bad or extra or fuzzy:
            status = "WARN"
            warning_ranges += 1

        lines.append(
            f"range {int(verse.get('index', 0)):03d}: {status}; "
            f"duration={float(verse.get('duration', 0)):.2f}s; "
            f"expected={expected}; matched={matched}; fuzzy={fuzzy}; "
            f"missing={missing}; mismatch={mismatch}; extra_actual={extra}; "
            f"boundary={m.get('boundary_reason', '')}"
        )

        events = m.get("events", [])
        interesting = [e for e in events if e.get("status") != "match"]
        for e in interesting[:25]:
            status_e = e.get("status")
            if status_e == "extra_actual":
                lines.append(
                    f"  extra actual {e.get('actual')!r} "
                    f"{float(e.get('start', 0)):.2f}..{float(e.get('end', 0)):.2f}"
                )
            elif status_e == "missing_expected":
                lines.append(f"  missing expected {e.get('expected')!r}; reason={e.get('reason', '')}")
            else:
                lines.append(
                    f"  {status_e}: expected={e.get('expected')!r}; actual={e.get('actual')!r}; "
                    f"sim={float(e.get('similarity', 0)):.3f}"
                )
        if len(interesting) > 25:
            lines.append(f"  ... {len(interesting) - 25} more non-exact events")

    lines.insert(0, f"total expected words : {total_expected}")
    lines.insert(1, f"total matched words  : {total_matched}")
    lines.insert(2, f"total fuzzy matches  : {total_fuzzy}")
    lines.insert(3, f"total missing words  : {total_missing}")
    lines.insert(4, f"total mismatches     : {total_mismatch}")
    lines.insert(5, f"total extra actual   : {total_extra}")
    lines.insert(6, f"warning ranges       : {warning_ranges}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_timeline_manifest(
    out_path: Path,
    output_root: Path,
    blocks: List[Dict[str, Any]],
    clips: List[Path],
    final_audio: Path,
    ass_path: Path,
    final_path: Path,
    subtitle_preview_path: Optional[Path],
    audio_mode: str,
    alignment_mode: str,
    limit: int,
    rework: Optional[List[int]],
    run_id: str,
    generation_info: Optional[Dict[int, Dict[str, Any]]] = None,
) -> None:
    generation_info = generation_info or {}
    items: List[Dict[str, Any]] = []
    for block, clip in zip(blocks, clips):
        block_index = int(block["block_index"])
        item = {
            "block_index": block_index,
            "kind": block["kind"],
            "verse_index": int(block.get("verse_index", block["block_index"])),
            "start": float(block["start"]),
            "end": float(block["end"]),
            "duration": float(block["duration"]),
            "text": block.get("text", ""),
            "bracket_directives": block.get("bracket_directives", []),
            "clip": str(clip.relative_to(output_root)) if clip.is_relative_to(output_root) else str(clip),
            "plan": str((output_root / "work" / "plans" / f"plan_{block_index:03d}.json").relative_to(output_root)),
        }
        if block_index in generation_info:
            item["generation"] = generation_info[block_index]
        items.append(item)

    manifest = {
        "output_dir": str(output_root),
        "run_id": run_id,
        "audio_mode": audio_mode,
        "alignment_mode": alignment_mode,
        "limit": limit,
        "rework": rework or [],
        "timeline_start": 0.0,
        "timeline_end": float(blocks[-1]["end"]) if blocks else 0.0,
        "audio": str(final_audio.relative_to(output_root)) if final_audio.is_relative_to(output_root) else str(final_audio),
        "subtitles": str(ass_path.relative_to(output_root)) if ass_path.is_relative_to(output_root) else str(ass_path),
        "subtitle_preview": (
            str(subtitle_preview_path.relative_to(output_root))
            if subtitle_preview_path is not None and subtitle_preview_path.is_relative_to(output_root)
            else (str(subtitle_preview_path) if subtitle_preview_path is not None else None)
        ),
        "final": str(final_path.relative_to(output_root)) if final_path.is_relative_to(output_root) else str(final_path),
        "blocks": items,
    }
    write_json(out_path, manifest)


def write_preview_manifest(
    out_path: Path,
    output_root: Path,
    blocks: List[Dict[str, Any]],
    final_audio: Path,
    ass_path: Path,
    subtitle_preview_path: Path,
    audio_mode: str,
    alignment_mode: str,
    limit: int,
    rework: Optional[List[int]],
    run_id: str,
) -> None:
    items: List[Dict[str, Any]] = []
    for block in blocks:
        block_index = int(block["block_index"])
        items.append({
            "block_index": block_index,
            "kind": block["kind"],
            "verse_index": int(block.get("verse_index", block["block_index"])),
            "start": float(block["start"]),
            "end": float(block["end"]),
            "duration": float(block["duration"]),
            "text": block.get("text", ""),
            "bracket_directives": block.get("bracket_directives", []),
            "clip": None,
            "plan": None,
            "generation": {
                "run_id": run_id,
                "generated_in_this_run": False,
                "preview_subtitles_only": True,
            },
        })
    manifest = {
        "output_dir": str(output_root),
        "run_id": run_id,
        "audio_mode": audio_mode,
        "alignment_mode": alignment_mode,
        "limit": limit,
        "rework": rework or [],
        "preview_subtitles_only": True,
        "timeline_start": 0.0,
        "timeline_end": float(blocks[-1]["end"]) if blocks else 0.0,
        "audio": str(final_audio.relative_to(output_root)) if final_audio.is_relative_to(output_root) else str(final_audio),
        "subtitles": str(ass_path.relative_to(output_root)) if ass_path.is_relative_to(output_root) else str(ass_path),
        "subtitle_preview": str(subtitle_preview_path.relative_to(output_root)) if subtitle_preview_path.is_relative_to(output_root) else str(subtitle_preview_path),
        "final": None,
        "blocks": items,
    }
    write_json(out_path, manifest)


def copy_file_if_exists(src_path: Path, dst_path: Path) -> None:
    if src_path.exists():
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)


def copy_planner_artifacts_to_part_debug(
    plans_dir: Path,
    base_name: str,
    part_debug_dir: Path,
) -> None:
    copy_file_if_exists(plans_dir / f"{base_name}_request.txt", part_debug_dir / "planner_request.txt")
    copy_file_if_exists(plans_dir / f"{base_name}_request.json", part_debug_dir / "planner_request.json")
    copy_file_if_exists(plans_dir / f"{base_name}_response.txt", part_debug_dir / "planner_response.txt")
    copy_file_if_exists(plans_dir / f"{base_name}_response.json", part_debug_dir / "planner_response.json")
    copy_file_if_exists(plans_dir / f"{base_name}_parsed.json", part_debug_dir / "planner_parsed.json")
    copy_file_if_exists(plans_dir / f"{base_name}.json", part_debug_dir / "planner_result.json")
    copy_file_if_exists(plans_dir / f"{base_name}_history.json", part_debug_dir / "planner_history.json")


def range_debug_dir(debug_dir: Path, block_index: int) -> Path:
    return debug_dir / "ranges" / f"range_{block_index:03d}"


def range_part_debug_dir(debug_dir: Path, block_index: int, sub_index: int) -> Path:
    return range_debug_dir(debug_dir, block_index) / f"part_{sub_index:03d}"


def write_range_debug_files(block: Dict[str, Any], subranges: List[Dict[str, Any]], debug_dir: Path) -> Path:
    block_i = int(block["block_index"])
    rdir = range_debug_dir(debug_dir, block_i)
    rdir.mkdir(parents=True, exist_ok=True)

    directives = block.get("bracket_directives") or []
    (rdir / "range_text.txt").write_text(str(block.get("text", "")), encoding="utf-8")
    (rdir / "range_directives.txt").write_text(
        "\n".join(str(x) for x in directives), encoding="utf-8"
    )

    range_context = {
        "block_index": block_i,
        "kind": str(block.get("kind", "")),
        "verse_index": block.get("verse_index"),
        "previous_verse_index": block.get("previous_verse_index"),
        "next_verse_index": block.get("next_verse_index"),
        "start": float(block.get("start", 0.0)),
        "end": float(block.get("end", 0.0)),
        "duration": float(block.get("duration", 0.0)),
        "text": str(block.get("text", "")),
        "bracket_directives": directives,
        "subranges": subranges,
    }
    write_json(rdir / "range_context.json", range_context)

    sub_count = len(subranges)
    for sub_i, subrange in enumerate(subranges):
        pdir = range_part_debug_dir(debug_dir, block_i, sub_i)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "subrange_text.txt").write_text(str(subrange.get("text", "")), encoding="utf-8")
        write_json(pdir / "subrange_context.json", {
            "subrange_index": sub_i,
            "subrange_count": sub_count,
            "subrange": subrange,
        })

    return rdir


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a ComfyUI music video from input-dir files.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {RUNNER_BUILD_ID}")
    ap.add_argument("--input-dir", default="input", help="Folder containing all song input files. Defaults to ./input.")
    ap.add_argument("--output-dir", default="output", help="Folder for all generated artifacts. Defaults to ./output.")
    ap.add_argument("--limit", type=int, default=0, help="Use only first N zero-based ranges for testing/final assembly. Example: --limit 3 selects R000..R002.")
    ap.add_argument("--rework", nargs="*", type=int, default=None, help="Generate only these zero-based range IDs; reuse existing clips for other selected ranges. Use the same RNNN numbers shown in subtitle_preview, without the R prefix.")
    ap.add_argument("--rebuild-final", action="store_true", help="Do not generate video; reuse existing unscaled clips and rebuild scaled clips/final only.")
    ap.add_argument("--refresh-alignment", action="store_true", help="Invalidate alignment/matching/subtitle/preview caches; missing artifacts are recreated lazily.")
    ap.add_argument("--preview-subtitles-only", action="store_true", help="Reuse or lazily build karaoke subtitles and subtitle_preview.mp4, then stop before any ComfyUI LLM/image/video generation.")
    ap.add_argument("--lyrics-language", default="en", help="Language code for stable-ts alignment. Default: en.")
    args = ap.parse_args()

    stats: Dict[str, float] = {"_run_start": time.perf_counter()}
    clips_generated = 0
    clips_reused = 0
    run_id = make_run_id()
    generation_info: Dict[int, Dict[str, Any]] = {}
    rework_indices = set(args.rework or [])

    script_dir = Path(__file__).resolve().parent
    input_dir = Path(args.input_dir).resolve()
    output_root = Path(args.output_dir).resolve()
    out_dir = output_root / "work"
    debug_dir = out_dir / "debug"
    alignment_dir = out_dir / "alignment"
    workflow_dir = script_dir / "workflows"
    rules_dir = script_dir / "rules"
    data_dir = script_dir / "data"
    ffmpeg_cmd = resolve_ffmpeg_command(script_dir)
    ffprobe_cmd = resolve_ffprobe_command(script_dir)
    stable_ts_cmd = resolve_stable_ts_command(script_dir)

    log(f"[runner] version  : {__version__}")
    log(f"[runner] script   : {Path(__file__).resolve()}")
    log(f"[stage] input dir : {input_dir}")
    log(f"[stage] output dir: {output_root}")
    log(f"[stage] workflows : {workflow_dir}")
    log(f"[stage] run id    : {run_id}")
    log(f"[stage] rules     : {rules_dir}")
    log(f"[stage] data      : {data_dir}")
    log(f"[stage] ffmpeg    : {ffmpeg_cmd}")
    log(f"[stage] ffprobe   : {ffprobe_cmd}")
    log(f"[stage] stable-ts : {stable_ts_cmd}")

    if args.refresh_alignment:
        log("[stage] refresh alignment: invalidate alignment/timeline/subtitle/scaled artifacts")
        invalidate_alignment_related_artifacts(output_root)

    log("[stage] read style/workflows/rules")
    rules = load_rules(rules_dir)
    video_style_path = input_dir / "video_style.txt"
    if not video_style_path.exists():
        raise FileNotFoundError(f"Required file not found: {video_style_path}")
    video_style = read_text(video_style_path)
    config = load_config(input_dir, data_dir)
    width = int(config["video_width"])
    height = int(config["video_height"])
    model_catalog = load_json(data_dir / "model_templates.json")
    llm_generator = create_generator("llm", model_catalog, config, workflow_dir)
    image_generator = create_generator("image", model_catalog, config, workflow_dir)
    video_generator = create_generator("video", model_catalog, config, workflow_dir)
    generation_settings = {"llm": llm_generator.metadata(), "image": image_generator.metadata(), "video": video_generator.metadata()}
    write_json(debug_dir / "generation_settings.json", generation_settings)
    log(f"[stage] LLM template: {llm_generator.name}")
    log(f"[stage] image template: {image_generator.name}")
    log(f"[stage] video template: {video_generator.name}")
    if video_generator.full_sampling:
        log("[warn] LTX uses unaccelerated sampling; this recipe requires visual validation")
    write_json(debug_dir / "config_used.json", config)
    block_video_styles, video_style_report = load_block_video_styles(input_dir, video_style, debug_dir)
    block_start_images = scan_block_start_images(input_dir)

    ensure_alignment_artifact(
        input_dir,
        out_dir,
        debug_dir,
        alignment_dir,
        stable_ts_cmd,
        args.lyrics_language,
    )

    log("[stage] parse alignment")
    stats_start(stats, "parse_alignment")
    verses, alignment_mode = parse_alignment(input_dir, alignment_dir, debug_dir, config)
    if not verses:
        raise RuntimeError("No verses parsed from alignment.")
    write_json(debug_dir / "parsed_verses_all.json", verses)
    write_alignment_match_report(verses, debug_dir / "alignment_match_report.txt")
    stats_end(stats, "parse_alignment")

    plans_dir = out_dir / "plans"

    total_verses = len(verses)

    log("[stage] prepare full audio")
    stats_start(stats, "prepare_audio")
    audio_mode, audio_a, audio_b = detect_audio(input_dir)
    log(f"[stage] audio mode={audio_mode}")
    full_mix = prepare_audio_full_mix(audio_mode, audio_a, audio_b, out_dir / "audio", ffmpeg_cmd)
    audio_duration = get_audio_duration(full_mix, ffprobe_cmd)
    log(f"[stage] full audio duration={audio_duration:.2f}s")
    stats_end(stats, "prepare_audio")

    stats_start(stats, "timeline")
    # Build the full range timeline first. Public range IDs are zero-based and
    # match preview labels: R000..RNNN. --limit is a count over this range list.
    preview_blocks, _preview_audio_end = make_timeline_blocks(verses, verses, audio_duration, False, config)
    if not preview_blocks:
        raise RuntimeError("No full-song timeline blocks created.")
    blocks = select_ranges_for_final(preview_blocks, args.limit)
    if not blocks:
        raise RuntimeError("No selected ranges.")
    has_effective_limit = len(blocks) < len(preview_blocks)
    audio_end = float(blocks[-1]["end"]) if has_effective_limit else None
    selected = [v for v in verses if audio_end is None or float(v.get("start", 0.0)) < float(audio_end)]
    stats_end(stats, "timeline")

    block_indices = {int(b["block_index"]) for b in blocks}
    if rework_indices:
        outside = sorted(rework_indices - block_indices)
        if outside:
            selected_desc = f"{format_range_id(min(block_indices))}..{format_range_id(max(block_indices))}" if block_indices else "none"
            raise RuntimeError(
                f"--rework contains range(s) outside selected range set: {format_range_id_list(outside)}. "
                f"With --limit {args.limit}, selected ranges are {selected_desc}. "
                f"Increase --limit or remove invalid indices."
            )

    log(f"[stage] verses total={total_verses} selected_for_subtitles={len(selected)} alignment={alignment_mode}")
    log(f"[stage] selected ranges={format_range_id(int(blocks[0]['block_index']))}..{format_range_id(int(blocks[-1]['block_index']))} count={len(blocks)}")
    log(f"[stage] subtitle preview timeline blocks={len(preview_blocks)} range=0.000s..{preview_blocks[-1]['end']:.3f}s (full song)")
    if has_effective_limit:
        log(f"[stage] limit mode: audio/video ends at selected range {format_range_id(int(blocks[-1]['block_index']))} end={float(blocks[-1]['end']):.3f}s")
    else:
        log("[stage] full-song mode: audio is not cut by range boundaries")

    if args.rebuild_final:
        log("[stage] rebuild-final: skip visual generation and rebuild final from existing unscaled clips")
    elif rework_indices:
        log(f"[stage] rework: generate only ranges {format_range_id_list(sorted(rework_indices))} and reuse the rest")
    else:
        log("[stage] generate selected ranges")

    write_json(debug_dir / "timeline_blocks.json", blocks)
    write_json(debug_dir / "preview_timeline_blocks.json", preview_blocks)

    log("[stage] render audio for timeline")
    stats_start(stats, "render_audio")
    final_audio = render_audio_for_timeline(full_mix, out_dir / "audio", ffmpeg_cmd, audio_end)
    render_duration = ffprobe_duration(final_audio, ffprobe_cmd)
    log(f"[stage] render audio duration={render_duration:.2f}s")
    stats_end(stats, "render_audio")

    subtitle_mode = "word" if alignment_mode == "json" else "line"
    ass_path = out_dir / "subs" / "karaoke.ass"
    preview_ass_path = out_dir / "subs" / "preview_karaoke.ass"
    preview_debug_ass = out_dir / "subs" / "preview_debug.ass"
    subtitle_preview = output_root / "subtitle_preview.mp4"

    missing_subtitle_artifacts = [
        path
        for path in (ass_path, preview_ass_path, preview_debug_ass)
        if not path.exists()
    ]
    if missing_subtitle_artifacts:
        log("[stage] build missing karaoke subtitle artifacts")
        stats_start(stats, "subtitles")
        style_section, subtitle_style_map, subtitle_style_report = build_subtitle_styles_for_blocks(
            preview_blocks,
            input_dir,
            data_dir,
            debug_dir,
        )
        # Release subtitles are full-song lazy artifacts, just like preview
        # subtitles. A limited final render naturally burns only events that
        # fall inside the limited video/audio duration.
        if not ass_path.exists():
            build_ass_subtitles(
                verses,
                preview_blocks,
                0.0,
                width,
                height,
                ass_path,
                subtitle_mode,
                style_section,
                subtitle_style_map,
                config,
                debug_dir / "timing_report.json",
            )
            log(f"[stage] subtitles: {ass_path} ({subtitle_mode}, full song)")
        else:
            log(f"[stage] subtitles: {ass_path} (cached)")

        if not preview_ass_path.exists():
            build_ass_subtitles(
                verses,
                preview_blocks,
                0.0,
                width,
                height,
                preview_ass_path,
                subtitle_mode,
                style_section,
                subtitle_style_map,
                config,
                debug_dir / "preview_timing_report.json",
            )
            log(f"[stage] preview subtitles: {preview_ass_path} ({subtitle_mode}, full song)")
        else:
            log(f"[stage] preview subtitles: {preview_ass_path} (cached)")

        if not preview_debug_ass.exists():
            build_preview_debug_ass(preview_blocks, preview_debug_ass, width, height, config, audio_duration)
            log(f"[stage] preview debug subtitles: {preview_debug_ass}")
        else:
            log(f"[stage] preview debug subtitles: {preview_debug_ass} (cached)")
        stats_end(stats, "subtitles")
    else:
        log(f"[stage] subtitles: {ass_path} (cached)")
        log(f"[stage] preview subtitles: {preview_ass_path} (cached)")
        log(f"[stage] preview debug subtitles: {preview_debug_ass} (cached)")

    if subtitle_preview.exists():
        log(f"[stage] subtitle preview: {subtitle_preview} (cached)")
    else:
        log("[stage] render subtitle preview")
        stats_start(stats, "subtitle_preview")
        render_subtitle_preview(
            full_mix,
            preview_ass_path,
            subtitle_preview,
            audio_duration,
            width,
            height,
            int(config["video_fps"]),
            ffmpeg_cmd,
            preview_debug_ass,
        )
        log(f"[stage] subtitle preview: {subtitle_preview}")
        stats_end(stats, "subtitle_preview")

    if args.preview_subtitles_only:
        write_preview_manifest(
            output_root / "manifest.json",
            output_root,
            preview_blocks,
            full_mix,
            preview_ass_path,
            subtitle_preview,
            audio_mode,
            alignment_mode,
            args.limit,
            sorted(rework_indices) if rework_indices else [],
            run_id,
        )
        write_json(debug_dir / "run_info.json", {
            "run_id": run_id,
            "runner_version": __version__,
            "script_path": str(Path(__file__).resolve()),
            "preview_subtitles_only": True,
        })
        print_run_stats(
            stats,
            total_verses,
            len(selected),
            len(preview_blocks),
            0,
            0,
            subtitle_preview,
        )
        log(f"\n[done] SUBTITLE PREVIEW: {subtitle_preview}")
        log(f"[done] MANIFEST: {output_root / 'manifest.json'}")
        return

    clips: List[Path] = []
    clips_dir = out_dir / "clips"
    clips_unscaled_dir = out_dir / "clips_unscaled"
    subclips_raw_root = out_dir / "subclips_raw"
    subclips_video_root = out_dir / "subclips_video"
    frames_root = out_dir / "frames"
    debug_dir.mkdir(parents=True, exist_ok=True)
    write_json(debug_dir / "run_info.json", {
        "run_id": run_id,
        "runner_version": __version__,
        "script_path": str(Path(__file__).resolve()),
        "refresh_alignment": bool(args.refresh_alignment),
    })
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips_unscaled_dir.mkdir(parents=True, exist_ok=True)
    subclips_raw_root.mkdir(parents=True, exist_ok=True)
    subclips_video_root.mkdir(parents=True, exist_ok=True)
    frames_root.mkdir(parents=True, exist_ok=True)

    ranges_to_generate = select_ranges_to_generate(blocks, args.rework)
    if args.rebuild_final:
        ranges_to_generate = []
    blocks_to_generate = {int(block["block_index"]) for block in ranges_to_generate}

    song_context: Optional[Dict[str, Any]] = None
    if blocks_to_generate:
        stats_start(stats, "song_context")
        song_context = get_or_create_song_context(
            llm_generator,
            rules,
            video_style,
            verses,
            plans_dir,
        )
        write_json(debug_dir / "song_context_used.json", {
            "context": song_context,
        })
        stats_end(stats, "song_context")
    else:
        log("[stage] no visual generation required; skip song context")

    for block in blocks:
        block_i = int(block["block_index"])
        kind = str(block["kind"])
        duration = max(0.1, float(block["duration"]))
        first_line = str(block.get("text", "")).splitlines()[0] if str(block.get("text", "")).splitlines() else ""
        first = first_line[:100]
        clip_local = block_clip_path(clips_dir, block)
        unscaled_clip = block_clip_path(clips_unscaled_dir, block)
        should_generate = block_i in blocks_to_generate

        log(f"\n=== {format_range_id(block_i)}: {first}")
        log(f"  [stage] time={float(block['start']):.3f}s..{float(block['end']):.3f}s duration={duration:.2f}s")

        subranges = build_subranges_for_block(block, config)
        range_dir = write_range_debug_files(block, subranges, debug_dir)

        if not should_generate:
            if not unscaled_clip.exists():
                raise FileNotFoundError(
                    f"Existing unscaled clip required but not found: {unscaled_clip}\n"
                    "Run full generation first, or include this block in --rework."
                )
            log(f"  [stage] reuse unscaled clip: {unscaled_clip}")
            generation_record = unscaled_clip.with_suffix(".generation.json")
            if not generation_record.exists():
                raise FileNotFoundError(f"Generation metadata required for reuse: {generation_record}. Regenerate this range with --rework.")
            reused_settings = load_json(generation_record)
            if any(reused_settings.get(k, {}).get("signature") != generation_settings[k]["signature"] for k in ("llm", "image", "video")):
                log("  [warn] reused clip has different generator settings; include this range in --rework to apply the new template")
            generation_info[block_i] = {
                "run_id": run_id,
                "segment_subdir": None,
                "seeds": None,
                "generated_in_this_run": False,
                "generation_settings": reused_settings,
            }
            clips_reused += 1
            continue

        stats_start(stats, "video_generation")

        block_subclips_raw_dir = subclips_raw_root / f"block_{block_i:03d}"
        block_subclips_video_dir = subclips_video_root / f"block_{block_i:03d}"
        block_frames_dir = frames_root / f"block_{block_i:03d}"
        for path in (block_subclips_raw_dir, block_subclips_video_dir, block_frames_dir):
            if path.exists():
                shutil.rmtree(path)
            path.mkdir(parents=True, exist_ok=True)

        if not block_has_lyric_text(block):
            local_context = build_instrumental_local_context(
                verses,
                int(block.get("previous_verse_index", 0)),
                block.get("next_verse_index"),
            )
        else:
            local_context = build_local_context(
                verses,
                int(block.get("verse_index", block_i + 1)),
                int(config["local_context_radius"]),
            )

        block_video_style = effective_video_style(block_i, video_style, block_video_styles)
        base_continuity = load_continuity_from_plans(plans_dir, block_i)
        part_continuity = list(base_continuity)
        subclip_paths: List[Path] = []
        subrange_infos: List[Dict[str, Any]] = []
        previous_subclip: Optional[Path] = None

        sub_count = len(subranges)
        for sub_i, subrange in enumerate(subranges):
            # The subrange list position is the canonical zero-based identity.
            # There is deliberately no duplicated sub_index/sub_count state in
            # the subrange object itself.
            sub_duration = max(0.1, float(subrange["duration"]))
            sub_dir = generation_part_subdir(run_id, block_i, sub_i)
            plan_suffix = f"_part_{sub_i:03d}"
            plan_base_name = f"plan_{block_i:03d}{plan_suffix}"

            log(f"  [subrange] S{sub_i:03d}/{sub_count:03d} time={float(subrange['start']):.3f}s..{float(subrange['end']):.3f}s duration={sub_duration:.2f}s")

            current_instruction = build_subrange_instruction(block, subrange, sub_i, sub_count)
            part_debug_dir = range_part_debug_dir(debug_dir, block_i, sub_i)
            part_debug_dir.mkdir(parents=True, exist_ok=True)
            write_json(part_debug_dir / "planner_context.json", {
                "block_index": block_i,
                "block_kind": kind,
                "subrange_index": sub_i,
                "subrange_count": sub_count,
                "subrange": subrange,
                "video_style_source": video_style_report.get("blocks", {}).get(str(block_i), video_style_report.get("default", {})),
                "video_style": block_video_style,
                "song_context": song_context,
                "local_context": local_context,
                "current_block": current_instruction,
                "continuity": part_continuity[-5:],
            })

            plan = run_visual_planner(
                llm_generator,
                rules,
                block_video_style,
                song_context,
                local_context,
                current_instruction,
                block_i,
                kind,
                plans_dir,
                part_continuity,
                plan_suffix=plan_suffix,
            )
            copy_planner_artifacts_to_part_debug(plans_dir, plan_base_name, part_debug_dir)

            seeds = {
                "image_seed": random_seed(),
                "video_seed": random_seed(),
                "video_refine_seed": random_seed(),
            }

            start_image_local = block_frames_dir / f"part_{sub_i:03d}_start.png"
            last_frame_local = block_frames_dir / f"part_{sub_i:03d}_last.png"

            if sub_i == 0 and block_i in block_start_images:
                image_path = block_start_images[block_i]
                start_image_local = start_image_local.with_suffix(image_path.suffix.lower())
                log(f"  [stage] copy supplied start image: {image_path}")
                shutil.copy2(image_path, start_image_local)
            elif sub_i == 0:
                log("  [stage] queue start image")
                image_result = image_generator.generate(ImageRequest(
                    prompt=plan["image_prompt"], seed=seeds["image_seed"],
                    output_prefix=f"{sub_dir}/start_image", sub_dir=sub_dir, debug_dir=part_debug_dir,
                ))
                image_path = image_result.path
                write_json(part_debug_dir / "image_generator.json", image_result.metadata)
                shutil.copy2(image_path, start_image_local)
            else:
                if previous_subclip is None:
                    raise RuntimeError(f"Internal error: no previous subclip for {format_range_id(block_i)} part {sub_i:03d}")
                log("  [stage] extract previous last frame as next start image")
                extract_last_frame(previous_subclip, start_image_local, ffmpeg_cmd)

            log("  [stage] patch video-from-image workflow")
            log(f"  [seeds] image={seeds['image_seed']} video={seeds['video_seed']} refine={seeds['video_refine_seed']}")
            if sub_duration > float(config["max_workflow_seconds"]):
                raise RuntimeError(f"{format_range_id(block_i)} part {sub_i:03d} exceeds max_workflow_seconds")
            if sub_duration > float(config["recommended_workflow_seconds"]):
                log(f"  [warn] subrange exceeds recommended_workflow_seconds: {sub_duration:.2f}s")
            video_result = video_generator.generate(VideoRequest(
                start_image=start_image_local, prompt=plan["video_prompt"],
                negative_prompt=plan.get("negative_prompt", ""), seconds=sub_duration,
                seed=seeds["video_seed"], refine_seed=seeds["video_refine_seed"],
                output_prefix=f"{sub_dir}/video", sub_dir=sub_dir, debug_dir=part_debug_dir,
            ))
            video_path = video_result.path
            write_json(part_debug_dir / "video_generator.json", video_result.metadata)

            raw_part = block_subclips_raw_dir / f"part_{sub_i:03d}{video_path.suffix}"
            shutil.copy2(video_path, raw_part)

            subclip_local = block_subclips_video_dir / f"part_{sub_i:03d}.mp4"
            log("  [stage] copy subclip video stream only; keep generated duration")
            copy_video_only(raw_part, subclip_local, ffmpeg_cmd)
            extract_last_frame(subclip_local, last_frame_local, ffmpeg_cmd)

            previous_subclip = subclip_local
            subclip_paths.append(subclip_local)
            scene_summary = str(plan.get("scene_summary", ""))
            part_continuity.append({
                "segment": f"{block_i}.{sub_i}",
                "scene_summary": scene_summary,
            })

            subrange_info = {
                "generation_settings": generation_settings,
                "subrange_index": sub_i,
                "subrange_count": sub_count,
                "start": float(subrange["start"]),
                "end": float(subrange["end"]),
                "duration": sub_duration,
                "text": str(subrange.get("text", "")),
                "text_mode": str(subrange.get("text_mode", "")),
                "scene_summary": scene_summary,
                "generation_subdir": sub_dir,
                "start_image": str(start_image_local.relative_to(output_root)) if start_image_local.is_relative_to(output_root) else str(start_image_local),
                "last_frame": str(last_frame_local.relative_to(output_root)) if last_frame_local.is_relative_to(output_root) else str(last_frame_local),
                "subclip": str(subclip_local.relative_to(output_root)) if subclip_local.is_relative_to(output_root) else str(subclip_local),
                "seeds": seeds,
                "plan": str((plans_dir / f"plan_{block_i:03d}{plan_suffix}.json").relative_to(output_root)),
            }
            subrange_infos.append(subrange_info)
            write_json(part_debug_dir / "video_generation.json", subrange_info)

        log("  [stage] assemble unscaled semantic clip from generated subclips")
        concat_or_copy_subclips(subclip_paths, unscaled_clip, ffmpeg_cmd)
        write_json(unscaled_clip.with_suffix(".generation.json"), generation_settings)
        scene_summary = " / ".join(x.get("scene_summary", "") for x in subrange_infos if x.get("scene_summary"))
        if not scene_summary:
            scene_summary = f"{format_range_id(block_i)}, rendered as {len(subrange_infos)} internal subrange(s)"
        aggregate_plan = {
            "scene_summary": scene_summary,
            "split_parts": len(subrange_infos),
            "subranges": [
                {
                    "subrange_index": x.get("subrange_index"),
                    "subrange_count": x.get("subrange_count"),
                    "start": x.get("start"),
                    "end": x.get("end"),
                    "duration": x.get("duration"),
                    "text_mode": x.get("text_mode"),
                    "scene_summary": x.get("scene_summary"),
                }
                for x in subrange_infos
            ],
        }
        write_json(plans_dir / f"plan_{block_i:03d}.json", aggregate_plan)

        generation_info[block_i] = {
            "run_id": run_id,
            "segment_subdir": f"aligned_song/{run_id}/block_{block_i:03d}",
            "generated_in_this_run": True,
            "generation_settings": generation_settings,
            "range_debug": relpath_or_abs(range_dir, output_root),
            "unscaled_clip": relpath_or_abs(unscaled_clip, output_root),
            "subranges": subrange_infos,
        }
        write_json(debug_dir / f"video_generation_{block_i:03d}.json", generation_info[block_i])

        clips_generated += 1
        stats_end(stats, "video_generation")

    log("\n[stage] validate and scale unscaled clips to current timeline")
    stats_start(stats, "clip_scaling")
    scaling_report: List[Dict[str, Any]] = []
    validation_report: List[Dict[str, Any]] = []
    for block in blocks:
        block_i = int(block["block_index"])
        unscaled_clip = block_clip_path(clips_unscaled_dir, block)
        clip_local = block_clip_path(clips_dir, block)
        validation_info = validate_unscaled_clip_for_timeline(
            block,
            unscaled_clip,
            output_root,
            config,
            ffprobe_cmd,
        )
        validation_report.append(validation_info)
        log(f"  [scale] {format_range_id(block_i)}: {unscaled_clip.name} -> {clip_local.name}")
        scale_info = retime_video_copy(
            unscaled_clip,
            max(0.1, float(block["duration"])),
            clip_local,
            ffmpeg_cmd,
            ffprobe_cmd,
            int(config["video_fps"]),
        )
        scale_info["block_index"] = block_i
        scale_info["kind"] = str(block.get("kind", ""))
        scaling_report.append(scale_info)
        clips.append(clip_local)
    write_json(debug_dir / "clip_validation_report.json", validation_report)
    write_json(debug_dir / "clip_scaling_report.json", scaling_report)
    stats_end(stats, "clip_scaling")

    log("\n[stage] concat scaled video clips")
    stats_start(stats, "concat")
    video_only = out_dir / "video" / "video_only.mp4"
    concat_videos(clips, video_only, ffmpeg_cmd)
    stats_end(stats, "concat")

    log("[stage] final mux audio + burn subtitles")
    stats_start(stats, "final_mux")
    final = output_root / "final_video.mp4"
    final_mux(video_only, final_audio, ass_path, final, ffmpeg_cmd, int(config["video_fps"]))
    stats_end(stats, "final_mux")

    write_timeline_manifest(
        output_root / "manifest.json",
        output_root,
        blocks,
        clips,
        final_audio,
        ass_path,
        final,
        subtitle_preview,
        audio_mode,
        alignment_mode,
        args.limit,
        sorted(rework_indices) if rework_indices else [],
        run_id,
        generation_info,
    )

    print_run_stats(
        stats,
        total_verses,
        len(selected),
        len(blocks),
        clips_generated,
        clips_reused,
        final,
    )

    log(f"\n[done] FINAL: {final}")
    log(f"[done] MANIFEST: {output_root / 'manifest.json'}")


if __name__ == "__main__":
    main()
