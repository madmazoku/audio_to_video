"""Template-driven generators, request contracts, and generator factory."""
from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from .comfyui import ComfyUIClient


def merge_config(base: dict, override: dict) -> dict:
    """Merge objects recursively; replace lists (including an empty LoRA list)."""
    result = deepcopy(base)
    for key, value in override.items():
        result[key] = merge_config(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else deepcopy(value)
    return result


@dataclass(frozen=True, kw_only=True)
class GenerationRequest:
    prompt: str
    seed: int
    sub_dir: str
    debug_dir: Path


@dataclass(frozen=True, kw_only=True)
class ImageRequest(GenerationRequest):
    output_prefix: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, kw_only=True)
class VideoRequest(GenerationRequest):
    start_image: Path
    negative_prompt: str
    refine_seed: int
    output_prefix: str
    seconds: float
    fps: float | None = None
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, kw_only=True)
class LlmRequest(GenerationRequest):
    system_prompt: str
    response_path: Path
    max_tokens: int | None = None


@dataclass(frozen=True)
class GenerationResult:
    path: Path
    metadata: dict


@dataclass(frozen=True)
class LlmResult:
    text: str
    metadata: dict


def number(value: Any, name: str, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return float(value)


class Generator(ABC):
    """Service-independent contract; implementations own settings and execution."""

    @abstractmethod
    def metadata(self) -> dict:
        """Return resolved settings and a stable signature for debug and reuse."""
        ...

    @abstractmethod
    def generate(self, request: GenerationRequest) -> GenerationResult | LlmResult:
        """Generate an artifact from the supplied conditions and runtime parameters."""
        ...


class ComfyUIGenerator(Generator):
    """ComfyUI workflow loading, validation, patching, and execution."""

    service = "comfyui"
    kind: str
    extensions: set[str]

    def __init__(self, name: str, settings: dict, workflow_dir: Path, client: ComfyUIClient):
        self.client = client
        self.name = name
        self.settings = deepcopy(settings)
        workflow_name = self.settings.get("workflow")
        if not isinstance(workflow_name, str) or not workflow_name:
            raise ValueError(f"Template {name}: workflow is required")
        workflow_path = (workflow_dir / workflow_name).resolve()
        if not workflow_path.is_relative_to(workflow_dir.resolve()):
            raise ValueError(f"Template {name}: workflow must be inside {workflow_dir}")
        self.graph = json.loads(workflow_path.read_text(encoding="utf-8"))
        if any(node.get("class_type", "").startswith("LoraLoader") for node in self.graph.values()):
            raise ValueError(f"Template {name}: workflow must not contain built-in LoRA nodes; declare adapters in the template")
        self.validate_settings()
        # Validate and assemble without submitting a job, so errors fail early.
        self.configured_graph = self.configure(deepcopy(self.graph))
        self.validate_links(self.configured_graph)
        probes = {"image": ImageRequest(prompt="", seed=0, output_prefix="probe/start_image", sub_dir="probe", debug_dir=Path(".")),
                  "video": VideoRequest(start_image=Path("probe.png"), prompt="", negative_prompt="", seconds=1.0, seed=0, refine_seed=0, output_prefix="probe/video", sub_dir="probe", debug_dir=Path(".")),
                  "llm": LlmRequest(system_prompt="", prompt="", seed=0, response_path=Path("probe.txt"), sub_dir="probe", debug_dir=Path("."))}
        probe = probes[self.kind]
        self.validate_links(self.build_workflow(probe))

    def validate_settings(self) -> None:
        for key in ("checkpoint",):
            if not isinstance(self.settings.get(key), str) or not self.settings[key]:
                raise ValueError(f"Template {self.name}: {key} must be a non-empty string")
        for key in (() if self.kind == "llm" else ("width", "height")):
            value = self.settings.get(key)
            number(value, key, 1)
            if not isinstance(value, int) or value % 8:
                raise ValueError(f"Template {self.name}: {key} must be an integer divisible by 8")
        loras = self.settings.setdefault("loras", [])
        if not isinstance(loras, list):
            raise ValueError(f"Template {self.name}: loras must be a list")
        for lora in loras:
            if not isinstance(lora, dict) or not isinstance(lora.get("name"), str) or not lora["name"]:
                raise ValueError(f"Template {self.name}: each LoRA needs a name")
            number(lora.get("weight"), "LoRA weight")
        if self.kind != "llm":
            for key in ("vae", "text_encoder"):
                if not isinstance(self.settings.get(key), str) or not self.settings[key]:
                    raise ValueError(f"Template {self.name}: {key} is required")

    @staticmethod
    def field(graph: dict, node_id: str, class_type: str, field: str, value: Any) -> None:
        node = graph.get(node_id)
        if not isinstance(node, dict) or node.get("class_type") != class_type or field not in node.get("inputs", {}):
            raise ValueError(f"Incompatible workflow: expected {class_type} node {node_id}, input {field}")
        node["inputs"][field] = value

    @staticmethod
    def validate_links(graph: dict) -> None:
        for node_id, node in graph.items():
            for key, value in node["inputs"].items():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
                    if value[0] not in graph:
                        raise ValueError(f"Dangling workflow link: {node_id}.{key} -> {value[0]}")

    @staticmethod
    def attach_loras(graph: dict, source: list, loras: list, consumers: list[tuple[str, str]], preferred_id: str) -> None:
        """Build a model-only chain; callers explicitly declare its consumers."""
        link = source
        for i, lora in enumerate(loras):
            node_id = preferred_id if i == 0 else f"{preferred_id}_lora_{i}"
            if node_id in graph:
                raise ValueError(f"LoRA node ID collision: {node_id}")
            graph[node_id] = {
                "class_type": "LoraLoaderModelOnly",
                "inputs": {"model": link, "lora_name": lora["name"], "strength_model": lora["weight"]},
                "_meta": {"title": "Load LoRA"},
            }
            link = [node_id, 0]
        for node_id, field in consumers:
            if field not in graph.get(node_id, {}).get("inputs", {}):
                raise ValueError(f"Missing LoRA consumer {node_id}.{field}")
            graph[node_id]["inputs"][field] = link

    def metadata(self) -> dict:
        payload = {"settings": self.settings, "workflow": self.configured_graph}
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return {"template": self.name, "type": self.settings["type"], "service": self.service,
                "settings": deepcopy(self.settings), "signature": signature}

    @abstractmethod
    def configure(self, graph: dict) -> dict: ...

    @abstractmethod
    def build_workflow(self, request: ImageRequest | VideoRequest | LlmRequest) -> dict: ...

    def generate(self, request: ImageRequest | VideoRequest) -> GenerationResult:
        graph = self.build_workflow(request)
        if self.kind == "video":
            uploaded = self.client.upload(request.start_image, f"aligned_song_inputs/{request.sub_dir}")
            graph = self.build_workflow(replace(request, start_image=uploaded))
        self.validate_links(graph)
        return GenerationResult(self.client.execute(graph, self.kind, self.extensions, request), self.metadata())


class Flux2Generator(ComfyUIGenerator):
    kind = "image"
    extensions = {".png", ".jpg", ".jpeg", ".webp"}

    def validate_settings(self) -> None:
        super().validate_settings()
        steps = self.settings.get("steps")
        number(steps, "steps", 1)
        if not isinstance(steps, int):
            raise ValueError("steps must be an integer")
        number(self.settings.get("guidance"), "guidance", 0)
        if self.settings.get("sampling") not in {"standard", "turbo"}:
            raise ValueError("FLUX2 sampling must be standard or turbo")
        if not isinstance(self.settings.get("sampler"), str) or not self.settings["sampler"]:
            raise ValueError("FLUX2 sampler is required")
        if self.settings.get("sampling") == "turbo" and not any(l.get("role") == "acceleration" for l in self.settings["loras"]):
            raise ValueError("FLUX2 Turbo requires its acceleration LoRA; select a standard template for no LoRA")

    def configure(self, graph: dict) -> dict:
        s = self.settings
        patches = [
            ("1001", "UNETLoader", "unet_name", s["checkpoint"]),
            ("1002", "CLIPLoader", "clip_name", s["text_encoder"]),
            ("1010", "VAELoader", "vae_name", s["vae"]),
            ("1025", "FluxGuidance", "guidance", s["guidance"]),
            ("1023", "KSamplerSelect", "sampler_name", s["sampler"]),
        ]
        for node, cls, field, value in patches:
            self.field(graph, node, cls, field, value)
        for node, cls in (("1007", "EmptyLatentImage"), ("1024", "Flux2Scheduler")):
            for key in ("width", "height"):
                self.field(graph, node, cls, key, s[key])
        self.field(graph, "1024", "Flux2Scheduler", "steps", s["steps"])
        self.attach_loras(graph, ["1001", 0], s["loras"], [("1006", "model")], "generation_lora")
        if s.get("sampling") == "turbo":
            sigmas = s.get("sigmas")
            if not isinstance(sigmas, list) or len(sigmas) != s["steps"] + 1:
                raise ValueError("Turbo sigmas must include one value per step plus terminal zero")
            for sigma in sigmas:
                number(sigma, "sigma", 0)
            if sigmas[-1] != 0 or any(a <= b for a, b in zip(sigmas, sigmas[1:])):
                raise ValueError("Turbo sigmas must decrease to zero")
            graph["1024"] = {"class_type": "ManualSigmas", "inputs": {"sigmas": ", ".join(map(str, sigmas))}}
        return graph

    def build_workflow(self, request: ImageRequest) -> dict:
        graph = deepcopy(self.configured_graph)
        for key in ("width", "height"):
            value = getattr(request, key)
            if value is not None:
                number(value, key, 1)
                if not isinstance(value, int) or value % 8:
                    raise ValueError(f"{key} must be an integer divisible by 8")
                self.field(graph, "1007", "EmptyLatentImage", key, value)
                if self.settings["sampling"] == "standard":
                    self.field(graph, "1024", "Flux2Scheduler", key, value)
        self.field(graph, "1004", "CLIPTextEncode", "text", request.prompt)
        self.field(graph, "1022", "RandomNoise", "noise_seed", request.seed)
        self.field(graph, "1011", "SaveImage", "filename_prefix", request.output_prefix)
        return graph


class LtxGenerator(ComfyUIGenerator):
    kind = "video"
    extensions = {".mp4", ".mov", ".webm", ".mkv"}

    def validate_settings(self) -> None:
        super().validate_settings()
        for key in ("fps", "cfg", "refine_cfg"):
            number(self.settings.get(key), key, 0.001)
        steps = self.settings.get("steps")
        number(steps, "steps", 1)
        if not isinstance(steps, int):
            raise ValueError("steps must be an integer")
        if self.settings.get("sampling") not in {"full", "distilled"}:
            raise ValueError("LTX sampling must be explicitly full or distilled")
        sampling = self.settings["sampling"]
        accelerated = any(l.get("role") == "acceleration" for l in self.settings["loras"])
        if sampling == "distilled" and not accelerated:
            raise ValueError("Distilled sampling requires an acceleration LoRA")
        self.full_sampling = sampling == "full"
        if self.full_sampling:
            value = self.settings.get("refine_steps")
            number(value, "refine_steps", 1)
            if not isinstance(value, int):
                raise ValueError("refine_steps must be an integer")
        elif not isinstance(self.settings.get("refine_sigmas"), str) or not self.settings["refine_sigmas"]:
            raise ValueError("Distilled LTX requires refine_sigmas")
        for key in ("sampler", "refine_sampler"):
            if not isinstance(self.settings.get(key), str) or not self.settings[key]:
                raise ValueError(f"LTX requires {key}")
        for key in ("text_encoder_projection", "audio_vae", "upscaler"):
            if not isinstance(self.settings.get(key), str) or not self.settings[key]:
                raise ValueError(f"LTX requires {key}")

    def configure(self, graph: dict) -> dict:
        s = self.settings
        patches = [
            ("270", "UNETLoader", "unet_name", s["checkpoint"]),
            ("267", "VAELoader", "vae_name", s["vae"]),
            ("389", "VAELoader", "vae_name", s["audio_vae"]),
            ("332", "DualCLIPLoader", "clip_name1", s["text_encoder"]),
            ("332", "DualCLIPLoader", "clip_name2", s["text_encoder_projection"]),
            ("297", "LatentUpscaleModelLoader", "model_name", s["upscaler"]),
            ("261", "INTConstant", "value", s["width"]),
            ("299", "INTConstant", "value", s["height"]),
            ("304", "PrimitiveFloat", "value", s["fps"]),
            ("307", "INTConstant", "value", s["steps"]),
            ("305", "PrimitiveFloat", "value", s["cfg"]),
            ("284", "CFGGuider", "cfg", s["refine_cfg"]),
            ("325", "KSamplerSelect", "sampler_name", s["sampler"]),
            ("286", "KSamplerSelect", "sampler_name", s["refine_sampler"]),
        ]
        for node, cls, field, value in patches:
            self.field(graph, node, cls, field, value)
        for first_id, consumer in (("349", "326"), ("294", "284")):
            self.field(graph, consumer, "CFGGuider", "model", ["269", 0])
            self.attach_loras(graph, ["269", 0], s["loras"], [(consumer, "model")], first_id)
        if self.full_sampling:
            # An unaccelerated refinement schedule, rather than the 3-step distilled schedule.
            self.field(graph, "285", "ManualSigmas", "sigmas", graph["285"]["inputs"]["sigmas"])
            graph["285"] = {"class_type": "LTXVScheduler", "inputs": {
                "steps": s["refine_steps"], "max_shift": 2.5, "base_shift": 1,
                "stretch": True, "terminal": 0.1, "latent": ["287", 0]},
                "_meta": {"title": "Unaccelerated refinement schedule"}}
        else:
            self.field(graph, "285", "ManualSigmas", "sigmas", s["refine_sigmas"])
        return graph

    def build_workflow(self, request: VideoRequest) -> dict:
        number(request.seconds, "video duration", 0.001)
        fps = self.settings["fps"] if request.fps is None else request.fps
        number(fps, "fps", 0.001)
        graph = deepcopy(self.configured_graph)
        for key, node in (("width", "261"), ("height", "299")):
            value = getattr(request, key)
            if value is not None:
                number(value, key, 1)
                if not isinstance(value, int) or value % 8:
                    raise ValueError(f"{key} must be an integer divisible by 8")
                self.field(graph, node, "INTConstant", "value", value)
        self.field(graph, "304", "PrimitiveFloat", "value", fps)
        for node, cls, field, value in (
            ("9000", "LoadImage", "image", str(request.start_image)),
            ("393", "PrimitiveStringMultiline", "value", request.prompt),
            ("328", "CLIPTextEncode", "text", request.negative_prompt),
            ("322", "PrimitiveFloat", "value", request.seconds),
            ("259", "RandomNoise", "noise_seed", request.seed),
            ("283", "RandomNoise", "noise_seed", request.refine_seed),
            ("327", "SaveVideo", "filename_prefix", request.output_prefix),
        ):
            self.field(graph, node, cls, field, value)
        return graph


class LlamaCppGenerator(ComfyUIGenerator):
    kind = "llm"

    def validate_settings(self) -> None:
        super().validate_settings()
        if self.settings["loras"]:
            raise ValueError("The current llama-cpp generator does not support LoRA adapters")
        for key in ("n_ctx", "max_tokens"):
            value = self.settings.get(key)
            number(value, key, 1)
            if not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
        if not isinstance(self.settings.get("parameters"), dict):
            raise ValueError("LLM parameters must be an object")
        for key, value in self.settings["parameters"].items():
            number(value, key)

    def configure(self, graph: dict) -> dict:
        self.field(graph, "1", "llama_cpp_model_loader", "model", self.settings["checkpoint"])
        self.field(graph, "1", "llama_cpp_model_loader", "n_ctx", self.settings["n_ctx"])
        self.field(graph, "4", "llama_cpp_parameters", "max_tokens", self.settings["max_tokens"])
        for key, value in self.settings["parameters"].items():
            self.field(graph, "4", "llama_cpp_parameters", key, value)
        # The unload node is a required part of this current workflow.
        self.field(graph, "9101", "llama_cpp_unload_model", "any", ["2", 0])
        return graph

    def build_workflow(self, request: LlmRequest) -> dict:
        graph = deepcopy(self.configured_graph)
        if request.max_tokens is not None:
            number(request.max_tokens, "max_tokens", 1)
            if not isinstance(request.max_tokens, int):
                raise ValueError("max_tokens must be an integer")
            self.field(graph, "4", "llama_cpp_parameters", "max_tokens", request.max_tokens)
        for field, value in (("system_prompt", request.system_prompt), ("custom_prompt", request.prompt), ("seed", request.seed)):
            self.field(graph, "2", "llama_cpp_instruct_adv", field, value)
        self.field(graph, "3", "Basic data handling: PathSaveStringFile", "path", str(request.response_path.resolve()))
        return graph

    def generate(self, request: LlmRequest) -> LlmResult:
        graph = self.build_workflow(request)
        self.validate_links(graph)
        request.response_path.parent.mkdir(parents=True, exist_ok=True)
        if request.response_path.exists():
            request.response_path.unlink()
        self.client.run(graph, request.debug_dir / f"{request.sub_dir}_patched.json",
                        request.debug_dir / f"{request.sub_dir}_history.json", "LLM generation")
        if not request.response_path.exists():
            raise RuntimeError(f"LLM did not write its response: {request.response_path}")
        metadata = self.metadata()
        (request.debug_dir / f"{request.sub_dir}_generator.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        return LlmResult(request.response_path.read_text(encoding="utf-8").strip(), metadata)


def create_generator(kind: str, catalog: dict, config: dict, workflow_dir: Path) -> Generator:
    if kind not in {"image", "video", "llm"}:
        raise ValueError(f"Unknown generator kind: {kind}")
    selection = config.get(f"{kind}_generation")
    if not isinstance(selection, dict):
        raise ValueError(f"{kind}_generation must be an object")
    name = selection.get("template")
    entries = catalog.get(f"{kind}_generation")
    if not isinstance(entries, dict):
        raise ValueError(f"Catalog requires {kind}_generation")
    if not isinstance(name, str) or name not in entries:
        raise ValueError(f"Unknown {kind} template: {name}")
    override = {key: value for key, value in selection.items() if key != "template"}
    settings = merge_config(entries[name], override)
    # Runtime dimensions take precedence over recipe defaults, as in the existing runner.
    dimensions = () if kind == "llm" else (("width", "video_width"), ("height", "video_height"), ("fps", "video_fps"))
    for key, config_key in dimensions:
        if key not in override and config_key in config:
            settings[key] = config[config_key]
    registry = {"image": {"flux2": Flux2Generator}, "video": {"ltx": LtxGenerator}, "llm": {"llama_cpp": LlamaCppGenerator}}
    cls = registry.get(kind, {}).get(settings.get("type"))
    if cls is None:
        raise ValueError(f"Unsupported {kind} generator type: {settings.get('type')}")
    if kind == "video" and (settings["width"], settings["height"], settings["fps"]) != (config["video_width"], config["video_height"], config["video_fps"]):
        raise ValueError("Video generator width/height/fps must match video_width/video_height/video_fps; set those global options")
    output_dir = Path(config["comfy_output_dir"])
    if not output_dir.is_absolute():
        output_dir = workflow_dir.resolve().parent / output_dir
    client = ComfyUIClient(config["comfy_url"], output_dir.resolve())
    return cls(name, settings, workflow_dir, client)
