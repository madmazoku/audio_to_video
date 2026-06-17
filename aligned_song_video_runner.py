from __future__ import annotations

import argparse
import difflib
import json
import math
import os
import re
import shutil
import subprocess
import time
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import random
import statistics
import websocket

IMAGE_N = {
    "image_prompt": "1004",
    "image_latent": "1007",
    "image_scheduler": "1024",
    "image_noise": "1022",
    "image_save": "1011",
}

VIDEO_N = {
    "start_image": "9000",
    "video_prompt": "393",
    "video_negative": "328",
    "video_seconds": "322",
    "video_fps": "304",
    "video_width": "261",
    "video_height": "299",
    "video_noise": "259",
    "video_refine_noise": "283",
    "video_save": "327",
}


def log(msg: str) -> None:
    print(msg, flush=True)



def clean_fresh_output_dir(output_root: Path) -> None:
    """Remove previous generated artifacts for a normal fresh run."""
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)


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


def comfy_segment_subdir(run_id: str, index: int) -> str:
    return f"aligned_song/{run_id}/segment_{index:03d}"


def queue_prompt(workflow: Dict[str, Any], comfy_url: str, client_id: Optional[str] = None) -> Tuple[str, str]:
    if client_id is None:
        client_id = str(uuid.uuid4())

    r = requests.post(
        comfy_url.rstrip("/") + "/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=60,
    )
    try:
        r.raise_for_status()
    except Exception:
        log("ComfyUI /prompt error:")
        log(r.text[:4000])
        raise

    data = r.json()
    if "prompt_id" not in data:
        raise RuntimeError(f"Unexpected /prompt response: {data}")

    return str(data["prompt_id"]), client_id



def comfy_ws_url(comfy_url: str, client_id: str) -> str:
    base = comfy_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif base.startswith("ws://") or base.startswith("wss://"):
        pass
    else:
        base = "ws://" + base
    return f"{base}/ws?clientId={client_id}"


def workflow_node_label(workflow: Dict[str, Any], node_id: Optional[str]) -> str:
    if node_id is None:
        return "unknown"

    node = workflow.get(str(node_id), {})
    if not isinstance(node, dict):
        return str(node_id)

    meta = node.get("_meta", {})
    title = ""
    if isinstance(meta, dict):
        title = str(meta.get("title", "")).strip()

    class_type = str(node.get("class_type", "")).strip()

    if title and class_type:
        return f"{node_id} {title} [{class_type}]"
    if title:
        return f"{node_id} {title}"
    if class_type:
        return f"{node_id} {class_type}"
    return str(node_id)


def format_progress(value: Any, maximum: Any) -> str:
    try:
        v = float(value)
        m = float(maximum)
        if m > 0:
            pct = v * 100.0 / m
            if float(value).is_integer() and float(maximum).is_integer():
                return f"{int(v)}/{int(m)} ({pct:.1f}%)"
            return f"{v:.2f}/{m:.2f} ({pct:.1f}%)"
    except Exception:
        pass

    if value is not None and maximum is not None:
        return f"{value}/{maximum}"
    if value is not None:
        return str(value)
    return ""


def wait_history_ws(
    prompt_id: str,
    client_id: str,
    workflow: Dict[str, Any],
    comfy_url: str,
    report_seconds: float = 5.0,
) -> Dict[str, Any]:
    ws_url = comfy_ws_url(comfy_url, client_id)
    history_url = comfy_url.rstrip("/") + f"/history/{prompt_id}"

    start = time.perf_counter()
    last_report = 0.0
    current_node: Optional[str] = None
    current_progress = ""
    last_node_label = ""
    finished_by_ws = False

    ws = websocket.WebSocket()
    ws.connect(ws_url, timeout=60)

    try:
        while True:
            elapsed = time.perf_counter() - start

            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                raw = None

            if raw:
                if isinstance(raw, bytes):
                    # Binary preview data can be sent by ComfyUI; ignore it for progress logging.
                    pass
                else:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        msg = {}

                    msg_type = msg.get("type")
                    data = msg.get("data", {})
                    if not isinstance(data, dict):
                        data = {}

                    msg_prompt_id = data.get("prompt_id")
                    if msg_prompt_id and msg_prompt_id != prompt_id:
                        continue

                    if msg_type == "execution_start":
                        log(f"  [comfy] execution started prompt_id={prompt_id}")

                    elif msg_type == "executing":
                        node = data.get("node")
                        if node is None:
                            # ComfyUI commonly sends node=None when the prompt is done.
                            finished_by_ws = True
                            break

                        current_node = str(node)
                        current_progress = ""
                        node_label = workflow_node_label(workflow, current_node)
                        if node_label != last_node_label:
                            last_node_label = node_label
                            log(f"  [comfy] node: {node_label}")

                    elif msg_type == "progress":
                        current_progress = format_progress(data.get("value"), data.get("max"))

                    elif msg_type == "executed":
                        node = data.get("node")
                        if node is not None:
                            log(f"  [comfy] executed: {workflow_node_label(workflow, str(node))}")

                    elif msg_type == "execution_error":
                        node = data.get("node_id") or data.get("node")
                        message = data.get("exception_message") or data.get("message") or ""
                        log(f"  [comfy] execution error at {workflow_node_label(workflow, str(node) if node is not None else None)}: {message}")
                        finished_by_ws = True
                        break

                    elif msg_type in {"execution_success", "execution_cached"}:
                        # Wait for history below; this event only tells us execution state.
                        pass

            elapsed = time.perf_counter() - start
            if elapsed - last_report >= report_seconds:
                last_report = elapsed
                node_label = workflow_node_label(workflow, current_node)
                if current_progress:
                    log(f"  [comfy] running... {elapsed:.0f}s | {node_label} | progress {current_progress}")
                else:
                    log(f"  [comfy] running... {elapsed:.0f}s | {node_label}")

            if finished_by_ws:
                break

        # After websocket completion/error signal, fetch authoritative history.
        deadline = time.perf_counter() + 60.0
        while True:
            r = requests.get(history_url, timeout=60)
            r.raise_for_status()
            h = r.json()
            if prompt_id in h:
                elapsed = time.perf_counter() - start
                log(f"  [comfy] finished after {elapsed:.0f}s")
                return h[prompt_id]

            if time.perf_counter() > deadline:
                raise RuntimeError(f"ComfyUI history did not appear for prompt_id={prompt_id}")

            time.sleep(0.5)

    finally:
        try:
            ws.close()
        except Exception:
            pass

def wait_history(
    prompt_id: str,
    comfy_url: str,
    workflow: Dict[str, Any],
    client_id: str,
) -> Dict[str, Any]:
    return wait_history_ws(prompt_id, client_id, workflow, comfy_url)


def check_history_status(history_item: Dict[str, Any], debug_path: Path) -> None:
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    debug_path.write_text(json.dumps(history_item, ensure_ascii=False, indent=2), encoding="utf-8")
    status = history_item.get("status", {})
    status_str = status.get("status_str")
    if status_str and status_str != "success":
        raise RuntimeError(
            "ComfyUI prompt did not finish successfully.\n"
            f"status={status_str}\n"
            f"Debug history saved to: {debug_path}\n"
            f"messages={json.dumps(status.get('messages', []), ensure_ascii=False)[:3000]}"
        )


def history_files(history_item: Dict[str, Any]) -> List[str]:
    files: List[str] = []
    for _, out in history_item.get("outputs", {}).items():
        for key in ("videos", "gifs", "images", "audio", "files"):
            items = out.get(key)
            if not items:
                continue
            if isinstance(items, dict):
                items = [items]
            for item in items:
                if isinstance(item, dict) and item.get("filename"):
                    sub = item.get("subfolder") or ""
                    rel = str(Path(sub) / item["filename"]) if sub else item["filename"]
                    files.append(rel)
    return files



def find_result_file(
    history_item: Dict[str, Any],
    output_dir: Path,
    expected_subdir: str,
    prefix: str,
    suffixes: set[str],
) -> Optional[Path]:
    expected_subdir_norm = expected_subdir.replace("\\", "/").strip("/")

    for rel in history_files(history_item):
        rel_norm = rel.replace("\\", "/").strip("/")
        p = output_dir / rel
        if not p.exists() or p.suffix.lower() not in suffixes:
            continue
        if not p.name.lower().startswith(prefix):
            continue
        if expected_subdir_norm and not rel_norm.startswith(expected_subdir_norm + "/") and rel_norm != expected_subdir_norm:
            continue
        return p

    expected_dir = output_dir / expected_subdir
    found: List[Path] = []
    for suffix in suffixes:
        found.extend(expected_dir.glob(f"{prefix}*{suffix}"))
        found.extend(expected_dir.glob(f"*{suffix}"))
    found = [p for p in found if p.is_file() and p.suffix.lower() in suffixes]
    if found:
        return max(found, key=lambda p: p.stat().st_mtime)
    return None


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


def infer_orientation_size(video_style: str) -> tuple[int, int]:
    text = video_style.lower()
    m = re.search(r"(\d{3,4})\s*[x×]\s*(\d{3,4})", video_style)
    if m:
        return int(m.group(1)), int(m.group(2))
    if "portrait" in text or "vertical" in text:
        return 720, 1280
    return 1280, 720


def extract_fps(video_style: str) -> int:
    m = re.search(r"(\d{2,3})\s*fps\b", video_style, re.I)
    return int(m.group(1)) if m else 24


APOSTROPHE_CHARS = "'’‘ʼ`´"
WORD_JOIN_CHARS = "-" + APOSTROPHE_CHARS


def norm_word(s: str) -> str:
    s = s.casefold().replace("\u0451", "\u0435").replace("\u0401", "\u0435")
    for ch in APOSTROPHE_CHARS:
        s = s.replace(ch, "'")
    s = re.sub(r"[^\w]+", "", s, flags=re.U)
    return s


def lyric_words(text: str) -> List[str]:
    # Keep contractions as a single display token. Stable-ts often returns
    # words such as should’ve / shouldn’t as one word; splitting lyrics on
    # curly apostrophes made subtitles render them as "should ve".
    joiners = re.escape(WORD_JOIN_CHARS)
    pattern = rf"\w+(?:[{joiners}]\w+)*"
    return [w for w in re.findall(pattern, text, flags=re.U) if norm_word(w)]




def is_alignment_meta_token(text: str) -> bool:
    stripped = str(text).strip()
    return (
        stripped == "***"
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
        if line == "***":
            out.append("")
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
    max_len = max(len(norm_word(a)), len(norm_word(b)))
    if max_len <= 2:
        return 1.0
    if max_len <= 4:
        return max(0.84, base_threshold)
    return base_threshold


def words_are_match(a: str, b: str, base_threshold: float) -> Tuple[bool, float, str]:
    sim = word_similarity(a, b)
    threshold = word_match_threshold(a, b, base_threshold)
    if sim >= 1.0:
        return True, sim, "match"
    if sim >= threshold:
        return True, sim, "fuzzy_match"
    return False, sim, "mismatch"


def expected_words_for_lyrics_verses(lyrics_verses: List[Dict[str, Any]]) -> List[List[str]]:
    return [lyric_words(str(v.get("text", ""))) for v in lyrics_verses]


def find_next_range_prefix(
    words: List[Dict[str, Any]],
    cursor: int,
    next_expected: List[str],
    lookahead: int,
    threshold: float,
) -> Optional[int]:
    if not next_expected:
        return None

    prefix = next_expected[:min(3, len(next_expected))]
    if not prefix:
        return None

    max_start = min(len(words), cursor + max(1, lookahead))
    for start in range(cursor, max_start):
        score = 0
        checked = 0
        for j, ew in enumerate(prefix):
            if start + j >= len(words):
                break
            aw = words[start + j]["text"]
            ok, _, _ = words_are_match(ew, aw, threshold)
            checked += 1
            if ok:
                score += 1
        if checked >= min(2, len(prefix)) and score >= min(2, len(prefix)):
            return start
        if len(prefix) == 1 and checked == 1 and score == 1:
            return start

    return None


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


def match_expected_range_words(
    expected_words: List[str],
    words: List[Dict[str, Any]],
    cursor: int,
    next_expected_words: List[str],
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], int, Dict[str, Any]]:
    lookahead = max(1, int(config.get("alignment_match_lookahead_words", 5)))
    threshold = float(config.get("alignment_match_similarity_threshold", 0.72))
    out: List[Dict[str, Any]] = []
    events: List[Dict[str, Any]] = []
    extras: List[Dict[str, Any]] = []
    stats = {
        "expected": len(expected_words),
        "matched": 0,
        "fuzzy": 0,
        "mismatch": 0,
        "missing": 0,
        "extra": 0,
        "start_cursor": cursor,
        "end_cursor": cursor,
        "events": events,
        "extra_actual": extras,
        "boundary_reason": "expected_exhausted",
    }

    i = 0
    previous_end: Optional[float] = None

    while i < len(expected_words):
        ew = expected_words[i]

        if cursor >= len(words):
            item = synthesize_word_timing(ew, previous_end, None)
            out.append(item)
            stats["missing"] += 1
            events.append({"status": "missing_expected", "expected": ew, "reason": "no_aligned_words_left"})
            previous_end = item["end"]
            i += 1
            continue

        # If the next range prefix is already here and we still have expected
        # words in current range, close current range with synthetic missing
        # words rather than stealing the next range.
        next_prefix_pos = find_next_range_prefix(words, cursor, next_expected_words, lookahead, threshold)
        if next_prefix_pos == cursor and i > 0:
            stats["boundary_reason"] = "next_range_prefix_detected"
            while i < len(expected_words):
                item = synthesize_word_timing(expected_words[i], previous_end, words[cursor]["start"])
                out.append(item)
                stats["missing"] += 1
                events.append({"status": "missing_expected", "expected": expected_words[i], "reason": "next_range_prefix_detected"})
                previous_end = item["end"]
                i += 1
            break

        aw = words[cursor]
        ok, sim, status = words_are_match(ew, str(aw["text"]), threshold)
        if ok:
            item = {
                "text": ew,
                "aligned_text": aw["text"],
                "start": aw["start"],
                "end": aw["end"],
                "probability": aw.get("probability"),
                "match_status": status,
                "synthetic_timing": False,
                "similarity": sim,
            }
            out.append(item)
            stats["matched"] += 1
            if status == "fuzzy_match":
                stats["fuzzy"] += 1
            events.append({
                "status": status,
                "expected": ew,
                "actual": aw["text"],
                "start": aw["start"],
                "end": aw["end"],
                "similarity": sim,
            })
            previous_end = item["end"]
            cursor += 1
            i += 1
            continue

        # Look ahead actual words for current expected word. Skipped actuals
        # become extra and are not used in subtitles.
        found_actual: Optional[int] = None
        found_sim = 0.0
        found_status = "mismatch"
        max_actual = min(len(words), cursor + lookahead + 1)
        for j in range(cursor + 1, max_actual):
            ok2, sim2, status2 = words_are_match(ew, str(words[j]["text"]), threshold)
            if ok2:
                found_actual = j
                found_sim = sim2
                found_status = status2
                break

        # Look ahead expected words for current actual word. Missing expected
        # words get synthetic timing.
        found_expected: Optional[int] = None
        found_expected_sim = 0.0
        found_expected_status = "mismatch"
        max_expected = min(len(expected_words), i + lookahead + 1)
        for k in range(i + 1, max_expected):
            ok3, sim3, status3 = words_are_match(expected_words[k], str(aw["text"]), threshold)
            if ok3:
                found_expected = k
                found_expected_sim = sim3
                found_expected_status = status3
                break

        if found_actual is not None and (found_expected is None or (found_actual - cursor) <= (found_expected - i)):
            for j in range(cursor, found_actual):
                extra = words[j]
                extra_event = {
                    "status": "extra_actual",
                    "actual": extra["text"],
                    "start": extra["start"],
                    "end": extra["end"],
                }
                extras.append(extra_event)
                events.append(extra_event)
                stats["extra"] += 1
            aw2 = words[found_actual]
            item = {
                "text": ew,
                "aligned_text": aw2["text"],
                "start": aw2["start"],
                "end": aw2["end"],
                "probability": aw2.get("probability"),
                "match_status": found_status,
                "synthetic_timing": False,
                "similarity": found_sim,
            }
            out.append(item)
            stats["matched"] += 1
            if found_status == "fuzzy_match":
                stats["fuzzy"] += 1
            events.append({
                "status": found_status,
                "expected": ew,
                "actual": aw2["text"],
                "start": aw2["start"],
                "end": aw2["end"],
                "similarity": found_sim,
                "after_extra_actual": found_actual - cursor,
            })
            previous_end = item["end"]
            cursor = found_actual + 1
            i += 1
            continue

        if found_expected is not None:
            for k in range(i, found_expected):
                item = synthesize_word_timing(expected_words[k], previous_end, aw["start"])
                out.append(item)
                stats["missing"] += 1
                events.append({
                    "status": "missing_expected",
                    "expected": expected_words[k],
                    "reason": "later_expected_matches_current_actual",
                })
                previous_end = item["end"]
            ew2 = expected_words[found_expected]
            item = {
                "text": ew2,
                "aligned_text": aw["text"],
                "start": aw["start"],
                "end": aw["end"],
                "probability": aw.get("probability"),
                "match_status": found_expected_status,
                "synthetic_timing": False,
                "similarity": found_expected_sim,
            }
            out.append(item)
            stats["matched"] += 1
            if found_expected_status == "fuzzy_match":
                stats["fuzzy"] += 1
            events.append({
                "status": found_expected_status,
                "expected": ew2,
                "actual": aw["text"],
                "start": aw["start"],
                "end": aw["end"],
                "similarity": found_expected_sim,
                "after_missing_expected": found_expected - i,
            })
            previous_end = item["end"]
            cursor += 1
            i = found_expected + 1
            continue

        # Last-resort pair as mismatch to keep time moving.
        item = {
            "text": ew,
            "aligned_text": aw["text"],
            "start": aw["start"],
            "end": aw["end"],
            "probability": aw.get("probability"),
            "match_status": "mismatch",
            "synthetic_timing": False,
            "similarity": sim,
        }
        out.append(item)
        stats["mismatch"] += 1
        events.append({
            "status": "mismatch",
            "expected": ew,
            "actual": aw["text"],
            "start": aw["start"],
            "end": aw["end"],
            "similarity": sim,
        })
        previous_end = item["end"]
        cursor += 1
        i += 1

    stats["end_cursor"] = cursor
    return out, cursor, stats


def split_matched_words_into_lines(
    ly: Dict[str, Any],
    matched_words: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    cursor = 0
    out_lines: List[Dict[str, Any]] = []

    for li, line_text in enumerate(ly["lines_text"], 1):
        expected_line_words = lyric_words(line_text)
        count = len(expected_line_words)
        line_words = matched_words[cursor:cursor + count]
        cursor += count

        if line_words:
            start = float(line_words[0]["start"])
            end = float(line_words[-1]["end"])
        elif out_lines:
            start = float(out_lines[-1]["end"])
            end = start + 0.25
        else:
            start = end = 0.0

        out_lines.append({
            "index": li,
            "text": line_text,
            "start": start,
            "end": max(start + 0.01, end),
            "words": line_words,
        })

    return out_lines


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
        config.update(override)
        source = str(override_path)

    required = {
        "comfy_url": str,
        "comfy_output_dir": str,
        "width": int,
        "height": int,
        "fps": int,
        "recommended_workflow_seconds": (int, float),
        "max_workflow_seconds": (int, float),
        "instrumental_gap_min_seconds": (int, float),
        "instrumental_gap_min_ratio_of_median_verse": (int, float),
        "local_context_radius": int,
        "range_visual_preroll_seconds": (int, float),
        "subtitle_line_preroll_seconds": (int, float),
        "min_karaoke_unit_seconds": (int, float),
        "alignment_match_lookahead_words": int,
        "alignment_match_similarity_threshold": (int, float),
        "alignment_match_warn_ratio": (int, float),
        "alignment_match_max_extra_ratio": (int, float),
        "clip_reuse_duration_tolerance_ratio": (int, float),
    }

    for key, expected_type in required.items():
        if key not in config:
            raise RuntimeError(f"Missing config key: {key}")
        if not isinstance(config[key], expected_type):
            raise RuntimeError(f"Bad config key {key}: expected {expected_type}, got {type(config[key]).__name__}")

    config["comfy_url"] = str(config["comfy_url"])
    config["comfy_output_dir"] = str(config["comfy_output_dir"])
    config["width"] = int(config["width"])
    config["height"] = int(config["height"])
    config["fps"] = int(config["fps"])
    config["recommended_workflow_seconds"] = float(config["recommended_workflow_seconds"])
    config["max_workflow_seconds"] = float(config["max_workflow_seconds"])
    config["instrumental_gap_min_seconds"] = float(config["instrumental_gap_min_seconds"])
    config["instrumental_gap_min_ratio_of_median_verse"] = float(config["instrumental_gap_min_ratio_of_median_verse"])
    config["local_context_radius"] = int(config["local_context_radius"])
    config["range_visual_preroll_seconds"] = float(config["range_visual_preroll_seconds"])
    config["subtitle_line_preroll_seconds"] = float(config["subtitle_line_preroll_seconds"])
    config["min_karaoke_unit_seconds"] = float(config["min_karaoke_unit_seconds"])
    config["alignment_match_lookahead_words"] = int(config["alignment_match_lookahead_words"])
    config["alignment_match_similarity_threshold"] = float(config["alignment_match_similarity_threshold"])
    config["alignment_match_warn_ratio"] = float(config["alignment_match_warn_ratio"])
    config["alignment_match_max_extra_ratio"] = float(config["alignment_match_max_extra_ratio"])
    config["clip_reuse_duration_tolerance_ratio"] = float(config["clip_reuse_duration_tolerance_ratio"])
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
    verses: List[Dict[str, Any]] = []
    for raw in text.split("***"):
        lyric_lines: List[str] = []
        directives: List[str] = []

        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if is_bracket_directive_line(stripped):
                directive = strip_bracket_directive(stripped)
                if directive:
                    directives.append(directive)
                continue
            lyric_lines.append(stripped)

        if lyric_lines:
            verses.append({
                "index": len(verses) + 1,
                "text": "\n".join(lyric_lines),
                "lines_text": lyric_lines,
                "bracket_directives": directives,
            })
    return verses




def wrap_flat_text(text: str, max_chars: int = 42) -> List[str]:
    """Wrap flattened lyrics into readable subtitle/prompt lines."""
    words = text.strip().split()
    lines: List[str] = []
    cur: List[str] = []

    for w in words:
        candidate = (" ".join(cur + [w])).strip()
        if cur and len(candidate) > max_chars:
            lines.append(" ".join(cur))
            cur = [w]
        else:
            cur.append(w)

        if cur and re.search(r"[.!?;:]$|[.!?;:]\"$", w) and len(" ".join(cur)) >= max_chars * 0.55:
            lines.append(" ".join(cur))
            cur = []

    if cur:
        lines.append(" ".join(cur))

    return lines or [text.strip()]


def parse_alignment_top_text_as_lyrics(data: Any) -> List[Dict[str, Any]]:
    """Fallback when alignment.json has top-level flattened text with *** separators."""
    text = str(data.get("text", "")).strip() if isinstance(data, dict) else ""
    if not text or "***" not in text:
        return []

    verses: List[Dict[str, Any]] = []
    for raw in text.split("***"):
        raw = raw.strip()
        if not raw:
            continue
        lines = wrap_flat_text(raw)
        verses.append({
            "index": len(verses) + 1,
            "text": "\n".join(lines),
            "lines_text": lines,
        })

    return verses


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


def build_verses_from_lrc(lrc_lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    verses: List[Dict[str, Any]] = []
    cur: List[Dict[str, Any]] = []

    def flush(end_override: Optional[float] = None) -> None:
        nonlocal cur
        if not cur:
            return
        idx = len(verses) + 1
        start = cur[0]["start"]
        end = end_override if end_override is not None else cur[-1]["end"]
        lines = []
        for j, l in enumerate(cur, 1):
            lines.append({
                "index": j,
                "text": l["text"],
                "start": l["start"],
                "end": min(l.get("end", end), end),
                "words": [],
            })
        verses.append({
            "index": idx,
            "start": start,
            "end": end,
            "duration": max(0.01, end - start),
            "text": "\n".join(x["text"] for x in cur),
            "lines": lines,
            "alignment_mode": "line_lrc",
            "bracket_directives": [],
        })
        cur = []

    for line in lrc_lines:
        if line["text"].strip() == "***":
            flush(end_override=line["start"])
        else:
            cur.append(line)
    flush()
    return verses


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


def build_verses_from_json_words(
    words: List[Dict[str, Any]],
    lyrics_verses: List[Dict[str, Any]],
    config: Optional[Dict[str, Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Match clean lyrics.txt ranges against a sequential alignment word stream.

    lyrics.txt is the source of semantic ranges. alignment.json is only timing
    evidence. This function does not require *** in alignment.json.
    """
    config = config or {}

    ignored_meta_words: List[Dict[str, Any]] = []
    clean_words: List[Dict[str, Any]] = []
    for w in words:
        if is_alignment_meta_token(str(w.get("text", ""))):
            ignored_meta_words.append(w)
        else:
            clean_words.append(w)

    report: Dict[str, Any] = {
        "mode": "lyrics_driven_fuzzy_sequential",
        "alignment_words_total": len(words),
        "alignment_words_clean": len(clean_words),
        "ignored_meta_words": ignored_meta_words,
        "ranges": [],
        "trailing_extra_actual": [],
    }

    if not lyrics_verses:
        # Fallback when lyrics.txt is absent: treat the whole alignment as one range.
        ly = {
            "index": 1,
            "text": " ".join(w["text"] for w in clean_words),
            "lines_text": [" ".join(w["text"] for w in clean_words)],
            "bracket_directives": [],
        }
        lyrics_verses = [ly]

    all_expected = expected_words_for_lyrics_verses(lyrics_verses)
    cursor = 0
    verses: List[Dict[str, Any]] = []

    for i, ly in enumerate(lyrics_verses):
        expected_words = all_expected[i]
        next_expected = all_expected[i + 1] if i + 1 < len(all_expected) else []
        matched_words, cursor, range_report = match_expected_range_words(
            expected_words,
            clean_words,
            cursor,
            next_expected,
            config,
        )

        out_lines = split_matched_words_into_lines(ly, matched_words)

        starts = [float(w["start"]) for w in matched_words if w.get("start") is not None]
        ends = [float(w["end"]) for w in matched_words if w.get("end") is not None]
        if starts and ends:
            start = min(starts)
            end = max(ends)
        elif out_lines:
            start = float(out_lines[0]["start"])
            end = float(out_lines[-1]["end"])
        else:
            start = end = 0.0

        verse_report = {
            "range_index": i + 1,
            "lyric_index": ly.get("index", i + 1),
            "text_preview": str(ly.get("text", "")).splitlines()[0] if str(ly.get("text", "")).splitlines() else "",
            **range_report,
            "start": start,
            "end": end,
            "duration": max(0.01, end - start),
        }
        report["ranges"].append(verse_report)

        verses.append({
            "index": i + 1,
            "start": start,
            "end": end,
            "duration": max(0.01, end - start),
            "text": ly["text"],
            "lines": out_lines,
            "alignment_mode": "word_json",
            "bracket_directives": list(ly.get("bracket_directives", [])),
            "alignment_match": verse_report,
        })

    # Any remaining actual words are extra. They are not used in subtitles.
    while cursor < len(clean_words):
        w = clean_words[cursor]
        report["trailing_extra_actual"].append({
            "actual": w.get("text"),
            "start": w.get("start"),
            "end": w.get("end"),
        })
        cursor += 1

    return verses, report



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
    lyrics_text = read_text(input_dir / "lyrics.txt", required=False)
    lyrics_verses = parse_lyrics_txt(lyrics_text) if lyrics_text else []

    json_path = alignment_dir / "alignment.json"
    lrc_path = alignment_dir / "alignment.lrc"

    if json_path.exists():
        data = load_json(json_path)
        words = extract_json_words(data)
        if not words:
            raise RuntimeError(f"No word timestamps found in {json_path}")
        if not lyrics_verses:
            raise RuntimeError("lyrics.txt is required for generated alignment.json parsing")

        verses, match_report = build_verses_from_json_words(words, lyrics_verses, config)
        write_json(debug_dir / "json_words.json", words[:200])
        write_json(debug_dir / "alignment_match_report.json", match_report)
        write_json(debug_dir / "alignment_ignored_meta_words.json", match_report.get("ignored_meta_words", []))
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


def detect_alignment_source(input_dir: Path) -> Tuple[str, Optional[Path], Optional[Path]]:
    vocals = find_first_existing(input_dir, "vocals")
    if vocals:
        return "vocals", vocals, None

    lrc = input_dir / "lyrics.lrc"
    if lrc.exists():
        return "lrc", lrc, None

    legacy_lrc = input_dir / "alignment.lrc"
    if legacy_lrc.exists():
        return "lrc", legacy_lrc, None

    raise FileNotFoundError(
        "No alignment source found. Use input/vocals.* for stable-ts word alignment, "
        "or input/lyrics.lrc for line-level timing when only full audio is available."
    )


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


def prepare_audio(
    mode: str,
    a: Path,
    b: Optional[Path],
    out_dir: Path,
    ffmpeg: str,
    selected_start: float,
    selected_end: float,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    full_mix = out_dir / "full_mix.wav"

    if mode == "stems":
        assert b is not None
        vocals_wav = out_dir / "vocals_48k.wav"
        inst_wav = out_dir / "instrumental_48k.wav"
        run_cmd([ffmpeg, "-y", "-i", str(a), "-ar", "48000", "-ac", "2", str(vocals_wav)])
        run_cmd([ffmpeg, "-y", "-i", str(b), "-ar", "48000", "-ac", "2", str(inst_wav)])
        # Keep stems 1:1. If Suno stems are already normalized, this is usually OK.
        run_cmd([
            ffmpeg, "-y",
            "-i", str(inst_wav),
            "-i", str(vocals_wav),
            "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0,alimiter=limit=0.97",
            "-ar", "48000",
            "-ac", "2",
            str(full_mix),
        ])
    else:
        run_cmd([ffmpeg, "-y", "-i", str(a), "-ar", "48000", "-ac", "2", str(full_mix)])

    cut = out_dir / "full_mix_cut.wav"
    duration = max(0.01, selected_end - selected_start)
    run_cmd([
        ffmpeg, "-y",
        "-ss", f"{selected_start:.3f}",
        "-i", str(full_mix),
        "-t", f"{duration:.3f}",
        "-ar", "48000",
        "-ac", "2",
        str(cut),
    ])
    return cut


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
    return r"{\alpha&HFF&\k" + str(centiseconds(duration)) + "}​" + r"{\alpha&H00&}"



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




def build_plain_subtitle_text(text: str) -> str:
    """Build normal visible subtitle text without karaoke timing tags."""
    return ass_escape(text).replace("\n", r"\N")



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
        if block.get("kind") == "verse":
            verse_to_block[int(block["verse_index"])] = int(block["block_index"])

    events: List[str] = []
    timing_report: List[Dict[str, Any]] = []
    previous_event_end = 0.0

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
                "block_visual_preroll": float(block.get("visual_preroll", 0.0)),
                "text": str(line.get("text", "")),
            })
            previous_event_end = end

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")

    if timing_report_path is not None:
        write_json(timing_report_path, {
            "subtitle_line_preroll_seconds": subtitle_preroll,
            "min_karaoke_unit_seconds": min_unit,
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

        for sub in subranges:
            sub_i = int(sub["sub_index"])
            sub_count = int(sub["sub_count"])
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


def duration_ratio_delta(source_duration: float, target_duration: float) -> float:
    target_duration = max(0.1, float(target_duration))
    return abs(float(source_duration) - target_duration) / target_duration


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


def extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    text = re.sub(r"^<think>.*?</think>\s*", "", text, flags=re.S | re.I)
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return json.loads(text[start:end + 1])
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


def patch_planner_workflow(
    template: Dict[str, Any],
    rules: Dict[str, str],
    video_style: str,
    song_context: Dict[str, Any],
    local_context: str,
    current_block: str,
    plan_path: Path,
    continuity: List[Dict[str, str]],
    block_kind: str,
    block_index: int,
    request_path: Optional[Path] = None,
    template_debug_path: Optional[Path] = None,
) -> Dict[str, Any]:
    wf = json.loads(json.dumps(template))
    if "2" not in wf or "3" not in wf:
        raise RuntimeError("Planner workflow must contain nodes 2=LLM_local and 3=PathSaveStringFile")

    user_prompt, template_name = build_planner_user_prompt(
        rules=rules,
        video_style=video_style,
        song_context=song_context,
        local_context=local_context,
        current_block=current_block,
        continuity=continuity,
        block_kind=block_kind,
        block_index=block_index,
    )

    if request_path is not None:
        save_prompt_debug(request_path, user_prompt)
    if template_debug_path is not None:
        save_prompt_debug(template_debug_path, rules[template_name])

    wf["2"]["inputs"]["system_prompt"] = rules["block_planner_system.txt"]
    wf["2"]["inputs"]["user_prompt"] = user_prompt
    wf["2"]["inputs"]["historical_record"] = ""
    wf["2"]["inputs"]["conversation_rounds"] = 1
    wf["2"]["inputs"]["is_memory"] = "disable"
    wf["2"]["inputs"]["is_locked"] = "disable"
    wf["2"]["inputs"]["main_brain"] = "enable"
    wf["3"]["inputs"]["path"] = str(plan_path)
    return wf


def run_comfy_planner(
    planner_template: Dict[str, Any],
    rules: Dict[str, str],
    video_style: str,
    song_context: Dict[str, Any],
    local_context: str,
    current_block: str,
    index: int,
    block_kind: str,
    comfy_url: str,
    plans_dir: Path,
    continuity: List[Dict[str, str]],
    plan_suffix: str = "",
) -> Dict[str, str]:
    plans_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"plan_{index:03d}{plan_suffix}"
    raw_path = plans_dir / f"{base_name}_raw.json"
    clean_path = plans_dir / f"{base_name}.json"
    request_path = plans_dir / f"{base_name}_request.txt"
    request_json_path = plans_dir / f"{base_name}_request.json"
    template_debug_path = plans_dir / f"{base_name}_template.txt"

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

    log("  [stage] ComfyUI LLM planner")
    wf = patch_planner_workflow(
        planner_template,
        rules,
        video_style,
        song_context,
        local_context,
        current_block,
        raw_path,
        continuity,
        block_kind,
        index,
        request_path=request_path,
        template_debug_path=template_debug_path,
    )
    pid, client_id = queue_prompt(wf, comfy_url)
    log(f"  [planner] prompt_id={pid}")
    h = wait_history(pid, comfy_url, wf, client_id)
    check_history_status(h, plans_dir / f"{base_name}_history.json")

    if not raw_path.exists():
        raise RuntimeError(f"Planner did not write plan file: {raw_path}")

    raw_text = raw_path.read_text(encoding="utf-8").strip()
    plan = extract_json_object(raw_text)
    required = ["scene_summary", "image_prompt", "video_prompt", "negative_prompt"]
    missing = [k for k in required if not str(plan.get(k, "")).strip()]
    if missing:
        raise RuntimeError(f"Planner JSON missing keys: {missing}. Raw response saved to {raw_path}")

    # Do not modify visual prompts in code. Prompt policy belongs in rules/*.txt templates.
    clean_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return {k: str(plan[k]) for k in required}



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


def patch_song_context_workflow(
    template: Dict[str, Any],
    rules: Dict[str, str],
    video_style: str,
    verses: List[Dict[str, Any]],
    raw_path: Path,
    request_path: Path,
    template_debug_path: Path,
) -> Dict[str, Any]:
    wf = json.loads(json.dumps(template))
    if "2" not in wf or "3" not in wf:
        raise RuntimeError("Planner workflow must contain nodes 2=LLM_local and 3=PathSaveStringFile")

    prompt = build_song_context_prompt(rules, video_style, verses)
    save_prompt_debug(request_path, prompt)
    save_prompt_debug(template_debug_path, rules["song_context_user.txt"])

    wf["2"]["inputs"]["system_prompt"] = rules["song_context_system.txt"]
    wf["2"]["inputs"]["user_prompt"] = prompt
    wf["2"]["inputs"]["historical_record"] = ""
    wf["2"]["inputs"]["conversation_rounds"] = 1
    wf["2"]["inputs"]["is_memory"] = "disable"
    wf["2"]["inputs"]["is_locked"] = "disable"
    wf["2"]["inputs"]["main_brain"] = "enable"
    wf["3"]["inputs"]["path"] = str(raw_path)
    return wf


def run_or_load_song_context(
    planner_template: Dict[str, Any],
    rules: Dict[str, str],
    video_style: str,
    verses: List[Dict[str, Any]],
    comfy_url: str,
    plans_dir: Path,
    mode: str,
) -> Dict[str, Any]:
    """Handle global song context according to run mode.

    mode values:
      fresh  : always generate song_context.json
      frozen : require existing song_context.json and load it
      skip   : do not call LLM; return empty context
    """
    plans_dir.mkdir(parents=True, exist_ok=True)
    clean_path = plans_dir / "song_context.json"
    raw_path = plans_dir / "song_context_raw.json"
    request_path = plans_dir / "song_context_request.txt"
    template_debug_path = plans_dir / "song_context_template.txt"

    if mode == "skip":
        if clean_path.exists():
            log(f"[stage] load existing song context for debug/reference: {clean_path}")
            return load_json(clean_path)
        log("[stage] skip song context: not needed for rebuild-final")
        return {}

    if mode == "frozen":
        if not clean_path.exists():
            raise FileNotFoundError(
                "Frozen song context is required for --rework, but it does not exist:\n"
                f"{clean_path}\n"
                "Run a normal full/limit generation first, or remove --rework."
            )
        log(f"[stage] use frozen song context: {clean_path}")
        return load_json(clean_path)

    if mode != "fresh":
        raise RuntimeError(f"Unknown song context mode: {mode}")

    log("[stage] generate song context for a fresh generation run")
    if raw_path.exists():
        raw_path.unlink()
    if clean_path.exists():
        clean_path.unlink()

    wf = patch_song_context_workflow(
        planner_template,
        rules,
        video_style,
        verses,
        raw_path,
        request_path,
        template_debug_path,
    )
    pid, client_id = queue_prompt(wf, comfy_url)
    log(f"[song-context] prompt_id={pid}")
    h = wait_history(pid, comfy_url, wf, client_id)
    check_history_status(h, plans_dir / "song_context_history.json")

    if not raw_path.exists():
        raise RuntimeError(f"Song-context planner did not write file: {raw_path}")

    raw_text = raw_path.read_text(encoding="utf-8").strip()
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


def build_current_block_instruction(block: Dict[str, Any], verses: List[Dict[str, Any]]) -> str:
    kind = str(block.get("kind", "verse"))
    directives = block.get("bracket_directives") or []
    directive_text = ""
    if directives:
        directive_text = (
            "\n\nBRACKET DIRECTIVES, lower priority than actual lyrics:\n"
            + "\n".join(f"- {x}" for x in directives)
            + "\nThese are songwriter or generation metadata from bracketed lyric lines. "
              "Do not treat them as sung lyrics. If they conflict with actual lyrics, lyrics win."
        )

    if kind == "intro":
        return (
            "This is the opening instrumental intro before any lyrics. "
            "Create an establishing opening visual for the whole song: introduce the world, main characters, mood and recurring motifs. "
            "Do not depict one specific verse event too literally. No visible text."
        ) + directive_text
    if kind == "outro":
        return (
            "This is the closing/outro after the final lyric. "
            "Create a warm final tableau that resolves the whole song visually. "
            "Do not introduce new plot events. No visible text."
        ) + directive_text
    if kind == "instrumental":
        return (
            "This is an instrumental break with no sung lyrics. "
            "Create a visual musical interlude or transition between the surrounding lyric sections. "
            "Do not render lyrics, captions, signs, or any visible text."
        ) + directive_text
    return "Current verse/block text:\n" + str(block.get("text", "")) + directive_text




def comfy_block_part_subdir(run_id: str, block_index: int, sub_index: int) -> str:
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


def upload_image_to_comfy(image_path: Path, comfy_url: str, subfolder: str) -> str:
    if not image_path.exists():
        raise FileNotFoundError(f"Image to upload not found: {image_path}")

    with image_path.open("rb") as f:
        r = requests.post(
            comfy_url.rstrip("/") + "/upload/image",
            files={"image": (image_path.name, f, "image/png")},
            data={"subfolder": subfolder, "overwrite": "true", "type": "input"},
            timeout=120,
        )
    try:
        r.raise_for_status()
    except Exception:
        log("ComfyUI /upload/image error:")
        log(r.text[:4000])
        raise

    data = r.json()
    name = data.get("name") or image_path.name
    returned_subfolder = data.get("subfolder")
    if returned_subfolder:
        return f"{returned_subfolder}/{name}".replace("\\", "/")
    if subfolder:
        return f"{subfolder}/{name}".replace("\\", "/")
    return str(name)


def timed_line_segments_for_block(block: Dict[str, Any]) -> List[Dict[str, Any]]:
    if str(block.get("kind")) != "verse":
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
            "start": ls,
            "end": le,
            "text": str(line.get("text", "")),
            "words": line.get("words", []),
        })

    return segments


def subrange_text_for_block(block: Dict[str, Any], sub_start: float, sub_end: float) -> str:
    if str(block.get("kind")) != "verse":
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


def split_boundaries_for_block(block: Dict[str, Any], config: Dict[str, Any]) -> List[float]:
    start = float(block["start"])
    end = float(block["end"])
    duration = max(0.01, end - start)
    max_seconds = float(config["max_workflow_seconds"])
    recommended = float(config["recommended_workflow_seconds"])

    if duration <= max_seconds:
        return [start, end]

    parts = max(2, int(math.ceil(duration / max(1.0, recommended))))
    target = duration / parts

    candidates = {start, end}
    if str(block.get("kind")) == "verse":
        for seg in timed_line_segments_for_block(block):
            if start < float(seg["end"]) < end:
                candidates.add(float(seg["end"]))
            for w in seg.get("words") or []:
                we = float(w.get("end", start))
                if start < we < end:
                    candidates.add(we)

    sorted_candidates = sorted(candidates)
    boundaries = [start]
    previous = start

    for part in range(1, parts):
        desired = start + target * part
        min_remaining = (parts - part) * 1.0
        valid = [c for c in sorted_candidates if previous + 1.0 <= c <= end - min_remaining]
        if valid:
            chosen = min(valid, key=lambda c: abs(c - desired))
        else:
            chosen = min(max(desired, previous + 1.0), end - min_remaining)

        if chosen <= previous + 0.05:
            chosen = min(end - min_remaining, previous + 1.0)
        boundaries.append(float(chosen))
        previous = float(chosen)

    boundaries.append(end)

    clean = [boundaries[0]]
    for b in boundaries[1:]:
        if b > clean[-1] + 0.05:
            clean.append(b)
    if clean[-1] < end:
        clean.append(end)
    return clean


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
            "sub_index": i + 1,
            "sub_count": count,
            "start": sub_start,
            "end": sub_end,
            "duration": max(0.01, sub_end - sub_start),
            "text": text,
            "text_mode": "whole_range" if count == 1 else "slice",
        })

    return out


def build_subrange_instruction(block: Dict[str, Any], subrange: Dict[str, Any]) -> str:
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
            "CURRENT SUBRANGE TEXT — HIGHEST FACTUAL PRIORITY:\n"
            + sub_text
            + "\n\nDepict this current subrange as the main action. "
              "Use the full semantic range only for continuity and meaning."
        )
    else:
        subrange_section = (
            "CURRENT SUBRANGE — HIGHEST FACTUAL PRIORITY:\n"
            "No lyrics are sung in this subrange. Continue the visual motion of this semantic range."
        )

    return (
        f"SEMANTIC RANGE:\n"
        f"Block index: {int(block['block_index']):03d}\n"
        f"Kind: {kind}\n"
        f"Subrange: {int(subrange['sub_index'])} of {int(subrange['sub_count'])}\n"
        f"Time: {float(subrange['start']):.3f}s..{float(subrange['end']):.3f}s\n\n"
        f"BRACKET DIRECTIVES, metadata for the whole semantic range:\n{directive_text}\n\n"
        f"FULL SEMANTIC RANGE LYRICS / RANGE TEXT:\n{full_text}\n\n"
        f"{subrange_section}\n\n"
        "Priority rules:\n"
        "- Always follow VISUAL STYLE for medium, look, palette, character design, camera and rendering.\n"
        "- For factual action, follow CURRENT SUBRANGE when it provides text.\n"
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


def patch_image_workflow(
    template: Dict[str, Any],
    image_prompt: str,
    block_index: int,
    sub_index: int,
    run_id: str,
    image_seed: int,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    wf = json.loads(json.dumps(template))
    width = int(config["width"])
    height = int(config["height"])
    segment_dir = comfy_block_part_subdir(run_id, block_index, sub_index)

    wf[IMAGE_N["image_prompt"]]["inputs"]["text"] = image_prompt
    wf[IMAGE_N["image_latent"]]["inputs"]["width"] = width
    wf[IMAGE_N["image_latent"]]["inputs"]["height"] = height
    wf[IMAGE_N["image_scheduler"]]["inputs"]["width"] = width
    wf[IMAGE_N["image_scheduler"]]["inputs"]["height"] = height
    wf[IMAGE_N["image_noise"]]["inputs"]["noise_seed"] = int(image_seed)
    wf[IMAGE_N["image_save"]]["inputs"]["filename_prefix"] = f"{segment_dir}/start_image"
    return wf


def patch_video_from_image_workflow(
    template: Dict[str, Any],
    start_image_name: str,
    visual_plan: Dict[str, str],
    duration: float,
    block_index: int,
    sub_index: int,
    run_id: str,
    seeds: Dict[str, int],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    wf = json.loads(json.dumps(template))
    width = int(config["width"])
    height = int(config["height"])
    fps = int(config["fps"])
    recommended_seconds = float(config["recommended_workflow_seconds"])
    max_seconds = float(config["max_workflow_seconds"])
    seconds = max(1, min(int(max_seconds), math.ceil(duration)))

    if duration > max_seconds:
        raise RuntimeError(
            f"Block {block_index:03d} part {sub_index:03d} duration is {duration:.2f}s, "
            f"but this video workflow hard-limits at {max_seconds:.2f}s."
        )
    if duration > recommended_seconds:
        log(
            f"  [warn] block {block_index:03d} part {sub_index:03d} duration is {duration:.2f}s; "
            f"workflow is optimized for <= {recommended_seconds:.2f}s and quality may degrade."
        )

    segment_dir = comfy_block_part_subdir(run_id, block_index, sub_index)

    wf[VIDEO_N["start_image"]]["inputs"]["image"] = start_image_name
    wf[VIDEO_N["video_prompt"]]["inputs"]["value"] = visual_plan["video_prompt"]
    if VIDEO_N["video_negative"] in wf:
        wf[VIDEO_N["video_negative"]]["inputs"]["text"] = visual_plan.get("negative_prompt", "")
    wf[VIDEO_N["video_seconds"]]["inputs"]["value"] = seconds
    wf[VIDEO_N["video_fps"]]["inputs"]["value"] = fps
    wf[VIDEO_N["video_width"]]["inputs"]["value"] = width
    wf[VIDEO_N["video_height"]]["inputs"]["value"] = height
    wf[VIDEO_N["video_noise"]]["inputs"]["noise_seed"] = int(seeds["video_seed"])
    wf[VIDEO_N["video_refine_noise"]]["inputs"]["noise_seed"] = int(seeds["video_refine_seed"])
    wf[VIDEO_N["video_save"]]["inputs"]["filename_prefix"] = f"{segment_dir}/video"
    return wf



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


def instrumental_gap_threshold(verses: List[Dict[str, Any]], config: Dict[str, Any]) -> float:
    durations = [float(v.get("duration", 0.0)) for v in verses if float(v.get("duration", 0.0)) > 0.01]
    median_duration = statistics.median(durations) if durations else 0.0
    return max(
        float(config["instrumental_gap_min_seconds"]),
        median_duration * float(config["instrumental_gap_min_ratio_of_median_verse"]),
    )




def should_create_silent_gap_block(gap_duration: float, verses: List[Dict[str, Any]], config: Dict[str, Any]) -> bool:
    return float(gap_duration) >= instrumental_gap_threshold(verses, config)
def make_timeline_blocks(
    all_verses: List[Dict[str, Any]],
    selected_verses: List[Dict[str, Any]],
    audio_duration: float,
    has_limit: bool,
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    """Create continuous visual timeline blocks.

    Lyric timing remains unchanged. Short intro/outro gaps are merged into the
    first/last lyric range using the same threshold as instrumental gaps.
    """
    total = len(all_verses)
    selected_count = len(selected_verses)
    full_song = selected_count >= total and not has_limit

    if selected_count >= total:
        full_song = True

    if full_song:
        audio_end: Optional[float] = None
        timeline_end = audio_duration
    else:
        next_verse = all_verses[selected_count]
        timeline_end = float(next_verse["start"])
        audio_end = timeline_end

    blocks: List[Dict[str, Any]] = []
    next_block_index = 0
    desired_preroll = max(0.0, float(config.get("range_visual_preroll_seconds", 0.0)))

    visual_starts: List[float] = []
    previous_lyric_end = 0.0
    for verse in selected_verses:
        lyric_start = float(verse["start"])
        available_gap = max(0.0, lyric_start - previous_lyric_end)
        actual_preroll = min(desired_preroll, available_gap)
        visual_starts.append(max(0.0, lyric_start - actual_preroll))
        previous_lyric_end = float(verse["end"])

    first_visual_start = visual_starts[0]
    intro_is_separate = should_create_silent_gap_block(first_visual_start, all_verses, config)

    if intro_is_separate:
        intro_text = (
            "Opening instrumental/intro block for the whole song. "
            "Establish the main setting, recurring characters and mood before the first lyric starts."
        )
        blocks.append({
            "block_index": next_block_index,
            "kind": "intro",
            "verse_index": 0,
            "start": 0.0,
            "end": first_visual_start,
            "duration": first_visual_start,
            "text": intro_text,
            "bracket_directives": [],
        })
        next_block_index += 1
    else:
        # Too short to be a meaningful generated clip. Fold it into the first
        # lyric range so we do not render a 0.x second intro video.
        visual_starts[0] = 0.0

    for pos, verse in enumerate(selected_verses):
        verse_index = int(verse["index"])
        lyric_start = float(verse["start"])
        start = float(visual_starts[pos])
        lyric_end = float(verse["end"])
        actual_preroll = max(0.0, lyric_start - start)

        if pos + 1 < len(selected_verses):
            next_lyric_start = float(selected_verses[pos + 1]["start"])
            next_visual_start = float(visual_starts[pos + 1])
            next_verse_index: Optional[int] = int(selected_verses[pos + 1]["index"])
        elif full_song:
            next_lyric_start = lyric_end
            next_visual_start = lyric_end
            next_verse_index = None
        else:
            next_lyric_start = timeline_end
            next_visual_start = timeline_end
            next_verse_index = int(all_verses[selected_count]["index"]) if selected_count < total else None

        lyric_gap = max(0.0, next_lyric_start - lyric_end)
        split_gap = should_create_silent_gap_block(lyric_gap, all_verses, config) and next_lyric_start > lyric_end

        verse_end = lyric_end if split_gap else next_visual_start
        if verse_end <= start:
            verse_end = lyric_end

        blocks.append({
            "block_index": next_block_index,
            "kind": "verse",
            "verse_index": verse_index,
            "start": start,
            "end": verse_end,
            "duration": max(0.01, verse_end - start),
            "text": verse.get("text", ""),
            "verse": verse,
            "lyric_start": lyric_start,
            "lyric_end": lyric_end,
            "visual_preroll": actual_preroll,
            "bracket_directives": list(verse.get("bracket_directives", [])),
        })
        next_block_index += 1

        if split_gap:
            instrumental_end = next_visual_start
            blocks.append({
                "block_index": next_block_index,
                "kind": "instrumental",
                "verse_index": verse_index,
                "previous_verse_index": verse_index,
                "next_verse_index": next_verse_index,
                "start": lyric_end,
                "end": instrumental_end,
                "duration": max(0.01, instrumental_end - lyric_end),
                "text": (
                    f"Instrumental break with no sung lyrics between verse {verse_index}"
                    + (f" and verse {next_verse_index}." if next_verse_index else " and the end of the selected range.")
                ),
                "bracket_directives": [],
            })
            next_block_index += 1

    if full_song:
        last = selected_verses[-1]
        outro_start = float(last["end"])
        outro_end = audio_duration
        outro_duration = max(0.0, outro_end - outro_start)
        if should_create_silent_gap_block(outro_duration, all_verses, config):
            outro_text = (
                "Closing instrumental/outro block for the whole song. "
                "Create a final warm visual tableau that resolves the story without any text."
            )
            blocks.append({
                "block_index": next_block_index,
                "kind": "outro",
                "verse_index": int(last["index"]) + 1,
                "start": outro_start,
                "end": outro_end,
                "duration": max(0.01, outro_duration),
                "text": outro_text,
                "bracket_directives": [],
            })
        elif outro_duration > 0.0 and blocks:
            # Too short for a standalone outro clip; merge into the previous
            # visual range so final video still covers full audio duration.
            blocks[-1]["end"] = outro_end
            blocks[-1]["duration"] = max(0.01, float(blocks[-1]["end"]) - float(blocks[-1]["start"]))

    return blocks, audio_end


def clip_filename_for_block(block: Dict[str, Any]) -> str:
    idx = int(block["block_index"])
    kind = str(block["kind"])
    if kind == "intro":
        return "clip_000_intro.mp4"
    if kind == "outro":
        return f"clip_{idx:03d}_outro.mp4"
    if kind == "instrumental":
        return f"clip_{idx:03d}_instrumental.mp4"
    verse_index = int(block.get("verse_index", idx))
    return f"clip_{idx:03d}_verse_{verse_index:03d}.mp4"


def block_clip_path(clips_dir: Path, block: Dict[str, Any]) -> Path:
    return clips_dir / clip_filename_for_block(block)




def load_continuity_from_plans(plans_dir: Path, before_block_index: int) -> List[Dict[str, str]]:
    """Load previous saved scene summaries for visual continuity."""
    out: List[Dict[str, str]] = []
    if not plans_dir.exists():
        return out

    for p in sorted(plans_dir.glob("plan_*.json")):
        m = re.match(r"plan_(\d+)\.json$", p.name)
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
            out.append({"segment": str(idx), "scene_summary": summary})

    return out


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
    copy_file_if_exists(plans_dir / f"{base_name}_template.txt", part_debug_dir / "planner_template.txt")
    copy_file_if_exists(plans_dir / f"{base_name}_raw.json", part_debug_dir / "planner_raw.json")
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

    for subrange in subranges:
        sub_i = int(subrange["sub_index"])
        pdir = range_part_debug_dir(debug_dir, block_i, sub_i)
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "subrange_text.txt").write_text(str(subrange.get("text", "")), encoding="utf-8")
        write_json(pdir / "subrange_context.json", subrange)

    return rdir



def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a ComfyUI music video from input-dir files.")
    ap.add_argument("--input-dir", default="input", help="Folder containing all song input files. Defaults to ./input.")
    ap.add_argument("--output-dir", default="output", help="Folder for all generated artifacts. Defaults to ./output.")
    ap.add_argument("--limit", type=int, default=0, help="Use only first N verses/clips for testing.")
    ap.add_argument("--rework", nargs="*", type=int, default=None, help="Generate only these block numbers; reuse existing clips for other selected blocks. 0=intro, 1..N=verses, N+1=outro.")
    ap.add_argument("--rebuild-final", action="store_true", help="Do not generate video; reuse existing unscaled clips and rebuild scaled clips/final only.")
    ap.add_argument("--refresh-alignment", action="store_true", help="Rebuild alignment/timeline from current input lyrics before deciding which visual clips to reuse or regenerate.")
    ap.add_argument("--preview-subtitles-only", action="store_true", help="Build karaoke subtitles and subtitle_preview.mp4, then stop before any ComfyUI LLM/image/video generation.")
    ap.add_argument("--comfy-url", default=None, help="Override comfy_url from config.json.")
    ap.add_argument("--comfy-output-dir", default=None, help="Override comfy_output_dir from config.json.")
    ap.add_argument("--lyrics-language", default="en", help="Language code for stable-ts alignment. Default: en.")
    args = ap.parse_args()

    stats: Dict[str, float] = {"_run_start": time.perf_counter()}
    clips_generated = 0
    clips_reused = 0
    run_id = make_run_id()
    generation_info: Dict[int, Dict[str, Any]] = {}
    fresh_generation_run = not args.rebuild_final and not args.rework and not args.refresh_alignment
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

    log(f"[stage] input dir : {input_dir}")
    log(f"[stage] output dir: {output_root}")
    log(f"[stage] workflows : {workflow_dir}")
    log(f"[stage] run id    : {run_id}")
    log(f"[stage] rules     : {rules_dir}")
    log(f"[stage] data      : {data_dir}")
    log(f"[stage] ffmpeg    : {ffmpeg_cmd}")
    log(f"[stage] ffprobe   : {ffprobe_cmd}")
    log(f"[stage] stable-ts : {stable_ts_cmd}")

    if fresh_generation_run:
        log(f"[stage] fresh run: clean output dir {output_root}")
        clean_fresh_output_dir(output_root)

    log("[stage] read style/workflows/rules")
    rules = load_rules(rules_dir)
    video_style_path = input_dir / "video_style.txt"
    if not video_style_path.exists():
        raise FileNotFoundError(f"Required file not found: {video_style_path}")
    video_style = read_text(video_style_path)
    config = load_config(input_dir, data_dir)
    width = int(config["width"])
    height = int(config["height"])
    comfy_url = args.comfy_url or str(config["comfy_url"])
    output_dir = Path(args.comfy_output_dir).resolve() if args.comfy_output_dir else resolve_config_path(str(config["comfy_output_dir"]), script_dir)
    planner_template = load_json(workflow_dir / "planner_visual_prompts_api.json")
    image_template = load_json(workflow_dir / "image_from_prompt_api.json")
    video_template = load_json(workflow_dir / "video_from_image_api.json")
    write_json(debug_dir / "config_used.json", config)
    log(f"[stage] comfy url : {comfy_url}")
    log(f"[stage] comfy out : {output_dir}")
    block_video_styles, video_style_report = load_block_video_styles(input_dir, video_style, debug_dir)

    if fresh_generation_run or args.refresh_alignment:
        align_kind, align_path, _ = detect_alignment_source(input_dir)
        alignment_dir.mkdir(parents=True, exist_ok=True)
        for stale_alignment in (alignment_dir / "alignment.json", alignment_dir / "alignment.lrc"):
            if stale_alignment.exists():
                stale_alignment.unlink()
        if align_kind == "vocals":
            run_stable_ts_alignment(
                input_dir,
                out_dir,
                debug_dir,
                stable_ts_cmd,
                args.lyrics_language,
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
    has_effective_limit = bool(args.limit and args.limit < total_verses)
    if args.limit and args.limit > total_verses:
        raise RuntimeError(f"--limit {args.limit} is greater than total verses {total_verses}")

    selected = verses[: args.limit] if args.limit and args.limit < total_verses else verses
    if not selected:
        raise RuntimeError("No selected verses.")

    log("[stage] prepare full audio")
    stats_start(stats, "prepare_audio")
    audio_mode, audio_a, audio_b = detect_audio(input_dir)
    log(f"[stage] audio mode={audio_mode}")
    full_mix = prepare_audio_full_mix(audio_mode, audio_a, audio_b, out_dir / "audio", ffmpeg_cmd)
    audio_duration = get_audio_duration(full_mix, ffprobe_cmd)
    log(f"[stage] full audio duration={audio_duration:.2f}s")
    stats_end(stats, "prepare_audio")

    stats_start(stats, "timeline")
    blocks, audio_end = make_timeline_blocks(verses, selected, audio_duration, has_effective_limit, config)
    if not blocks:
        raise RuntimeError("No timeline blocks created.")

    # Subtitle preview is always full-song, regardless of --limit.
    # --limit affects generation/final assembly only.
    preview_blocks, preview_audio_end = make_timeline_blocks(verses, verses, audio_duration, False, config)
    if not preview_blocks:
        raise RuntimeError("No full-song preview timeline blocks created.")
    stats_end(stats, "timeline")

    block_indices = {int(b["block_index"]) for b in blocks}
    if rework_indices:
        outside = sorted(rework_indices - block_indices)
        if outside:
            raise RuntimeError(
                f"--rework contains block(s) outside current selected timeline: {outside}. "
                f"Available blocks are {sorted(block_indices)}. "
                f"Use 0 for intro, 1..N for verses, N+1 for outro on full song."
            )

    log(f"[stage] verses total={total_verses} selected={len(selected)} alignment={alignment_mode}")
    log(f"[stage] timeline blocks={len(blocks)} range=0.000s..{blocks[-1]['end']:.3f}s")
    log(f"[stage] subtitle preview timeline blocks={len(preview_blocks)} range=0.000s..{preview_blocks[-1]['end']:.3f}s (full song)")
    if has_effective_limit:
        log(f"[stage] limit mode: audio/video ends at start of verse {len(selected) + 1:03d}")
    else:
        log("[stage] full-song mode: audio is not cut by verse boundaries")

    if args.rebuild_final:
        log("[stage] rebuild-final mode: no video generation; rebuild scaled clips/final only")
    elif args.refresh_alignment and rework_indices:
        log(f"[stage] refresh-alignment + rework mode: regenerate blocks {sorted(rework_indices)} and validate/reuse the rest")
    elif args.refresh_alignment:
        log("[stage] refresh-alignment mode: reuse compatible unscaled clips and generate missing/incompatible ranges")
    elif rework_indices:
        log(f"[stage] rework mode: regenerate blocks {sorted(rework_indices)} and reuse the rest")
    else:
        log("[stage] full generation mode for selected timeline blocks")

    write_json(debug_dir / "timeline_blocks.json", blocks)
    write_json(debug_dir / "preview_timeline_blocks.json", preview_blocks)

    log("[stage] render audio for timeline")
    stats_start(stats, "render_audio")
    final_audio = render_audio_for_timeline(full_mix, out_dir / "audio", ffmpeg_cmd, audio_end)
    render_duration = ffprobe_duration(final_audio, ffprobe_cmd)
    log(f"[stage] render audio duration={render_duration:.2f}s")
    stats_end(stats, "render_audio")

    log("[stage] build karaoke subtitles")
    stats_start(stats, "subtitles")
    subtitle_mode = "word" if alignment_mode == "json" else "line"
    ass_path = out_dir / "subs" / "karaoke.ass"
    preview_ass_path = out_dir / "subs" / "preview_karaoke.ass"
    style_section, subtitle_style_map, subtitle_style_report = build_subtitle_styles_for_blocks(
        preview_blocks,
        input_dir,
        data_dir,
        debug_dir,
    )
    # Release subtitles follow the selected timeline used by final assembly.
    build_ass_subtitles(
        selected,
        blocks,
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
    # Preview subtitles are always full-song, independent of --limit.
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
    log(f"[stage] subtitles: {ass_path} ({subtitle_mode})")
    log(f"[stage] preview subtitles: {preview_ass_path} ({subtitle_mode}, full song)")
    stats_end(stats, "subtitles")

    preview_debug_ass = out_dir / "subs" / "preview_debug.ass"
    build_preview_debug_ass(preview_blocks, preview_debug_ass, width, height, config, audio_duration)

    log("[stage] render subtitle preview")
    stats_start(stats, "subtitle_preview")
    subtitle_preview = output_root / "subtitle_preview.mp4"
    render_subtitle_preview(
        full_mix,
        preview_ass_path,
        subtitle_preview,
        audio_duration,
        width,
        height,
        int(config["fps"]),
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
            "fresh_generation_run": fresh_generation_run,
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
        "fresh_generation_run": fresh_generation_run,
        "refresh_alignment": bool(args.refresh_alignment),
    })
    clips_dir.mkdir(parents=True, exist_ok=True)
    clips_unscaled_dir.mkdir(parents=True, exist_ok=True)
    subclips_raw_root.mkdir(parents=True, exist_ok=True)
    subclips_video_root.mkdir(parents=True, exist_ok=True)
    frames_root.mkdir(parents=True, exist_ok=True)

    reuse_tolerance = float(config["clip_reuse_duration_tolerance_ratio"])
    blocks_to_generate: set = set()
    reuse_plan: Dict[int, Dict[str, Any]] = {}

    for block in blocks:
        block_i = int(block["block_index"])
        duration = max(0.1, float(block["duration"]))
        unscaled_clip = block_clip_path(clips_unscaled_dir, block)

        if args.rebuild_final:
            should_generate = False
        elif rework_indices:
            should_generate = block_i in rework_indices
        elif fresh_generation_run:
            should_generate = True
        else:
            should_generate = not unscaled_clip.exists()

        if not should_generate and unscaled_clip.exists():
            source_duration = ffprobe_duration(unscaled_clip, ffprobe_cmd)
            ratio_delta = duration_ratio_delta(source_duration, duration)
            reuse_plan[block_i] = {
                "source": str(unscaled_clip.relative_to(output_root)) if unscaled_clip.is_relative_to(output_root) else str(unscaled_clip),
                "source_duration": source_duration,
                "target_duration": duration,
                "ratio_delta": ratio_delta,
                "tolerance": reuse_tolerance,
            }
            if args.refresh_alignment and rework_indices and ratio_delta > reuse_tolerance:
                raise RuntimeError(
                    f"Existing unscaled clip is not compatible with refreshed alignment for block {block_i:03d}:\n"
                    f"  clip duration={source_duration:.3f}s current range duration={duration:.3f}s ratio_delta={ratio_delta:.3f}\n"
                    f"  tolerance={reuse_tolerance:.3f}. Add --rework {block_i} or run full generation."
                )
            if args.refresh_alignment and not rework_indices and ratio_delta > reuse_tolerance:
                should_generate = True

        if not should_generate and not unscaled_clip.exists():
            if args.rebuild_final:
                raise FileNotFoundError(
                    f"Existing unscaled clip required but not found: {unscaled_clip}\n"
                    "Run visual generation first, or remove --rebuild-final."
                )
            should_generate = True

        if should_generate:
            blocks_to_generate.add(block_i)

    write_json(debug_dir / "clip_reuse_plan.json", reuse_plan)

    song_context: Optional[Dict[str, Any]] = None
    if blocks_to_generate:
        song_context_mode = "fresh" if (fresh_generation_run or args.refresh_alignment or not rework_indices) else "frozen"
        stats_start(stats, "song_context")
        song_context = run_or_load_song_context(
            planner_template,
            rules,
            video_style,
            verses,
            comfy_url,
            plans_dir,
            song_context_mode,
        )
        write_json(debug_dir / "song_context_used.json", {
            "mode": song_context_mode,
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

        log(f"\n=== block {block_i:03d} / {kind}: {first}")
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
            generation_info[block_i] = {
                "run_id": run_id,
                "segment_subdir": None,
                "seeds": None,
                "generated_in_this_run": False,
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

        if kind == "instrumental":
            local_context = build_instrumental_local_context(
                verses,
                int(block.get("previous_verse_index", 0)),
                block.get("next_verse_index"),
            )
        else:
            local_context = build_local_context(
                verses,
                int(block.get("verse_index", block_i)),
                int(config["local_context_radius"]),
            )

        block_video_style = effective_video_style(block_i, video_style, block_video_styles)
        base_continuity = load_continuity_from_plans(plans_dir, block_i)
        part_continuity = list(base_continuity)
        subclip_paths: List[Path] = []
        subrange_infos: List[Dict[str, Any]] = []
        previous_subclip: Optional[Path] = None

        for subrange in subranges:
            sub_i = int(subrange["sub_index"])
            sub_count = int(subrange["sub_count"])
            sub_duration = max(0.1, float(subrange["duration"]))
            sub_dir = comfy_block_part_subdir(run_id, block_i, sub_i)
            plan_suffix = f"_part_{sub_i:03d}"
            plan_base_name = f"plan_{block_i:03d}{plan_suffix}"

            log(f"  [subrange] {sub_i}/{sub_count} time={float(subrange['start']):.3f}s..{float(subrange['end']):.3f}s duration={sub_duration:.2f}s")

            current_instruction = build_subrange_instruction(block, subrange)
            part_debug_dir = range_part_debug_dir(debug_dir, block_i, sub_i)
            part_debug_dir.mkdir(parents=True, exist_ok=True)
            write_json(part_debug_dir / "planner_context.json", {
                "block_index": block_i,
                "block_kind": kind,
                "subrange": subrange,
                "video_style_source": video_style_report.get("blocks", {}).get(str(block_i), video_style_report.get("default", {})),
                "video_style": block_video_style,
                "song_context": song_context,
                "local_context": local_context,
                "current_block": current_instruction,
                "continuity": part_continuity[-5:],
            })

            plan = run_comfy_planner(
                planner_template,
                rules,
                block_video_style,
                song_context,
                local_context,
                current_instruction,
                block_i,
                kind,
                comfy_url,
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

            if sub_i == 1:
                log("  [stage] queue start image")
                iwf = patch_image_workflow(
                    image_template,
                    plan["image_prompt"],
                    block_i,
                    sub_i,
                    run_id,
                    seeds["image_seed"],
                    config,
                )
                write_json(part_debug_dir / "image_patched.json", iwf)
                pid, client_id = queue_prompt(iwf, comfy_url)
                log(f"  [image] prompt_id={pid}")
                ih = wait_history(pid, comfy_url, iwf, client_id)
                check_history_status(ih, part_debug_dir / "image_history.json")
                image_path = find_result_file(ih, output_dir, sub_dir, "start_image", {".png", ".jpg", ".jpeg", ".webp"})
                if not image_path:
                    raise RuntimeError(f"Start image result not found for block {block_i:03d} part {sub_i:03d}")
                shutil.copy2(image_path, start_image_local)
            else:
                if previous_subclip is None:
                    raise RuntimeError(f"Internal error: no previous subclip for block {block_i:03d} part {sub_i:03d}")
                log("  [stage] extract previous last frame as next start image")
                extract_last_frame(previous_subclip, start_image_local, ffmpeg_cmd)

            comfy_input_name = upload_image_to_comfy(
                start_image_local,
                comfy_url,
                f"aligned_song_inputs/{run_id}/block_{block_i:03d}",
            )

            log("  [stage] patch video-from-image workflow")
            log(f"  [seeds] image={seeds['image_seed']} video={seeds['video_seed']} refine={seeds['video_refine_seed']}")
            vwf = patch_video_from_image_workflow(
                video_template,
                comfy_input_name,
                plan,
                sub_duration,
                block_i,
                sub_i,
                run_id,
                seeds,
                config,
            )
            write_json(part_debug_dir / "video_patched.json", vwf)

            log("  [stage] queue video")
            pid, client_id = queue_prompt(vwf, comfy_url)
            log(f"  [video] prompt_id={pid}")
            vh = wait_history(pid, comfy_url, vwf, client_id)
            check_history_status(vh, part_debug_dir / "video_history.json")
            video_path = find_result_file(vh, output_dir, sub_dir, "video", {".mp4", ".mov", ".webm", ".mkv"})
            if not video_path:
                raise RuntimeError(f"Video result not found for block {block_i:03d} part {sub_i:03d}")

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
                "sub_index": sub_i,
                "sub_count": sub_count,
                "start": float(subrange["start"]),
                "end": float(subrange["end"]),
                "duration": sub_duration,
                "text": str(subrange.get("text", "")),
                "text_mode": str(subrange.get("text_mode", "")),
                "scene_summary": scene_summary,
                "comfy_subdir": sub_dir,
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

        scene_summary = " / ".join(x.get("scene_summary", "") for x in subrange_infos if x.get("scene_summary"))
        if not scene_summary:
            scene_summary = f"{kind} block {block_i:03d}, rendered as {len(subrange_infos)} internal subrange(s)"
        aggregate_plan = {
            "scene_summary": scene_summary,
            "split_parts": len(subrange_infos),
            "subranges": [
                {
                    "sub_index": x.get("sub_index"),
                    "sub_count": x.get("sub_count"),
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
            "range_debug": str(range_dir.relative_to(output_root)) if range_dir.is_relative_to(output_root) else str(range_dir),
            "unscaled_clip": str(unscaled_clip.relative_to(output_root)) if unscaled_clip.is_relative_to(output_root) else str(unscaled_clip),
            "subranges": subrange_infos,
        }
        write_json(debug_dir / f"video_generation_{block_i:03d}.json", generation_info[block_i])

        clips_generated += 1
        stats_end(stats, "video_generation")

    log("\n[stage] scale unscaled clips to current timeline")
    stats_start(stats, "clip_scaling")
    scaling_report: List[Dict[str, Any]] = []
    for block in blocks:
        block_i = int(block["block_index"])
        unscaled_clip = block_clip_path(clips_unscaled_dir, block)
        clip_local = block_clip_path(clips_dir, block)
        if not unscaled_clip.exists():
            raise FileNotFoundError(f"Unscaled clip not found for block {block_i:03d}: {unscaled_clip}")
        log(f"  [scale] block {block_i:03d}: {unscaled_clip.name} -> {clip_local.name}")
        scale_info = retime_video_copy(
            unscaled_clip,
            max(0.1, float(block["duration"])),
            clip_local,
            ffmpeg_cmd,
            ffprobe_cmd,
            int(config["fps"]),
        )
        scale_info["block_index"] = block_i
        scale_info["kind"] = str(block.get("kind", ""))
        scaling_report.append(scale_info)
        clips.append(clip_local)
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
    final_mux(video_only, final_audio, ass_path, final, ffmpeg_cmd, int(config["fps"]))
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
