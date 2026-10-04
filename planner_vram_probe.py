from __future__ import annotations

import argparse
import csv
import json
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests


def ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def log(msg: str) -> None:
    for line in str(msg).splitlines() or [""]:
        print(f"{ts()}  {line}" if line.strip() else "", flush=True)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def query_vram_mb() -> Optional[Tuple[int, int]]:
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
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 2:
            try:
                return int(float(parts[0])), int(float(parts[1]))
            except ValueError:
                pass
    return None


def fmt_vram(vram: Optional[Tuple[int, int]]) -> str:
    if vram is None:
        return "vram unavailable"
    return f"{vram[0]} / {vram[1]}"


class CsvLogger:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fp = self.path.open("w", newline="", encoding="utf-8")
        self.writer = csv.writer(self.fp)
        self.writer.writerow(["timestamp", "iteration", "phase", "elapsed_s", "used_mb", "total_mb", "note"])
        self.fp.flush()

    def row(self, iteration: int, phase: str, elapsed_s: float, note: str = "") -> None:
        vram = query_vram_mb()
        used = vram[0] if vram else ""
        total = vram[1] if vram else ""
        self.writer.writerow([ts(), iteration, phase, f"{elapsed_s:.3f}", used, total, note])
        self.fp.flush()

    def close(self) -> None:
        self.fp.close()


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


def wait_history_poll(
    prompt_id: str,
    comfy_url: str,
    iteration: int,
    csvlog: CsvLogger,
    report_seconds: float,
    timeout_seconds: float,
) -> Dict[str, Any]:
    url = comfy_url.rstrip("/") + f"/history/{prompt_id}"
    start = time.perf_counter()
    last_report = 0.0
    while True:
        elapsed = time.perf_counter() - start
        if elapsed > timeout_seconds:
            raise TimeoutError(f"ComfyUI prompt did not finish within {timeout_seconds:.0f}s: {prompt_id}")

        r = requests.get(url, timeout=60)
        r.raise_for_status()
        h = r.json()
        if prompt_id in h:
            csvlog.row(iteration, "planner_finished", elapsed, prompt_id)
            log(f"[planner] finished after {elapsed:.1f}s vram={fmt_vram(query_vram_mb())}")
            return h[prompt_id]

        if elapsed - last_report >= report_seconds:
            last_report = elapsed
            csvlog.row(iteration, "planner_running", elapsed, prompt_id)
            log(f"[planner] running {elapsed:.1f}s vram={fmt_vram(query_vram_mb())}")

        time.sleep(0.5)


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


def free_comfy_memory(
    comfy_url: str,
    iteration: int,
    csvlog: CsvLogger,
    reason: str,
    wait_seconds: float,
    poll_interval: float,
) -> None:
    payload = {"unload_models": True, "free_memory": True}
    base = comfy_url.rstrip("/")
    before = query_vram_mb()
    used_before = before[0] if before else None
    ok_path = None

    csvlog.row(iteration, "free_before", 0.0, reason)

    for path in ("/free", "/api/free"):
        try:
            r = requests.post(base + path, json=payload, timeout=60)
            if r.status_code == 404:
                continue
            r.raise_for_status()
            ok_path = path
            break
        except Exception as exc:
            log(f"[comfy] free memory {fmt_vram(before)} failed ({reason}): {path}: {exc}")

    if ok_path:
        log(f"[comfy] free memory {fmt_vram(before)} ok ({reason}): {ok_path}")
    else:
        log(f"[comfy] free memory {fmt_vram(before)} failed/non-fatal ({reason})")

    start = time.perf_counter()
    elapsed = 0.0
    while elapsed + 1e-9 < wait_seconds:
        step = min(poll_interval, wait_seconds - elapsed)
        time.sleep(step)
        elapsed = time.perf_counter() - start
        vram = query_vram_mb()
        csvlog.row(iteration, "free_wait", elapsed, reason)
        log(f"[comfy] waiting {elapsed:.1f}s: {fmt_vram(vram)}")

    after = query_vram_mb()
    csvlog.row(iteration, "free_after", time.perf_counter() - start, reason)
    if used_before is not None and after is not None:
        log(f"[comfy] free done ({reason}): {fmt_vram(after)} delta={after[0] - used_before:+d}MB")
    else:
        log(f"[comfy] free done ({reason}): {fmt_vram(after)}")


def make_iteration_workflow(template: Dict[str, Any], out_path: Path, iteration: int, prompt_repeat: int) -> Dict[str, Any]:
    """Preserve model/loader settings exactly as they are in workflow.

    Only patch the actual planner request text and save path, like the runner does.
    This intentionally does not touch the llama-cpp model loader, n_ctx,
    max_tokens, sampler settings, or any other model setting.
    """
    wf = json.loads(json.dumps(template))

    if "2" not in wf or "3" not in wf:
        raise RuntimeError("planner workflow must contain node 2=llama-cpp planner and node 3=PathSaveStringFile")

    node2_inputs = wf["2"].setdefault("inputs", {})
    node2_inputs["system_prompt"] = (
        "You are a visual prompt planner. Return ONLY one valid JSON object with keys "
        "scene_summary, image_prompt, video_prompt, negative_prompt. Do not use markdown."
    )

    base_prompt = (
        "Create a compact visual plan for a gritty fantasy tavern music-video scene. "
        "The current scene: a tired cleric hears a magical sending stone ringing after a battle. "
        "Describe a non-looping action with a clear start, change, and final frame. "
        "Do not request visible text, captions, signs, labels, watermarks, or subtitles. "
    )
    node2_inputs["custom_prompt"] = (base_prompt * max(1, int(prompt_repeat))).strip() + f"\nIteration: {iteration}. Return JSON only."

    node3_inputs = wf["3"].setdefault("inputs", {})
    node3_inputs["path"] = str(out_path)
    node3_inputs["create_dirs"] = True
    node3_inputs["encoding"] = "utf-8"
    return wf


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Repeatedly run planner_visual_prompts_api.json and measure whether VRAM is released by ComfyUI /free. Preserves llama-cpp model and sampler settings."
    )
    ap.add_argument("--comfy-url", default="http://127.0.0.1:8188")
    ap.add_argument("--workflow", type=Path, default=Path("workflows/planner_visual_prompts_api.json"))
    ap.add_argument("--out-dir", type=Path, default=Path("output/work/vram_probe/planner"))
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--free-wait-seconds", type=float, default=60.0)
    ap.add_argument("--free-poll-interval", type=float, default=1.0)
    ap.add_argument("--run-report-seconds", type=float, default=5.0)
    ap.add_argument("--prompt-timeout-seconds", type=float, default=900.0)
    ap.add_argument("--prompt-repeat", type=int, default=16, help="Increase to create a larger planner prompt.")
    ap.add_argument("--free-before-first", action=argparse.BooleanOptionalAction, default=True)
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / f"planner_vram_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    csvlog = CsvLogger(csv_path)

    log(f"[probe] comfy_url={args.comfy_url}")
    log(f"[probe] workflow={args.workflow.resolve()}")
    log(f"[probe] out_dir={args.out_dir.resolve()}")
    log(f"[probe] csv={csv_path.resolve()}")
    log(f"[probe] iterations={args.iterations} free_wait={args.free_wait_seconds}s poll={args.free_poll_interval}s")
    log("[probe] model, context, output, and sampler settings are preserved from the workflow")
    log(f"[probe] initial vram={fmt_vram(query_vram_mb())}")

    template = load_json(args.workflow)

    try:
        if args.free_before_first:
            free_comfy_memory(args.comfy_url, 0, csvlog, "before first planner", args.free_wait_seconds, args.free_poll_interval)

        after_free_used = []
        for i in range(1, args.iterations + 1):
            log("")
            log(f"=== planner probe iteration {i}/{args.iterations}")
            csvlog.row(i, "iteration_start", 0.0, "")
            log(f"[probe] before planner: {fmt_vram(query_vram_mb())}")

            out_path = args.out_dir / f"planner_response_{i:03d}.txt"
            hist_path = args.out_dir / f"planner_history_{i:03d}.json"
            try:
                out_path.unlink()
            except FileNotFoundError:
                pass

            wf = make_iteration_workflow(template, out_path, i, args.prompt_repeat)
            pid, _client_id = queue_prompt(wf, args.comfy_url)
            log(f"[planner] prompt_id={pid}")
            csvlog.row(i, "planner_queued", 0.0, pid)
            history = wait_history_poll(pid, args.comfy_url, i, csvlog, args.run_report_seconds, args.prompt_timeout_seconds)
            check_history_status(history, hist_path)

            if out_path.exists():
                log(f"[planner] response={out_path}")
            else:
                log(f"[planner] WARNING: response file was not created: {out_path}")

            log(f"[probe] after planner before free: {fmt_vram(query_vram_mb())}")
            free_comfy_memory(args.comfy_url, i, csvlog, "after planner", args.free_wait_seconds, args.free_poll_interval)
            v = query_vram_mb()
            if v:
                after_free_used.append(v[0])
            csvlog.row(i, "iteration_end", 0.0, "")

        log("")
        if after_free_used:
            first = after_free_used[0]
            last = after_free_used[-1]
            log(f"[summary] after-free used MB by iteration: {after_free_used}")
            log(f"[summary] first={first}MB last={last}MB growth={last - first:+d}MB")
        log(f"[summary] csv={csv_path.resolve()}")
    finally:
        csvlog.close()


if __name__ == "__main__":
    main()
