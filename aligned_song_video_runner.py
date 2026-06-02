from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import random
import statistics
import websocket

COMFY_URL = "http://127.0.0.1:8188"
COMFY_OUTPUT_DIR = Path(r"G:\Git\ComfyUI\output")
FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"

# Video duration policy.
VIDEO_RECOMMENDED_SECONDS = 20
VIDEO_MAX_SECONDS = 30

# Prompt planning context policy.
LOCAL_CONTEXT_RADIUS = 2

VIDEO_N = {
    "image_prompt": "1004",
    "image_latent": "1007",
    "image_scheduler": "1024",
    "image_noise": "1022",
    "image_save": "1011",
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


def norm_word(s: str) -> str:
    s = s.casefold().replace("\u0451", "\u0435").replace("\u0401", "\u0435")
    s = re.sub(r"[^\w]+", "", s, flags=re.U)
    return s


def lyric_words(text: str) -> List[str]:
    return [w for w in re.findall(r"\w+(?:[-\']\w+)?", text, flags=re.U) if norm_word(w)]



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
        "width": int,
        "height": int,
        "fps": int,
        "recommended_workflow_seconds": (int, float),
        "max_workflow_seconds": (int, float),
        "instrumental_gap_min_seconds": (int, float),
        "instrumental_gap_min_ratio_of_median_verse": (int, float),
    }

    for key, expected_type in required.items():
        if key not in config:
            raise RuntimeError(f"Missing config key: {key}")
        if not isinstance(config[key], expected_type):
            raise RuntimeError(f"Bad config key {key}: expected {expected_type}, got {type(config[key]).__name__}")

    config["width"] = int(config["width"])
    config["height"] = int(config["height"])
    config["fps"] = int(config["fps"])
    config["recommended_workflow_seconds"] = float(config["recommended_workflow_seconds"])
    config["max_workflow_seconds"] = float(config["max_workflow_seconds"])
    config["instrumental_gap_min_seconds"] = float(config["instrumental_gap_min_seconds"])
    config["instrumental_gap_min_ratio_of_median_verse"] = float(config["instrumental_gap_min_ratio_of_median_verse"])
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


def build_verses_from_json_words(words: List[Dict[str, Any]], lyrics_verses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    # Use *** words as verse boundaries, then map timings to original lyrics words.
    verse_word_chunks: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    for w in words:
        if "***" in w["text"]:
            if cur:
                verse_word_chunks.append(cur)
                cur = []
            continue
        cur.append(w)
    if cur:
        verse_word_chunks.append(cur)

    verses: List[Dict[str, Any]] = []
    n = min(len(verse_word_chunks), len(lyrics_verses)) if lyrics_verses else len(verse_word_chunks)

    for i in range(n):
        chunk = verse_word_chunks[i]
        ly = lyrics_verses[i] if lyrics_verses else {
            "text": " ".join(w["text"] for w in chunk),
            "lines_text": [" ".join(w["text"] for w in chunk)],
            "bracket_directives": [],
        }

        # Sequentially allocate word timings to original lyric lines.
        cursor = 0
        out_lines: List[Dict[str, Any]] = []
        for li, line_text in enumerate(ly["lines_text"], 1):
            expected_words = lyric_words(line_text)
            line_words: List[Dict[str, Any]] = []
            for ew in expected_words:
                if cursor >= len(chunk):
                    break
                aw = chunk[cursor]
                cursor += 1
                line_words.append({
                    "text": ew,
                    "aligned_text": aw["text"],
                    "start": aw["start"],
                    "end": aw["end"],
                    "probability": aw.get("probability"),
                })
            if line_words:
                start = line_words[0]["start"]
                end = line_words[-1]["end"]
            elif out_lines:
                start = out_lines[-1]["end"]
                end = start + 0.25
            elif chunk:
                start = chunk[0]["start"]
                end = start + 0.25
            else:
                start = end = 0.0

            out_lines.append({
                "index": li,
                "text": line_text,
                "start": start,
                "end": end,
                "words": line_words,
            })

        start = out_lines[0]["start"] if out_lines else (chunk[0]["start"] if chunk else 0)
        end = out_lines[-1]["end"] if out_lines else (chunk[-1]["end"] if chunk else start)
        if chunk:
            end = max(end, chunk[-1]["end"])

        verses.append({
            "index": i + 1,
            "start": start,
            "end": end,
            "duration": max(0.01, end - start),
            "text": ly["text"],
            "lines": out_lines,
            "alignment_mode": "word_json",
            "bracket_directives": list(ly.get("bracket_directives", [])),
        })

    return verses


def parse_alignment(input_dir: Path, debug_dir: Path) -> Tuple[List[Dict[str, Any]], str]:
    lyrics_text = read_text(input_dir / "lyrics.txt", required=False)
    lyrics_verses = parse_lyrics_txt(lyrics_text) if lyrics_text else []

    json_path = input_dir / "alignment.json"
    lrc_path = input_dir / "alignment.lrc"

    if json_path.exists():
        data = load_json(json_path)
        words = extract_json_words(data)
        if not words:
            raise RuntimeError(f"No word timestamps found in {json_path}")

        if not lyrics_verses:
            lyrics_verses = parse_alignment_top_text_as_lyrics(data)
            if lyrics_verses:
                log("[stage] WARNING: no lyrics.txt; using flattened alignment.json text split by ***")
            else:
                log("[stage] WARNING: no lyrics.txt and no usable top-level alignment text; using word chunks only")

        verses = build_verses_from_json_words(words, lyrics_verses)
        write_json(debug_dir / "json_words.json", words[:200])
        return verses, "json"

    if lrc_path.exists():
        lrc_lines = parse_lrc(lrc_path)
        if not lrc_lines:
            raise RuntimeError(f"No LRC lines found in {lrc_path}")
        verses = build_verses_from_lrc(lrc_lines)
        if lyrics_verses and len(lyrics_verses) == len(verses):
            for verse, lyric_verse in zip(verses, lyrics_verses):
                verse["bracket_directives"] = list(lyric_verse.get("bracket_directives", []))
        else:
            for verse in verses:
                verse.setdefault("bracket_directives", [])
        return verses, "lrc"

    raise FileNotFoundError(f"No alignment found. Put {input_dir / 'alignment.json'} or {input_dir / 'alignment.lrc'}")


def detect_audio(input_dir: Path) -> Tuple[str, Path, Optional[Path]]:
    vocals = input_dir / "vocals.mp3"
    instrumental = input_dir / "instrumental.mp3"
    full = input_dir / "audio.mp3"

    if vocals.exists() and instrumental.exists():
        return "stems", vocals, instrumental
    if full.exists():
        return "full", full, None

    # Accept common wav/m4a fallbacks.
    for ext in (".wav", ".m4a", ".flac"):
        vocals2 = input_dir / f"vocals{ext}"
        instrumental2 = input_dir / f"instrumental{ext}"
        full2 = input_dir / f"audio{ext}"
        if vocals2.exists() and instrumental2.exists():
            return "stems", vocals2, instrumental2
        if full2.exists():
            return "full", full2, None

    raise FileNotFoundError("No audio found. Use input/audio.mp3 or input/vocals.mp3 + input/instrumental.mp3")


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


def build_char_karaoke_text(text: str, total_duration: float) -> str:
    """Build char-level ASS karaoke tags over a known time range.

    Letters and digits receive timing tags. Spaces and punctuation are kept in
    the text but do not receive their own duration; they attach visually to the
    surrounding timed characters.
    """
    escaped = ass_escape(text)
    timed_indices = [i for i, ch in enumerate(escaped) if is_karaoke_timed_char(ch)]

    if not timed_indices:
        return r"{\k" + str(centiseconds(max(0.1, total_duration))) + "}" + escaped

    durations = split_centiseconds_evenly(centiseconds(max(0.1, total_duration)), len(timed_indices))
    duration_by_index = dict(zip(timed_indices, durations))

    out: List[str] = []
    for i, ch in enumerate(escaped):
        if i in duration_by_index:
            out.append(r"{\k" + str(duration_by_index[i]) + "}" + ch)
        else:
            out.append(ch)

    return "".join(out)


def build_word_karaoke_line(line: Dict[str, Any], shift: float) -> str:
    pieces: List[str] = []

    for w in line.get("words", []):
        duration = max(0.05, float(w["end"]) - float(w["start"]))
        pieces.append(build_char_karaoke_text(str(w["text"]), duration))

    if pieces:
        return " ".join(pieces)

    line_duration = max(0.1, float(line["end"]) - float(line["start"]))
    return build_char_karaoke_text(str(line["text"]), line_duration)



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
) -> None:
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
    for block in blocks:
        if block.get("kind") == "verse":
            verse_to_block[int(block["verse_index"])] = int(block["block_index"])

    events: List[str] = []

    for verse in verses:
        verse_index = int(verse.get("index", 0))
        block_index = verse_to_block.get(verse_index, verse_index)
        styles = style_map.get(block_index, {"line": "default_line"})

        for line in verse["lines"]:
            start = max(0.0, float(line["start"]) - shift)
            end = max(start + 0.1, float(line["end"]) - shift)
            style = styles["line"]

            if mode == "word" and line.get("words"):
                text = build_word_karaoke_line(line, shift)
            else:
                duration = max(0.1, end - start)
                text = build_char_karaoke_text(str(line["text"]), duration)

            events.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},{style},,0,0,0,,{text}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


def ffmpeg_sub_path(path: Path) -> str:
    return path.resolve().as_posix().replace(":", r"\:").replace("'", r"\'")


def trim_or_pad_video(video_in: Path, duration: float, video_out: Path, ffmpeg: str) -> None:
    video_out.parent.mkdir(parents=True, exist_ok=True)
    # Generated videos are usually slightly longer because frame counts are rounded.
    # setpts normalizes timestamps; tpad covers rare too-short clips.
    run_cmd([
        ffmpeg, "-y",
        "-i", str(video_in),
        "-vf", f"tpad=stop_mode=clone:stop_duration=2,trim=duration={duration:.3f},setpts=PTS-STARTPTS",
        "-an",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        str(video_out),
    ])


def concat_videos(clips: List[Path], out: Path, ffmpeg: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    concat_txt = out.parent / "concat.txt"
    concat_txt.write_text("\n".join(f"file '{p.as_posix()}'" for p in clips), encoding="utf-8")
    run_cmd([
        ffmpeg, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_txt),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-an",
        str(out),
    ])


def final_mux(video_in: Path, audio_in: Path, ass_path: Path, out: Path, ffmpeg: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    sub_arg = ffmpeg_sub_path(ass_path)
    run_cmd([
        ffmpeg, "-y",
        "-i", str(video_in),
        "-i", str(audio_in),
        "-vf", f"subtitles='{sub_arg}'",
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
) -> Dict[str, str]:
    plans_dir.mkdir(parents=True, exist_ok=True)
    raw_path = plans_dir / f"plan_{index:03d}_raw.json"
    clean_path = plans_dir / f"plan_{index:03d}.json"
    request_path = plans_dir / f"plan_{index:03d}_request.txt"
    request_json_path = plans_dir / f"plan_{index:03d}_request.json"
    template_debug_path = plans_dir / f"plan_{index:03d}_template.txt"

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
    check_history_status(h, plans_dir / f"planner_history_{index:03d}.json")

    if not raw_path.exists():
        raise RuntimeError(f"Planner did not write plan file: {raw_path}")

    raw_text = raw_path.read_text(encoding="utf-8").strip()
    plan = extract_json_object(raw_text)
    required = ["scene_summary", "image_prompt", "video_prompt", "negative_prompt"]
    missing = [k for k in required if not str(plan.get(k, "")).strip()]
    if missing:
        raise RuntimeError(f"Planner JSON missing keys: {missing}. Raw response saved to {raw_path}")

    guard = "No visible text, no letters, no captions, no subtitles, no signs, no calligraphy, no logo, no watermark."
    plan["image_prompt"] = f"{str(plan['image_prompt']).strip()} {guard}"
    plan["video_prompt"] = f"{str(plan['video_prompt']).strip()} {guard}"
    plan["negative_prompt"] = (
        f"{str(plan['negative_prompt']).strip()}, text, letters, readable words, captions, subtitles, "
        "signs, calligraphy, title card, logo, watermark, manuscript page, poster layout"
    )
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


def build_local_context(verses: List[Dict[str, Any]], verse_index: int, radius: int = LOCAL_CONTEXT_RADIUS) -> str:
    if verse_index <= 0:
        early = verses[:max(1, radius + 1)]
        return "Intro local context: early song setup and first verses.\n" + "\n\n".join(
            format_verse_context(v, "Verse") for v in early
        )

    total = len(verses)
    if verse_index > total:
        late = verses[max(0, total - radius - 1):]
        return "Outro/instrumental local context: final song resolution and nearby verses.\n" + "\n\n".join(
            format_verse_context(v, "Verse") for v in late
        )

    pos = verse_index - 1
    start = max(0, pos - radius)
    end = min(total, pos + radius + 1)
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



def patch_video_workflow(
    template: Dict[str, Any],
    visual_plan: Dict[str, str],
    duration: float,
    index: int,
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
            f"Block {index:03d} duration is {duration:.2f}s, but this video workflow hard-limits at {max_seconds:.2f}s."
        )
    if duration > recommended_seconds:
        log(
            f"  [warn] block {index:03d} duration is {duration:.2f}s; "
            f"workflow is optimized for <= {recommended_seconds:.2f}s and quality may degrade."
        )

    segment_dir = comfy_segment_subdir(run_id, index)

    wf[VIDEO_N["image_prompt"]]["inputs"]["text"] = visual_plan["image_prompt"]
    wf[VIDEO_N["image_latent"]]["inputs"]["width"] = width
    wf[VIDEO_N["image_latent"]]["inputs"]["height"] = height
    wf[VIDEO_N["image_scheduler"]]["inputs"]["width"] = width
    wf[VIDEO_N["image_scheduler"]]["inputs"]["height"] = height
    wf[VIDEO_N["image_noise"]]["inputs"]["noise_seed"] = int(seeds["image_seed"])
    wf[VIDEO_N["image_save"]]["inputs"]["filename_prefix"] = f"{segment_dir}/start_image"

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


def make_timeline_blocks(
    all_verses: List[Dict[str, Any]],
    selected_verses: List[Dict[str, Any]],
    audio_duration: float,
    has_limit: bool,
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Optional[float]]:
    """Create continuous video blocks.

    Normal blocks:
      intro, verse, instrumental, outro

    Instrumental blocks are created for long gaps without lyrics. Short gaps
    remain attached to the previous verse block.
    """
    total = len(all_verses)
    selected_count = len(selected_verses)
    full_song = selected_count >= total and not has_limit

    # If --limit N where N == total, treat as full song equivalent.
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
    threshold = instrumental_gap_threshold(all_verses, config)

    first = selected_verses[0]
    first_start = float(first["start"])

    if first_start > 0.05:
        intro_text = (
            "Opening instrumental/intro block for the whole song. "
            "Establish the main setting, recurring characters and mood before the first lyric starts."
        )
        blocks.append({
            "block_index": next_block_index,
            "kind": "intro",
            "verse_index": 0,
            "start": 0.0,
            "end": first_start,
            "duration": first_start,
            "text": intro_text + "\n\nFirst verse context:\n" + first.get("text", ""),
            "bracket_directives": [],
        })
        next_block_index += 1

    for pos, verse in enumerate(selected_verses):
        verse_index = int(verse["index"])
        start = float(verse["start"])
        lyric_end = float(verse["end"])

        if pos + 1 < len(selected_verses):
            next_start = float(selected_verses[pos + 1]["start"])
            next_verse_index: Optional[int] = int(selected_verses[pos + 1]["index"])
        elif full_song:
            next_start = lyric_end
            next_verse_index = None
        else:
            next_start = timeline_end
            next_verse_index = int(all_verses[selected_count]["index"]) if selected_count < total else None

        gap = max(0.0, next_start - lyric_end)
        split_gap = gap >= threshold and next_start > lyric_end

        verse_end = lyric_end if split_gap else next_start
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
            "bracket_directives": list(verse.get("bracket_directives", [])),
        })
        next_block_index += 1

        if split_gap:
            blocks.append({
                "block_index": next_block_index,
                "kind": "instrumental",
                "verse_index": verse_index,
                "previous_verse_index": verse_index,
                "next_verse_index": next_verse_index,
                "start": lyric_end,
                "end": next_start,
                "duration": max(0.01, next_start - lyric_end),
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
        if outro_end - outro_start > 0.05:
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
                "duration": max(0.01, outro_end - outro_start),
                "text": outro_text + "\n\nLast verse context:\n" + last.get("text", ""),
                "bracket_directives": [],
            })

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
    total_lyric = 0
    total_aligned = 0
    warnings = 0

    for verse in verses:
        lyric_count = 0
        aligned_count = 0
        mismatch_count = 0
        low_prob_count = 0

        for line in verse.get("lines", []):
            for w in line.get("words", []):
                lyric_count += 1
                if w.get("aligned_text") is not None:
                    aligned_count += 1
                if norm_word(str(w.get("text", ""))) != norm_word(str(w.get("aligned_text", ""))):
                    mismatch_count += 1
                prob = w.get("probability")
                if isinstance(prob, (int, float)) and prob < 0.15:
                    low_prob_count += 1

        total_lyric += lyric_count
        total_aligned += aligned_count

        status = "OK"
        notes: List[str] = []
        if mismatch_count:
            status = "WARN"
            warnings += 1
            notes.append(f"mismatches={mismatch_count}")
        if low_prob_count:
            status = "WARN"
            warnings += 1
            notes.append(f"low_probability={low_prob_count}")
        if not lyric_count and verse.get("alignment_mode") == "word_json":
            status = "WARN"
            warnings += 1
            notes.append("no_words")

        lines.append(
            f"verse {int(verse.get('index', 0)):03d}: {status}; "
            f"duration={float(verse.get('duration', 0)):.2f}s; "
            f"words={lyric_count}; " + (", ".join(notes) if notes else "clean")
        )

    lines.insert(0, f"total lyric words : {total_lyric}")
    lines.insert(1, f"total aligned words: {total_aligned}")
    lines.insert(2, f"warning verses     : {warnings}")

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
        "final": str(final_path.relative_to(output_root)) if final_path.is_relative_to(output_root) else str(final_path),
        "blocks": items,
    }
    write_json(out_path, manifest)

def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a ComfyUI music video from input-dir files.")
    ap.add_argument("--input-dir", default="input", help="Folder containing all song input files. Defaults to ./input.")
    ap.add_argument("--output-dir", default="output", help="Folder for all generated artifacts. Defaults to ./output.")
    ap.add_argument("--limit", type=int, default=0, help="Use only first N verses/clips for testing.")
    ap.add_argument("--rework", nargs="*", type=int, default=None, help="Generate only these block numbers; reuse existing clips for other selected blocks. 0=intro, 1..N=verses, N+1=outro.")
    ap.add_argument("--rebuild-final", action="store_true", help="Do not generate video; reuse existing clips and rebuild concat/final only.")
    ap.add_argument("--comfy-url", default=COMFY_URL)
    ap.add_argument("--comfy-output-dir", default=str(COMFY_OUTPUT_DIR))
    ap.add_argument("--ffmpeg", default=FFMPEG)
    ap.add_argument("--ffprobe", default=FFPROBE)
    args = ap.parse_args()

    stats: Dict[str, float] = {"_run_start": time.perf_counter()}
    clips_generated = 0
    clips_reused = 0
    run_id = make_run_id()
    generation_info: Dict[int, Dict[str, Any]] = {}
    fresh_generation_run = not args.rebuild_final and not args.rework
    rework_indices = set(args.rework or [])

    script_dir = Path(__file__).resolve().parent
    input_dir = Path(args.input_dir).resolve()
    output_root = Path(args.output_dir).resolve()
    out_dir = output_root / "work"
    debug_dir = out_dir / "debug"
    output_dir = Path(args.comfy_output_dir)
    workflow_dir = script_dir / "workflows"
    rules_dir = script_dir / "rules"
    data_dir = script_dir / "data"

    log(f"[stage] input dir : {input_dir}")
    log(f"[stage] output dir: {output_root}")
    log(f"[stage] workflows : {workflow_dir}")
    log(f"[stage] run id    : {run_id}")
    log(f"[stage] rules     : {rules_dir}")
    log(f"[stage] data      : {data_dir}")

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
    write_json(debug_dir / "config_used.json", config)
    block_video_styles, video_style_report = load_block_video_styles(input_dir, video_style, debug_dir)

    planner_template = load_json(workflow_dir / "planner_visual_prompts_api.json")
    video_template = load_json(workflow_dir / "video_from_generated_image_api.json")
    width, height = int(config["width"]), int(config["height"])

    log("[stage] parse alignment")
    stats_start(stats, "parse_alignment")
    verses, alignment_mode = parse_alignment(input_dir, debug_dir)
    if not verses:
        raise RuntimeError("No verses parsed from alignment.")
    write_json(debug_dir / "parsed_verses_all.json", verses)
    write_alignment_match_report(verses, debug_dir / "alignment_match_report.txt")
    stats_end(stats, "parse_alignment")

    plans_dir = out_dir / "plans"
    if args.rebuild_final:
        song_context_mode = "skip"
    elif rework_indices:
        song_context_mode = "frozen"
    else:
        song_context_mode = "fresh"

    stats_start(stats, "song_context")
    song_context = run_or_load_song_context(
        planner_template,
        rules,
        video_style,
        verses,
        args.comfy_url,
        plans_dir,
        song_context_mode,
    )
    write_json(debug_dir / "song_context_used.json", {
        "mode": song_context_mode,
        "context": song_context,
    })
    stats_end(stats, "song_context")

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
    full_mix = prepare_audio_full_mix(audio_mode, audio_a, audio_b, out_dir / "audio", args.ffmpeg)
    audio_duration = get_audio_duration(full_mix, args.ffprobe)
    log(f"[stage] full audio duration={audio_duration:.2f}s")
    stats_end(stats, "prepare_audio")

    stats_start(stats, "timeline")
    blocks, audio_end = make_timeline_blocks(verses, selected, audio_duration, has_effective_limit, config)
    if not blocks:
        raise RuntimeError("No timeline blocks created.")
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
    if has_effective_limit:
        log(f"[stage] limit mode: audio/video ends at start of verse {len(selected) + 1:03d}")
    else:
        log("[stage] full-song mode: audio is not cut by verse boundaries")

    if args.rebuild_final:
        log("[stage] rebuild-final mode: no video generation, existing clips only")
    elif rework_indices:
        log(f"[stage] rework mode: regenerate blocks {sorted(rework_indices)} and reuse the rest")
    else:
        log("[stage] full generation mode for selected timeline blocks")

    write_json(debug_dir / "timeline_blocks.json", blocks)

    log("[stage] render audio for timeline")
    stats_start(stats, "render_audio")
    final_audio = render_audio_for_timeline(full_mix, out_dir / "audio", args.ffmpeg, audio_end)
    render_duration = ffprobe_duration(final_audio, args.ffprobe)
    log(f"[stage] render audio duration={render_duration:.2f}s")
    stats_end(stats, "render_audio")

    log("[stage] build karaoke subtitles")
    stats_start(stats, "subtitles")
    subtitle_mode = "word" if alignment_mode == "json" else "line"
    ass_path = out_dir / "subs" / "karaoke.ass"
    style_section, subtitle_style_map, subtitle_style_report = build_subtitle_styles_for_blocks(
        blocks,
        input_dir,
        data_dir,
        debug_dir,
    )
    # Timeline always starts at 0 now, so subtitles keep real timestamps.
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
    )
    log(f"[stage] subtitles: {ass_path} ({subtitle_mode})")
    stats_end(stats, "subtitles")

    clips: List[Path] = []
    clips_dir = out_dir / "clips"
    raw_dir = out_dir / "clips_raw"
    debug_dir.mkdir(parents=True, exist_ok=True)
    write_json(debug_dir / "run_info.json", {"run_id": run_id, "fresh_generation_run": fresh_generation_run})
    clips_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    for block in blocks:
        block_i = int(block["block_index"])
        kind = str(block["kind"])
        duration = max(0.1, float(block["duration"]))
        first_line = str(block.get("text", "")).splitlines()[0] if str(block.get("text", "")).splitlines() else ""
        first = first_line[:100]
        clip_local = block_clip_path(clips_dir, block)

        if args.rebuild_final:
            should_generate = False
        elif rework_indices:
            should_generate = block_i in rework_indices
        else:
            should_generate = True

        log(f"\n=== block {block_i:03d} / {kind}: {first}")
        log(f"  [stage] time={float(block['start']):.3f}s..{float(block['end']):.3f}s duration={duration:.2f}s")

        if not should_generate:
            if not clip_local.exists():
                raise FileNotFoundError(
                    f"Existing clip required but not found: {clip_local}\n"
                    "Run full generation first, or include this block in --rework."
                )
            log(f"  [stage] reuse existing clip: {clip_local}")
            generation_info[block_i] = {
                "run_id": run_id,
                "segment_subdir": None,
                "seeds": None,
                "generated_in_this_run": False,
            }
            clips_reused += 1
            clips.append(clip_local)
            continue

        stats_start(stats, "video_generation")
        continuity_before = block_i if kind == "verse" else (1 if kind == "intro" else total_verses + 1)
        continuity = load_continuity_from_plans(plans_dir, block_i)
        if kind == "instrumental":
            local_context = build_instrumental_local_context(
                verses,
                int(block.get("previous_verse_index", 0)),
                block.get("next_verse_index"),
            )
        else:
            local_context = build_local_context(verses, int(block.get("verse_index", block_i)), LOCAL_CONTEXT_RADIUS)
        current_instruction = build_current_block_instruction(block, verses)
        block_video_style = effective_video_style(block_i, video_style, block_video_styles)
        write_json(debug_dir / f"planner_context_{block_i:03d}.json", {
            "block_index": block_i,
            "block_kind": kind,
            "video_style_source": video_style_report.get("blocks", {}).get(str(block_i), video_style_report.get("default", {})),
            "video_style": block_video_style,
            "song_context": song_context,
            "local_context": local_context,
            "current_block": current_instruction,
            "continuity": continuity[-5:],
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
            args.comfy_url,
            plans_dir,
            continuity,
        )

        seeds = {
            "image_seed": random_seed(),
            "video_seed": random_seed(),
            "video_refine_seed": random_seed(),
        }
        segment_subdir = comfy_segment_subdir(run_id, block_i)
        generation_info[block_i] = {
            "run_id": run_id,
            "segment_subdir": segment_subdir.replace("\\", "/"),
            "seeds": seeds,
            "generated_in_this_run": True,
        }

        log("  [stage] patch video workflow")
        log(f"  [seeds] image={seeds['image_seed']} video={seeds['video_seed']} refine={seeds['video_refine_seed']}")
        vwf = patch_video_workflow(video_template, plan, duration, block_i, run_id, seeds, config)
        write_json(debug_dir / f"video_patched_{block_i:03d}.json", vwf)
        write_json(debug_dir / f"video_generation_{block_i:03d}.json", generation_info[block_i])

        log("  [stage] queue video")
        pid, client_id = queue_prompt(vwf, args.comfy_url)
        log(f"  [video] prompt_id={pid}")
        vh = wait_history(pid, args.comfy_url, vwf, client_id)
        check_history_status(vh, debug_dir / f"video_history_{block_i:03d}.json")
        video_path = find_result_file(vh, output_dir, segment_subdir, "video", {".mp4", ".mov", ".webm", ".mkv"})
        if not video_path:
            raise RuntimeError(f"Video result not found for block {block_i:03d}")

        raw_local = raw_dir / f"{clip_local.stem}{video_path.suffix}"
        shutil.copy2(video_path, raw_local)

        log("  [stage] trim/pad video to exact block duration; remove raw workflow audio")
        trim_or_pad_video(raw_local, duration, clip_local, args.ffmpeg)
        clips_generated += 1
        stats_end(stats, "video_generation")
        clips.append(clip_local)

    log("\n[stage] concat video clips")
    stats_start(stats, "concat")
    video_only = out_dir / "video" / "video_only.mp4"
    concat_videos(clips, video_only, args.ffmpeg)
    stats_end(stats, "concat")

    log("[stage] final mux audio + burn subtitles")
    stats_start(stats, "final_mux")
    final = output_root / "final_video.mp4"
    final_mux(video_only, final_audio, ass_path, final, args.ffmpeg)
    stats_end(stats, "final_mux")

    write_timeline_manifest(
        output_root / "manifest.json",
        output_root,
        blocks,
        clips,
        final_audio,
        ass_path,
        final,
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
