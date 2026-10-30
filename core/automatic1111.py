"""AUTOMATIC1111 API client for checkpoint-controlled image generation."""
from __future__ import annotations

import base64
import json
import logging
import re
import struct
from pathlib import Path
from typing import Callable

import requests


class Automatic1111Client:
    def __init__(self, url: str, output_dir: Path, before_generate: Callable[[], None] | None = None):
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("AUTOMATIC1111 URL must be an HTTP(S) URL")
        self.url = url.rstrip("/")
        self.output_dir = output_dir
        self.before_generate = before_generate
        self.session = requests.Session()
        self.session.trust_env = False

    def get(self, endpoint: str):
        response = self.session.get(self.url + endpoint, timeout=30)
        response.raise_for_status()
        return response.json()

    def release_memory(self) -> None:
        response = self.session.post(self.url + "/sdapi/v1/unload-checkpoint", timeout=120)
        response.raise_for_status()

    def resolve_checkpoint(self, name: str) -> str:
        matches = []
        for item in self.get("/sdapi/v1/sd-models"):
            identifiers = (item["title"], item["model_name"],
                           item["filename"].replace("\\", "/").rsplit("/", 1)[-1])
            if name in identifiers:
                matches.append(item["title"])
        if len(matches) != 1:
            raise ValueError(f"Checkpoint must identify exactly one installed AUTOMATIC1111 model: {name}")
        return matches[0]

    def generate(self, payload: dict, output_prefix: str, debug_dir: Path) -> Path:
        output_path = (self.output_dir / (output_prefix + ".png")).resolve()
        if not output_path.is_relative_to(self.output_dir.resolve()):
            raise ValueError("Image output prefix must stay inside the service output directory")
        debug_dir.mkdir(parents=True, exist_ok=True)
        payload = dict(payload)
        payload["override_settings"] = dict(payload["override_settings"])
        payload["override_settings"]["sd_model_checkpoint"] = self.resolve_checkpoint(
            payload["override_settings"]["sd_model_checkpoint"])
        samplers = {item["name"] for item in self.get("/sdapi/v1/samplers")}
        if payload["sampler_name"] not in samplers:
            raise ValueError(f"Unknown AUTOMATIC1111 sampler: {payload['sampler_name']}")
        schedulers = {value for item in self.get("/sdapi/v1/schedulers")
                      for value in (item["name"], item["label"])}
        if payload["scheduler"] not in schedulers:
            raise ValueError(f"Unknown AUTOMATIC1111 scheduler: {payload['scheduler']}")
        loras = re.findall(r"<lora:([^:>]+):[^>]+>", payload["prompt"])
        if loras:
            installed = {value for item in self.get("/sdapi/v1/loras")
                         for value in (item["name"], item.get("alias"))}
            missing = set(loras) - installed
            if missing:
                raise ValueError(f"Unknown AUTOMATIC1111 LoRAs: {sorted(missing)}")
        (debug_dir / "image_request.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        if self.before_generate is not None:
            self.before_generate()
        # Request-local checkpoint override avoids the silent failed /options switch.
        failure = None
        try:
            selected = {k: payload["override_settings"][k] for k in ("sd_model_checkpoint", "sd_vae")}
            switch = self.session.post(self.url + "/sdapi/v1/options", json=selected, timeout=3600)
            switch.raise_for_status()
            # unload-checkpoint moves weights to CPU; an unchanged checkpoint needs
            # an explicit move back to the GPU on the following request.
            reload = self.session.post(self.url + "/sdapi/v1/reload-checkpoint", timeout=3600)
            reload.raise_for_status()
            response = self.session.post(self.url + "/sdapi/v1/txt2img", json=payload, timeout=3600)
            response.raise_for_status()
            data = response.json()
            if not data.get("images"):
                raise RuntimeError("AUTOMATIC1111 returned no image")
            encoded = data["images"][0].split(",", 1)[-1]
            decoded = base64.b64decode(encoded, validate=True)
            if len(decoded) < 24 or not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
                raise RuntimeError("AUTOMATIC1111 did not return a PNG image")
            if struct.unpack(">II", decoded[16:24]) != (payload["width"], payload["height"]):
                raise RuntimeError("AUTOMATIC1111 returned unexpected image dimensions")
            (debug_dir / "image_response.json").write_text(
                json.dumps({"info": data.get("info"), "parameters": data.get("parameters")}, indent=2), encoding="utf-8")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(decoded)
            return output_path
        except Exception as exc:
            failure = exc
            raise
        finally:
            # Release SDXL before the next ComfyUI planner/video job.
            try:
                self.release_memory()
            except Exception:
                if failure is None:
                    raise
                logging.exception("AUTOMATIC1111 cleanup failed after generation error")
