"""ComfyUI transport, input upload, progress and result retrieval."""
from __future__ import annotations

import json
import mimetypes
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
import websocket


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

    for path in ("/free",):
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
    else:
        raise ValueError("ComfyUI URL must use HTTP(S)")
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

    return None


def upload_image_to_comfy(image_path: Path, comfy_url: str, subfolder: str) -> str:
    if not image_path.exists():
        raise FileNotFoundError(f"Image to upload not found: {image_path}")

    with image_path.open("rb") as f:
        r = requests.post(
            comfy_url.rstrip("/") + "/upload/image",
            files={"image": (image_path.name, f, mimetypes.guess_type(image_path.name)[0] or "application/octet-stream")},
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
    name = data["name"]
    returned_subfolder = data.get("subfolder")
    if returned_subfolder:
        return f"{returned_subfolder}/{name}".replace("\\", "/")
    return str(name)


class ComfyUIClient:
    def __init__(self, url: str, output_dir: Path):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("ComfyUI URL must be an HTTP(S) URL")
        self.url = url
        self.output_dir = output_dir

    def run(self, workflow: dict, graph_path: Path, history_path: Path, reason: str) -> dict:
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(json.dumps(workflow, ensure_ascii=False, indent=2), encoding="utf-8")
        free_comfy_memory(self.url, reason, sleep_time=1.0)
        prompt_id, client_id = queue_prompt(workflow, self.url)
        log(f"  [{reason}] prompt_id={prompt_id}")
        history = wait_history(prompt_id, self.url, workflow, client_id)
        check_history_status(history, history_path)
        return history

    def upload(self, image_path: Path, subfolder: str) -> str:
        return upload_image_to_comfy(Path(image_path), self.url, subfolder)

    def execute(self, workflow: dict, kind: str, extensions: set[str], context) -> Path:
        history = self.run(workflow, context.debug_dir / f"{kind}_patched.json",
                           context.debug_dir / f"{kind}_history.json", f"before {kind} generation")
        prefix = "start_image" if kind == "image" else "video"
        path = find_result_file(history, self.output_dir, context.sub_dir, prefix, extensions)
        if path is None:
            raise RuntimeError(f"{kind.title()} result not found for {context.sub_dir}")
        return path
