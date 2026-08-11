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
from datetime import datetime
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

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


def wall_clock_ms() -> str:
    now = time.time()
    return time.strftime("%H:%M:%S", time.localtime(now)) + f".{int((now % 1.0) * 1000):03d}"


def fmt_elapsed(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 100.0:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def log_comfy(msg: str, workflow_start: Optional[float] = None, node_start: Optional[float] = None) -> None:
    parts = []
    now = time.perf_counter()
    if workflow_start is not None:
        parts.append(f"t+{fmt_elapsed(now - workflow_start)}")
    if node_start is not None:
        parts.append(f"node+{fmt_elapsed(now - node_start)}")
    if parts:
        log(f"[comfy] {' '.join(parts)} | {msg}")
    else:
        log(f"[comfy] {msg}")


def clean_fresh_output_dir(output_root: Path) -> None:
    """Remove previous generated artifacts for a manual clean run helper."""
    if output_root.exists():
        shutil.rmtree(output_root)
    output_root.mkdir(parents=True, exist_ok=True)


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

    Alignment is a lazy artifact: --refresh-alignment invalidates it, and this
    function recreates it only when the timeline/subtitles stage needs it.
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




def query_vram_mb() -> Optional[Tuple[int, int]]:
    """Return (used_mb, total_mb) for the first NVIDIA GPU, or None."""
    try:
        cp = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=True,
        )
    except Exception:
        return None

    for line in cp.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 2:
            try:
                return int(float(parts[0])), int(float(parts[1]))
            except ValueError:
                continue
    return None


def format_vram(vram: Optional[Tuple[int, int]]) -> str:
    if vram is None:
        return "vram unavailable"
    used_mb, total_mb = vram
    return f"{used_mb} / {total_mb}"


def wait_after_free_memory(wait_seconds: float, poll_interval: float = 0.5, report_vram: bool = True) -> None:
    wait_seconds = max(0.0, float(wait_seconds))
    poll_interval = max(0.1, float(poll_interval))
    if wait_seconds <= 0.0:
        return

    elapsed = 0.0
    while elapsed + 1e-9 < wait_seconds:
        step = min(poll_interval, wait_seconds - elapsed)
        time.sleep(step)
        elapsed += step
        if report_vram:
            vram = query_vram_mb()
            if vram is not None:
                log_comfy(f"waiting {elapsed:.1f}s: {format_vram(vram)}")


def free_comfy_memory(comfy_url: str, reason: str = "", sleep_time: Optional[float] = None) -> None:
    """Best-effort ComfyUI VRAM/cache cleanup.

    This only asks the ComfyUI server process to unload models/free memory.
    It is intentionally non-fatal: unsupported endpoints or transient errors
    should not stop generation.
    When sleep_time is provided, wait that many seconds after a successful
    free-memory request and log VRAM during that wait.
    """
    payload = {"unload_models": True, "free_memory": True}
    base = comfy_url.rstrip("/")
    label = f" ({reason})" if reason else ""
    before_vram = query_vram_mb()
    report_wait_vram = before_vram is not None

    for path in ("/free", "/api/free"):
        url = base + path
        try:
            r = requests.post(url, json=payload, timeout=60)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            log_comfy(f"free memory {format_vram(before_vram)} ok{label}: {path}")
            if sleep_time is not None:
                wait_after_free_memory(float(sleep_time), poll_interval=0.5, report_vram=report_wait_vram)
            return
        except Exception as exc:
            log_comfy(f"free memory {format_vram(before_vram)} failed{label}: {path}: {exc}")
    log_comfy(f"free memory {format_vram(before_vram)} failed/non-fatal{label}")

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

    # Keep progress logs compact: node id + visible title/name only.
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
    current_node_start: Optional[float] = None
    node_start_by_id: Dict[str, float] = {}
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
                        log_comfy(f"execution started prompt_id={prompt_id}", workflow_start=start)

                    elif msg_type == "executing":
                        node = data.get("node")
                        if node is None:
                            # ComfyUI commonly sends node=None when the prompt is done.
                            finished_by_ws = True
                            break

                        current_node = str(node)
                        current_node_start = time.perf_counter()
                        node_start_by_id[current_node] = current_node_start
                        current_progress = ""
                        node_label = workflow_node_label(workflow, current_node)
                        if node_label != last_node_label:
                            last_node_label = node_label
                            log_comfy(f"node: {node_label}", workflow_start=start)

                    elif msg_type == "progress":
                        current_progress = format_progress(data.get("value"), data.get("max"))

                    elif msg_type == "executed":
                        node = data.get("node")
                        if node is not None:
                            node_id = str(node)
                            log_comfy(
                                f"executed: {workflow_node_label(workflow, node_id)}",
                                workflow_start=start,
                                node_start=node_start_by_id.get(node_id),
                            )

                    elif msg_type == "execution_error":
                        node = data.get("node_id") or data.get("node")
                        message = data.get("exception_message") or data.get("message") or ""
                        log_comfy(
                            f"execution error at {workflow_node_label(workflow, str(node) if node is not None else None)}: {message}",
                            workflow_start=start,
                            node_start=current_node_start,
                        )
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
                    log_comfy(f"running... | {node_label} | progress {current_progress}", workflow_start=start, node_start=current_node_start)
                else:
                    log_comfy(f"running... | {node_label}", workflow_start=start, node_start=current_node_start)

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
                log_comfy(f"finished after {fmt_elapsed(elapsed)}", workflow_start=start)
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




LYRICS_SEPARATOR_SPLIT_RE = re.compile(
    r"(?m)(^[ \t]*(?:\*{3}|-{3})(?:[ \t]+(?:@|#)[ \t]+(?:\d+:)?\d+:\d{2}\.\d{3})?[ \t]*$)"
)
LYRICS_SEPARATOR_RE = re.compile(
    r"^(?P<separator>\*{3}|-{3})(?:\s+(?P<mode>@|#)\s+"
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
    return {
        "separator": match.group("separator"),
        "mode": "exact" if mode_token == "@" else ("snap_previous" if mode_token == "#" else "automatic"),
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
    lines = block.get("lines_text")
    if isinstance(lines, list):
        return any(str(line).strip() for line in lines)
    lines = block.get("lines")
    if isinstance(lines, list):
        return any(str(line.get("text", "")).strip() if isinstance(line, dict) else str(line).strip() for line in lines)
    return bool(str(block.get("text", "")).strip())

def expected_words_for_lyrics_verses(lyrics_verses: List[Dict[str, Any]]) -> List[List[str]]:
    return [lyric_words(str(v.get("text", ""))) if block_has_lyric_text(v) else [] for v in lyrics_verses]


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

        collapsed = (
            expected_count >= 2
            and (
                duration < min_plausible_duration
                or words_per_second > 14.0
                or (zeroish_ratio >= 0.65 and duration < max(2.00, expected_count * 0.35))
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
        "words_per_second": words_per_second,
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
        ws = float(line["start"]) + slot * i
        we = float(line["start"]) + slot * (i + 1)
        w["start"] = ws
        w["end"] = max(ws + 0.001, we)
        if estimated:
            w["timing_estimated"] = True
            w["timing_source"] = "estimated_line_window"


def estimate_unreliable_line_timings(lines: List[Dict[str, Any]], config: Dict[str, Any]) -> None:
    if not lines:
        return

    i = 0
    while i < len(lines):
        if lines[i].get("timing_reliable", False):
            i += 1
            continue

        run_start = i
        while i < len(lines) and not lines[i].get("timing_reliable", False):
            i += 1
        run_end = i

        prev_line = lines[run_start - 1] if run_start > 0 else None
        next_line = lines[run_end] if run_end < len(lines) else None

        word_counts = [max(1, int(lines[j].get("diagnostics", {}).get("expected_words", 0))) for j in range(run_start, run_end)]
        total_weight = max(1, sum(word_counts))

        if prev_line is not None and next_line is not None:
            start = float(prev_line["end"])
            end = float(next_line["start"])
            if end <= start + 0.05:
                # No usable gap: keep a very small monotonic window rather than
                # damaging the neighboring reliable anchors.
                end = start + max(0.05 * total_weight, 0.10)
        elif prev_line is not None:
            start = float(prev_line["end"])
            estimated_duration = max(0.25 * total_weight, 0.50)
            raw_end = max(float(lines[j].get("end", start)) for j in range(run_start, run_end))
            end = max(start + estimated_duration, raw_end)
        elif next_line is not None:
            end = float(next_line["start"])
            estimated_duration = max(0.25 * total_weight, 0.50)
            raw_start = min(float(lines[j].get("start", end)) for j in range(run_start, run_end))
            start = min(raw_start, max(0.0, end - estimated_duration))
        else:
            start = min(float(lines[j].get("start", 0.0)) for j in range(run_start, run_end))
            end = max(float(lines[j].get("end", start + 0.01)) for j in range(run_start, run_end))
            if end <= start + 0.05:
                end = start + max(0.25 * total_weight, 0.50)

        cursor = start
        available = max(0.01, end - start)
        for j, weight in zip(range(run_start, run_end), word_counts):
            part = available * (weight / total_weight)
            line_start = cursor
            line_end = end if j == run_end - 1 else cursor + part
            redistribute_line_word_timings(lines[j], line_start, line_end, estimated=True)
            lines[j]["timing_reliable"] = False
            lines[j]["timing_estimated"] = True
            lines[j].setdefault("diagnostics", {}).setdefault("issues", []).append("timing estimated from neighboring reliable lines")
            cursor = line_end



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
        should_repair = (
            gap >= far_gap_seconds
            and duration <= zeroish_seconds
            and (confidence_bad or confidence_unknown_but_gap_extreme)
        )

        if should_repair:
            new_start = previous_end
            new_duration = estimate_sanitized_word_duration(str(word.get("text", "")), gap)
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
    clean_words: List[Dict[str, Any]] = []
    for w in words:
        if is_alignment_meta_token(str(w.get("text", ""))):
            ignored_meta_words.append(w)
        else:
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
            "boundary_reason": "line_aware_expected_exhausted",
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

            matched_words, cursor, line_report = match_lyrics_line_words(
                expected_line_words,
                clean_words,
                cursor,
                next_expected,
                config,
                allow_long_start_gap=(li == 1),
            )

            sanitizer_report = sanitize_matched_line_word_timings(line_text, matched_words)
            if out_lines:
                default_line_start = float(out_lines[-1]["end"])
            else:
                default_line_start = 0.0
            line_start, line_end = line_timing_bounds_from_words(matched_words, default_line_start)

            diag = analyze_matched_line_timing(line_text, matched_words, config)
            if sanitizer_report.get("repaired_words"):
                diag.setdefault("issues", []).append(
                    f"sanitized word timings: {len(sanitizer_report.get('repaired_words', []))}"
                )
                diag["timing_sanitizer"] = sanitizer_report
            line = {
                "index": li,
                "text": line_text,
                "start": line_start,
                "end": max(line_start + 0.01, line_end),
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

    # Final global timing estimation across range boundaries. A whole range can
    # be missing/collapsed while the next range has a reliable anchor; estimating
    # only inside each range would leave such ranges near-zero. This pass keeps
    # the full song timeline monotonic and distributes unreliable lyric lines
    # between neighboring reliable anchors.
    all_lines: List[Dict[str, Any]] = []
    for verse in verses:
        all_lines.extend(verse.get("lines", []) or [])
    estimate_unreliable_line_timings(all_lines, config)

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


def split_matched_words_into_lines(
    ly: Dict[str, Any],
    matched_words: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    # Kept for compatibility with non-JSON fallback helpers. JSON alignment now
    # uses build_line_aware_verses_from_json_words().
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
        "llm_max_ctx": int,
        "llm_max_length": int,
    }

    for key, expected_type in required.items():
        if key not in config:
            raise RuntimeError(f"Missing config key: {key}")
        if not isinstance(config[key], expected_type):
            raise RuntimeError(f"Bad config key {key}: expected {expected_type}, got {type(config[key]).__name__}")

    config["comfy_url"] = str(config["comfy_url"])
    config["comfy_output_dir"] = str(config["comfy_output_dir"])
    config["video_width"] = int(config["video_width"])
    config["video_height"] = int(config["video_height"])
    config["video_fps"] = int(config["video_fps"])
    config["clip_duration_tolerance_ratio"] = float(config["clip_duration_tolerance_ratio"])
    config["llm_max_ctx"] = max(1024, int(config["llm_max_ctx"]))
    config["llm_max_length"] = max(128, int(config["llm_max_length"]))
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
                    "@/# MM:SS.mmm."
                )
            if is_bracket_directive_line(stripped):
                directive = strip_bracket_directive(stripped)
                if directive:
                    directives.append(directive)
                continue
            lyric_lines.append(stripped)
    finish_block()
    return blocks




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
            "subrange_divider_after_lines": [],
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
            "subrange_divider_after_lines": [],
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
            "subrange_divider_after_lines": [],
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
            "subrange_divider_after_lines": list(ly.get("subrange_divider_after_lines", [])),
            "timed_subrange_boundaries": list(ly.get("timed_subrange_boundaries", [])),
            "semantic_boundary_before": ly.get("semantic_boundary_before"),
            "alignment_match": verse_report,
        })

    # Any remaining actual words are extra. They are not used in subtitles.
    # Final global timing estimation across range boundaries. A whole range can
    # be missing/collapsed while the next range has a reliable anchor; estimating
    # only inside each range would leave such ranges near-zero. This pass keeps
    # the full song timeline monotonic and distributes unreliable lyric lines
    # between neighboring reliable anchors.
    all_lines: List[Dict[str, Any]] = []
    for verse in verses:
        all_lines.extend(verse.get("lines", []) or [])
    estimate_unreliable_line_timings(all_lines, config)

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
    # Matched lyrics/timeline input is a lazy artifact. If it exists, trust it
    # until --refresh-alignment invalidates the alignment directory. This keeps
    # normal rework/rebuild runs from rematching lyrics when neither the raw
    # alignment nor lyrics were intentionally refreshed.
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
        log(f"[stage] ignore stale matched alignment cache: {matched_cache_path}")

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
        ensure_line_level_lrc_from_matched_verses(verses, alignment_dir)
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
        ensure_line_level_lrc_from_matched_verses(verses, alignment_dir)
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
        if block_has_lyric_text(block):
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



def approx_token_count_for_log(text: str) -> int:
    # Rough tokenizer-independent estimate for context diagnostics only.
    return max(1, int(len(text) / 4)) if text else 0


def apply_llm_workflow_config(template: Dict[str, Any], llm_max_ctx: int, llm_max_length: int) -> Dict[str, Any]:
    """Apply LLM runtime settings from config to the ComfyUI LLM workflow template.

    The project uses data/config.json (or input/config.json override) as the
    source of truth. The workflow file can keep default values, but the runner
    patches every node that exposes max_ctx and/or max_length inputs before any
    LLM call. max_ctx controls context window; max_length controls generated
    response length.
    """
    wf = json.loads(json.dumps(template))
    patched_ctx_nodes: List[str] = []
    patched_length_nodes: List[str] = []
    for node_id, node in wf.items():
        inputs = node.get("inputs") if isinstance(node, dict) else None
        if isinstance(inputs, dict):
            if "max_ctx" in inputs:
                inputs["max_ctx"] = int(llm_max_ctx)
                patched_ctx_nodes.append(str(node_id))
            if "max_length" in inputs:
                inputs["max_length"] = int(llm_max_length)
                patched_length_nodes.append(str(node_id))
    if not patched_ctx_nodes:
        raise RuntimeError("LLM workflow config error: no workflow node exposes a max_ctx input")
    if not patched_length_nodes:
        raise RuntimeError("LLM workflow config error: no workflow node exposes a max_length input")
    return wf


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

    log("  [stage] ComfyUI LLM planner")
    free_comfy_memory(comfy_url, "before LLM planner", sleep_time=1.0)
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


def patch_song_context_workflow(
    template: Dict[str, Any],
    rules: Dict[str, str],
    video_style: str,
    verses: List[Dict[str, Any]],
    raw_path: Path,
    request_path: Path,
) -> Dict[str, Any]:
    wf = json.loads(json.dumps(template))
    if "2" not in wf or "3" not in wf:
        raise RuntimeError("Planner workflow must contain nodes 2=LLM_local and 3=PathSaveStringFile")

    prompt = build_song_context_prompt(rules, video_style, verses)
    save_prompt_debug(request_path, prompt)

    wf["2"]["inputs"]["system_prompt"] = rules["song_context_system.txt"]
    wf["2"]["inputs"]["user_prompt"] = prompt
    wf["2"]["inputs"]["historical_record"] = ""
    wf["2"]["inputs"]["conversation_rounds"] = 1
    wf["2"]["inputs"]["is_memory"] = "disable"
    wf["2"]["inputs"]["is_locked"] = "disable"
    wf["2"]["inputs"]["main_brain"] = "enable"
    wf["3"]["inputs"]["path"] = str(raw_path)
    return wf


def get_or_create_song_context(
    planner_template: Dict[str, Any],
    rules: Dict[str, str],
    video_style: str,
    verses: List[Dict[str, Any]],
    comfy_url: str,
    plans_dir: Path,
) -> Dict[str, Any]:
    """Load cached song_context.json or build it when visual generation needs it."""
    plans_dir.mkdir(parents=True, exist_ok=True)
    clean_path = plans_dir / "song_context.json"
    raw_path = plans_dir / "song_context_response.txt"
    response_json_path = plans_dir / "song_context_response.json"
    request_path = plans_dir / "song_context_request.txt"

    if clean_path.exists():
        log(f"[stage] use cached song context: {clean_path}")
        return load_json(clean_path)

    log("[stage] build missing song context")
    if raw_path.exists():
        raw_path.unlink()

    wf = patch_song_context_workflow(
        planner_template,
        rules,
        video_style,
        verses,
        raw_path,
        request_path,
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


def build_current_block_instruction(block: Dict[str, Any], verses: List[Dict[str, Any]]) -> str:
    directives = block.get("bracket_directives") or []
    directive_text = ""
    if directives:
        directive_text = (
            "\n\nBRACKET DIRECTIVES, lower priority than actual lyrics:\n"
            + "\n".join(f"- {x}" for x in directives)
            + "\nThese are songwriter or generation metadata from bracketed lyric lines. "
              "Do not treat them as sung lyrics. If they conflict with actual lyrics, lyrics win."
        )

    if not block_has_lyric_text(block):
        return (
            "This is an explicit non-lyrical song block from lyrics.txt. "
            "It may be instrumental, intro, outro, breakdown, solo, rest, or metadata-only. "
            "No words are sung in this block. Continue or resolve the surrounding visual scene using the music and bracket directives. "
            "Do not render lyrics, captions, signs, section labels, or any visible text."
        ) + directive_text

    return "Current song block text:\n" + str(block.get("text", "")) + directive_text




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
    if descriptor.get("mode") == "snap_previous":
        lookback = max(0.0, float(config.get("manual_boundary_snap_max_seconds", 10.0)))
        valid = sorted({
            float(value) for value in candidates
            if allowed_start < float(value) <= requested + 1e-6
            and requested - float(value) <= lookback + 1e-6
        })
        if valid:
            resolved = valid[-1]
            reason = "previous_lyric_boundary"
        else:
            reason = "no_previous_candidate_within_radius"
            warning = True
            log(
                f"WARNING: {label} {descriptor.get('source')} has no previous lyric boundary within "
                f"{lookback:.3f}s; using exact requested time {requested:.3f}s"
            )

    result = dict(descriptor)
    result.update({
        "resolved_time": resolved,
        "shift_seconds": resolved - requested,
        "resolution_reason": reason,
        "warning": warning,
    })
    if descriptor.get("mode") == "snap_previous" and not warning:
        log(
            f"[boundary] {label}: {requested:.3f}s -> {resolved:.3f}s "
            f"({resolved - requested:+.3f}s, previous lyric boundary)"
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
    width = int(config["video_width"])
    height = int(config["video_height"])
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
    width = int(config["video_width"])
    height = int(config["video_height"])
    fps = float(config["video_fps"])
    recommended_seconds = float(config["recommended_workflow_seconds"])
    max_seconds = float(config["max_workflow_seconds"])
    seconds = max(0.001, float(duration))

    if duration > max_seconds:
        raise RuntimeError(
            f"{format_range_id(block_index)} part {sub_index:03d} duration is {duration:.2f}s, "
            f"but this video workflow hard-limits at {max_seconds:.2f}s."
        )
    if duration > recommended_seconds:
        log(
            f"  [warn] {format_range_id(block_index)} part {sub_index:03d} duration is {duration:.2f}s; "
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

    AI-11 keeps the legacy `kind` field for the master single-pass planner:
    lyric blocks are `verse`; non-lyrical blocks become intro/instrumental/outro
    according to their position.
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
    ap.add_argument("--limit", type=int, default=0, help="Use only first N zero-based ranges for testing/final assembly. Example: --limit 3 selects R000..R002.")
    ap.add_argument("--rework", nargs="*", type=int, default=None, help="Generate only these zero-based range IDs; reuse existing clips for other selected ranges. Use the same RNNN numbers shown in subtitle_preview, without the R prefix.")
    ap.add_argument("--rebuild-final", action="store_true", help="Do not generate video; reuse existing unscaled clips and rebuild scaled clips/final only.")
    ap.add_argument("--refresh-alignment", action="store_true", help="Invalidate alignment/matching/subtitle/preview caches; missing artifacts are recreated lazily.")
    ap.add_argument("--preview-subtitles-only", action="store_true", help="Reuse or lazily build karaoke subtitles and subtitle_preview.mp4, then stop before any ComfyUI LLM/image/video generation.")
    ap.add_argument("--comfy-url", default=None, help="Override comfy_url from config.json.")
    ap.add_argument("--comfy-output-dir", default=None, help="Override comfy_output_dir from config.json.")
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
    comfy_url = args.comfy_url or str(config["comfy_url"])
    output_dir = Path(args.comfy_output_dir).resolve() if args.comfy_output_dir else resolve_config_path(str(config["comfy_output_dir"]), script_dir)
    planner_template = apply_llm_workflow_config(
        load_json(workflow_dir / "planner_visual_prompts_api.json"),
        int(config["llm_max_ctx"]),
        int(config["llm_max_length"]),
    )
    image_template = load_json(workflow_dir / "image_from_prompt_api.json")
    video_template = load_json(workflow_dir / "video_from_image_api.json")
    write_json(debug_dir / "config_used.json", config)
    log(f"[stage] comfy url : {comfy_url}")
    log(f"[stage] comfy out : {output_dir}")
    log(f"[stage] llm max ctx: {int(config['llm_max_ctx'])}")
    log(f"[stage] llm max length: {int(config['llm_max_length'])}")
    block_video_styles, video_style_report = load_block_video_styles(input_dir, video_style, debug_dir)

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
            planner_template,
            rules,
            video_style,
            verses,
            comfy_url,
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
                free_comfy_memory(comfy_url, "before image generation", sleep_time=1.0)
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
                    raise RuntimeError(f"Start image result not found for {format_range_id(block_i)} part {sub_i:03d}")
                shutil.copy2(image_path, start_image_local)
            else:
                if previous_subclip is None:
                    raise RuntimeError(f"Internal error: no previous subclip for {format_range_id(block_i)} part {sub_i:03d}")
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
            free_comfy_memory(comfy_url, "before video generation", sleep_time=1.0)
            pid, client_id = queue_prompt(vwf, comfy_url)
            log(f"  [video] prompt_id={pid}")
            vh = wait_history(pid, comfy_url, vwf, client_id)
            check_history_status(vh, part_debug_dir / "video_history.json")
            video_path = find_result_file(vh, output_dir, sub_dir, "video", {".mp4", ".mov", ".webm", ".mkv"})
            if not video_path:
                raise RuntimeError(f"Video result not found for {format_range_id(block_i)} part {sub_i:03d}")

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
            scene_summary = f"{format_range_id(block_i)}, rendered as {len(subrange_infos)} internal subrange(s)"
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
