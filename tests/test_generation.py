"""Offline checks for template resolution and multi-pass graph wiring."""
import json
from pathlib import Path
import tempfile
import unittest

from core.generator import Generator, GenerationResult, GenerationRequest, ImageRequest, VideoRequest, LlmRequest, create_generator, merge_config
from aligned_song_video_runner import load_config


ROOT = Path(__file__).resolve().parents[1]


class GenerationTests(unittest.TestCase):
    def test_karaoke_delays_use_only_a_zero_width_character(self):
        import re
        from aligned_song_video_runner import build_karaoke_delay, build_word_karaoke_line
        self.assertEqual(re.sub(r"\{[^}]*\}", "", build_karaoke_delay(.76)), "\u200b")
        line = {"start": 0, "end": 2, "text": "I said That s just my face", "words": [
            {"text": "I said", "start": 0, "end": .4},
            {"text": "That s", "start": 1.16, "end": 1.5},
            {"text": "just my face", "start": 1.56, "end": 2},
        ]}
        text = re.sub(r"\{[^}]*\}", "", build_word_karaoke_line(line, 0))
        self.assertEqual(text.replace("\u200b", ""), line["text"])
        self.assertEqual(text.count("\u200b"), 2)

    def test_base_contract_needs_no_comfy_workflow_or_connection(self):
        class IndependentGenerator(Generator):
            def metadata(self):
                return {"signature": "independent"}

            def generate(self, request):
                return GenerationResult(Path("result.png"), self.metadata())

        gen = IndependentGenerator()
        result = gen.generate(ImageRequest(prompt="captain", seed=1, output_prefix="image", sub_dir="run", debug_dir=ROOT))
        self.assertEqual(result.path, Path("result.png"))
        self.assertEqual(Generator.__abstractmethods__, {"generate", "metadata"})
        for name in ("configure", "build_workflow", "validate_settings", "field", "validate_links", "attach_loras"):
            self.assertFalse(hasattr(Generator, name))

    def test_requests_are_independent_immutable_types(self):
        from dataclasses import FrozenInstanceError
        self.assertTrue(issubclass(VideoRequest, GenerationRequest))
        self.assertFalse(issubclass(VideoRequest, ImageRequest))
        request = ImageRequest(prompt="captain", seed=1, output_prefix="image", sub_dir="run", debug_dir=ROOT)
        with self.assertRaises(FrozenInstanceError):
            request.prompt = "changed"

    def test_runtime_parameters_override_without_mutating_generator(self):
        from dataclasses import replace
        image = self.generator("image")
        request = ImageRequest(prompt="captain", seed=1, output_prefix="image", sub_dir="run", debug_dir=ROOT)
        before = image.metadata()
        graph = image.build_workflow(replace(request, width=768, height=512))
        self.assertEqual(graph["1007"]["inputs"]["width"], 768)
        self.assertEqual(graph["1024"]["inputs"]["height"], 512)
        self.assertEqual(image.metadata(), before)
        turbo = self.generator("image", image_generation={"template": "flux2_dev_turbo"})
        self.assertEqual(turbo.build_workflow(replace(request, width=768))["1007"]["inputs"]["width"], 768)
        llm = self.generator("llm")
        graph = llm.build_workflow(LlmRequest(prompt="question", system_prompt="system", seed=0,
            response_path=ROOT / "response.txt", sub_dir="plan", debug_dir=ROOT, max_tokens=1024))
        self.assertEqual(graph["4"]["inputs"]["max_tokens"], 1024)
        self.assertEqual(llm.settings["max_tokens"], 4096)

    def test_video_seconds_and_invalid_requests_before_upload(self):
        from dataclasses import replace
        from unittest.mock import Mock
        gen = self.generator("video")
        request = VideoRequest(prompt="turn", start_image=Path("start.png"), negative_prompt="", seed=1,
            refine_seed=2, output_prefix="video", sub_dir="run", debug_dir=ROOT,
            seconds=5.25, fps=24, width=768, height=512)
        graph = gen.build_workflow(request)
        self.assertEqual(graph["404"], gen.configured_graph["404"])
        self.assertEqual(graph["322"]["inputs"]["value"], 5.25)
        self.assertEqual(graph["261"]["inputs"]["value"], 768)
        gen.client.upload = Mock(side_effect=AssertionError("unexpected upload"))
        gen.client.execute = Mock(side_effect=AssertionError("unexpected execution"))
        for change in ({"seconds":0}, {"seconds":-1}, {"seconds":None}, {"seconds":float("nan")}, {"fps":0}, {"width":767}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                gen.generate(replace(request, **change))
        gen.client.upload.assert_not_called()
        gen.client.execute.assert_not_called()

    def setUp(self):
        self.config = json.loads((ROOT / "data/config.json").read_text())
        self.catalog = json.loads((ROOT / "data/model_templates.json").read_text())

    def generator(self, kind, execute=None, **override):
        config = merge_config(self.config, override)
        gen = create_generator(kind, self.catalog, config, ROOT / "workflows")
        if execute is not None:
            gen.client.execute = lambda graph, kind, extensions, context: execute(gen.client.url, graph, kind, extensions, context)
        return gen

    def test_default_image_preserves_previous_graph(self):
        expected = json.loads((ROOT / "workflows/image_from_prompt_api.json").read_text())
        expected["1004"]["inputs"]["text"] = "captain"
        expected["1022"]["inputs"]["noise_seed"] = 123
        expected["1011"]["inputs"]["filename_prefix"] = "run/start_image"
        actual = self.generator("image").build_workflow(ImageRequest(prompt="captain", seed=123, output_prefix="run/start_image", sub_dir="probe", debug_dir=Path(".")))
        self.assertEqual(actual, expected)

    def test_default_video_adds_only_template_adapters(self):
        expected = json.loads((ROOT / "workflows/video_from_image_api.json").read_text())
        lora = self.catalog["video_generation"]["ltx23_current"]["loras"][0]
        for node, consumer in (("349", "326"), ("294", "284")):
            expected[node] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                "model": ["269", 0], "lora_name": lora["name"], "strength_model": lora["weight"]},
                "_meta": {"title": "Load LoRA"}}
            expected[consumer]["inputs"]["model"] = [node, 0]
        for node, field, value in (("9000", "image", "input.png"), ("393", "value", "turn"),
                                    ("328", "text", "text"), ("322", "value", 7.5),
                                    ("259", "noise_seed", 12), ("283", "noise_seed", 34),
                                    ("327", "filename_prefix", "run/video")):
            expected[node]["inputs"][field] = value
        actual = self.generator("video").build_workflow(VideoRequest(start_image="input.png", prompt="turn", negative_prompt="text", seconds=7.5, seed=12, refine_seed=34, output_prefix="run/video", sub_dir="probe", debug_dir=Path(".")))
        self.assertEqual(actual, expected)

    def test_image_lora_chain_and_dimensions(self):
        loras = [{"name": "a.safetensors", "weight": .7}, {"name": "b.safetensors", "weight": -.2}]
        gen = self.generator("image", image_generation={"checkpoint": "custom.safetensors", "loras": loras, "width": 768})
        graph = gen.configured_graph
        self.assertEqual(graph["1001"]["inputs"]["unet_name"], "custom.safetensors")
        self.assertEqual(graph["1007"]["inputs"]["width"], 768)
        self.assertEqual(graph["1024"]["inputs"]["width"], 768)
        self.assertEqual(graph["generation_lora_lora_1"]["inputs"]["model"], ["generation_lora", 0])
        self.assertEqual(graph["1006"]["inputs"]["model"], ["generation_lora_lora_1", 0])
        self.assertEqual(graph["generation_lora_lora_1"]["inputs"]["strength_model"], -.2)

    def test_both_ltx_passes_receive_all_loras(self):
        loras = self.catalog["video_generation"]["ltx23_current"]["loras"] + [{"name": "style.safetensors", "weight": .5}]
        graph = self.generator("video", video_generation={"loras": loras}).configured_graph
        for first, consumer in (("349", "326"), ("294", "284")):
            self.assertEqual(graph[first + "_lora_1"]["inputs"]["model"], [first, 0])
            self.assertEqual(graph[consumer]["inputs"]["model"], [first + "_lora_1", 0])

    def test_empty_ltx_loras_requires_explicit_full_recipe(self):
        with self.assertRaisesRegex(ValueError, "requires an acceleration LoRA"):
            self.generator("video", video_generation={"loras": []})
        gen = self.generator("video", video_generation={"template": "ltx23_unaccelerated"})
        graph = gen.configured_graph
        self.assertFalse(any(n["class_type"].startswith("LoraLoader") for n in graph.values()))
        self.assertEqual(graph["326"]["inputs"]["model"], ["269", 0])
        self.assertEqual(graph["284"]["inputs"]["model"], ["269", 0])
        self.assertEqual(graph["285"]["class_type"], "LTXVScheduler")
        self.assertTrue(gen.full_sampling)

    def test_generation_selection_is_required_without_fallback(self):
        for missing in ("image_generation", "video_generation"):
            config = dict(self.config)
            del config[missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "must be an object"):
                create_generator(missing.split("_")[0], self.catalog, config, ROOT / "workflows")
        for selection in ({}, {"steps": 25}):
            config = dict(self.config, image_generation=selection)
            with self.assertRaisesRegex(ValueError, "Unknown image template"):
                create_generator("image", self.catalog, config, ROOT / "workflows")

    def test_missing_sampling_is_not_inferred(self):
        catalog = merge_config(self.catalog, {})
        del catalog["image_generation"]["flux2_dev_current"]["sampling"]
        with self.assertRaisesRegex(ValueError, "sampling"):
            create_generator("image", catalog, self.config, ROOT / "workflows")

    def test_unaccelerated_fields_can_be_overridden_directly(self):
        gen = self.generator("video", video_generation={"template": "ltx23_unaccelerated", "steps": 35, "refine_steps": 15})
        self.assertEqual(gen.configured_graph["307"]["inputs"]["value"], 35)
        self.assertEqual(gen.configured_graph["285"]["inputs"]["steps"], 15)

    def test_turbo_and_full_vae_templates(self):
        turbo = self.generator("image", image_generation={"template": "flux2_dev_turbo"})
        self.assertEqual(turbo.configured_graph["1024"]["class_type"], "ManualSigmas")
        self.assertEqual(len(turbo.configured_graph["1024"]["inputs"]["sigmas"].split(",")), 9)
        self.assertEqual(self.generator("image", image_generation={"template": "flux2_dev_full_vae"}).settings["vae"], "flux2-vae.safetensors")
        with self.assertRaisesRegex(ValueError, "requires"):
            self.generator("image", image_generation={"template": "flux2_dev_turbo", "loras": []})

    def test_video_variants_use_one_generator_and_no_hidden_loras(self):
        baseline = self.generator("video")
        fp8 = self.generator("video", video_generation={"template": "ltx23_fp8"})
        full = self.generator("video", video_generation={"template": "ltx23_unaccelerated"})
        self.assertIs(type(fp8), type(baseline))
        self.assertIs(type(full), type(baseline))
        self.assertEqual(fp8.configured_graph["270"]["inputs"]["unet_name"], "ltx-2.3-22b-dev-fp8.safetensors")
        self.assertEqual(full.settings["loras"], [])
        self.assertTrue(full.full_sampling)
        self.assertEqual(full.configured_graph["307"]["inputs"]["value"], 30)

    def test_partial_local_config_merges_and_empty_list_replaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "data"
            data.mkdir()
            base = dict(self.config, image_generation={"template": "flux2_dev_current", "steps": 20, "loras": [{"name": "a", "weight": 1}]})
            (data / "config.json").write_text(json.dumps(base))
            (root / "config.json").write_text(json.dumps({"image_generation": {"loras": []}}))
            resolved = load_config(root, data)
            self.assertEqual(resolved["image_generation"], {"template": "flux2_dev_current", "steps": 20, "loras": []})
            generator = create_generator("image", self.catalog, resolved, ROOT / "workflows")
            self.assertEqual(generator.name, "flux2_dev_current")
            self.assertEqual(generator.configured_graph["1024"]["inputs"]["steps"], 20)

    def test_invalid_configs_fail_early(self):
        for args in ({"image_generation": []}, {"image_generation": {"template": "missing"}}, {"image_generation": {"type": "sdxl"}},
                     {"image_generation": {"width": 786}},
                     {"image_generation": {"loras": [{"name": "a", "weight": float("nan")}]}}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.generator("image", **args)
        with self.assertRaisesRegex(ValueError, "match"):
            self.generator("video", video_generation={"fps": 16})

    def test_changed_workflow_fails_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            graph = json.loads((ROOT / "workflows/image_from_prompt_api.json").read_text())
            graph["1001"]["class_type"] = "OtherLoader"
            (path / "image_from_prompt_api.json").write_text(json.dumps(graph))
            with self.assertRaisesRegex(ValueError, "Incompatible workflow"):
                create_generator("image", self.catalog, self.config, path)

    def test_workflow_with_embedded_lora_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            graph = json.loads((ROOT / "workflows/image_from_prompt_api.json").read_text())
            graph["hidden_lora"] = {"class_type": "LoraLoaderModelOnly", "inputs": {
                "model": ["1001", 0], "lora_name": "hidden.safetensors", "strength_model": 1}}
            (path / "image_from_prompt_api.json").write_text(json.dumps(graph))
            with self.assertRaisesRegex(ValueError, "built-in LoRA"):
                create_generator("image", self.catalog, self.config, path)

    def test_runtime_binding_changes_fail_at_factory(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            graph = json.loads((ROOT / "workflows/video_from_image_api.json").read_text())
            del graph["9000"]["inputs"]["image"]
            (path / "video_from_image_api.json").write_text(json.dumps(graph))
            with self.assertRaisesRegex(ValueError, "Incompatible workflow"):
                create_generator("video", self.catalog, self.config, path)

    def test_invalid_duration_does_not_submit_job(self):
        calls = []
        execute = lambda *args: calls.append(args)
        with self.assertRaisesRegex(ValueError, "duration"):
            self.generator("video", execute=execute).generate(VideoRequest(start_image="in.png", prompt="turn", negative_prompt="", seconds=-1, seed=1, refine_seed=2, output_prefix="video", sub_dir="run", debug_dir=ROOT))
        self.assertEqual(calls, [])

    def test_result_and_metadata_are_reproducible(self):
        gen = self.generator("image")
        original = gen.metadata()["signature"]
        submitted = []
        gen = self.generator("image", execute=lambda url, graph, kind, extensions, context: submitted.append(graph) or Path("image.png"))
        result = gen.generate(ImageRequest(prompt="captain", seed=12, output_prefix="run/start_image", sub_dir="run", debug_dir=ROOT))
        self.assertEqual(result.path, Path("image.png"))
        self.assertEqual(result.metadata["signature"], original)
        self.assertEqual(gen.metadata()["signature"], original)
        self.assertNotEqual(original, self.generator("image", image_generation={"guidance": 2}).metadata()["signature"])
        self.assertEqual(submitted[0]["1004"]["inputs"]["text"], "captain")

    def test_generator_owns_connection_from_config(self):
        calls = []
        context = ImageRequest(prompt="", seed=0, output_prefix="run/start_image", sub_dir="run", debug_dir=ROOT)
        gen = self.generator("image", comfy_url="http://127.0.0.1:8190",
                             execute=lambda *args: calls.append(args) or Path("image.png"))
        gen.generate(ImageRequest(prompt="captain", seed=1, output_prefix="run/start_image", sub_dir=context.sub_dir, debug_dir=context.debug_dir))
        self.assertEqual(gen.client.url, "http://127.0.0.1:8190")
        self.assertEqual(calls[0][0], gen.client.url)
        self.assertEqual(calls[0][-1].sub_dir, context.sub_dir)

    def test_constructing_generator_does_not_contact_service(self):
        from unittest.mock import patch
        with patch("core.comfyui.requests.post", side_effect=AssertionError("unexpected request")):
            gen = self.generator("image")
            self.assertEqual(gen.client.url, self.config["comfy_url"])

    def test_llm_template_patches_loader_limits_and_sampling(self):
        gen = self.generator("llm", llm_generation={"n_ctx": 8192, "max_tokens": 2048, "parameters": {"temperature": .25}})
        graph = gen.build_workflow(LlmRequest(system_prompt="system", prompt="question", seed=123, response_path=ROOT / "response.txt", sub_dir="probe", debug_dir=Path(".")))
        self.assertEqual(graph["1"]["inputs"]["model"], "Qwen2.5-14B-Instruct-Q5_K_M.gguf")
        self.assertEqual(graph["1"]["inputs"]["n_ctx"], 8192)
        self.assertEqual(graph["4"]["inputs"]["max_tokens"], 2048)
        self.assertEqual(graph["4"]["inputs"]["temperature"], .25)
        self.assertEqual(graph["2"]["inputs"]["system_prompt"], "system")
        self.assertEqual(graph["2"]["inputs"]["custom_prompt"], "question")
        self.assertEqual(graph["9101"]["class_type"], "llama_cpp_unload_model")

    def test_llm_executes_and_reads_only_fresh_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            response = root / "plan_response.txt"
            response.write_text("stale response")
            gen = self.generator("llm")
            seen = []
            def run(graph, graph_path, history_path, reason):
                self.assertFalse(response.exists())
                self.assertEqual(graph["3"]["inputs"]["path"], str(response.resolve()))
                response.write_text('{"scene_summary":"captain"}')
                seen.append(history_path)
            gen.client.run = run
            result = gen.generate(LlmRequest(system_prompt="system", prompt="question", seed=0, response_path=response, sub_dir="plan", debug_dir=root))
            self.assertEqual(json.loads(result.text)["scene_summary"], "captain")
            self.assertEqual(seen, [root / "plan_history.json"])
            self.assertTrue((root / "plan_generator.json").exists())

    def test_video_generator_uploads_local_image_without_runner_service_calls(self):
        gen = self.generator("video")
        seen = []
        gen.client.upload = lambda path, subfolder: seen.append(path) or "uploaded/start.png"
        gen.client.execute = lambda graph, kind, extensions, context: seen.append(graph["9000"]["inputs"]["image"]) or Path("video.mp4")
        result = gen.generate(VideoRequest(start_image=Path("local.png"), prompt="turn", negative_prompt="text", seconds=5, seed=1, refine_seed=2, output_prefix="run/video", sub_dir="run", debug_dir=ROOT))
        self.assertEqual(seen, [Path("local.png"), "uploaded/start.png"])
        self.assertEqual(result.path, Path("video.mp4"))

    def test_song_context_cache_tracks_llm_template(self):
        from aligned_song_video_runner import get_or_create_song_context
        from core.generator import LlmResult
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gen = self.generator("llm")
            clean = root / "song_context.json"
            clean.write_text('{"song_summary":"cached"}')
            record = root / "song_context_generator.json"
            record.write_text(json.dumps(gen.metadata()))
            gen.generate = lambda *args: self.fail("matching cache must not generate")
            self.assertEqual(get_or_create_song_context(gen, {}, "", [], root)["song_summary"], "cached")
            record.write_text('{"signature":"other-template"}')
            seen = []
            gen.generate = lambda request: seen.append(request) or LlmResult('{"song_summary":"fresh"}', gen.metadata())
            with patch("aligned_song_video_runner.build_song_context_prompt", return_value="question"):
                result = get_or_create_song_context(gen, {"song_context_system.txt": "system"}, "", [], root)
            self.assertEqual(result["song_summary"], "fresh")
            self.assertEqual(seen[0].prompt, "question")

    def test_service_client_saves_graph_and_propagates_failed_history(self):
        from core.comfyui import ComfyUIClient
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = ComfyUIClient("http://localhost:8188", root)
            history = {"status": {"status_str": "error", "messages": ["failed"]}}
            with patch("core.comfyui.free_comfy_memory"), patch("core.comfyui.queue_prompt", return_value=("prompt", "client")), patch("core.comfyui.wait_history", return_value=history):
                with self.assertRaisesRegex(RuntimeError, "did not finish successfully"):
                    client.run({"node": {}}, root / "graph.json", root / "history.json", "test")
            self.assertEqual(json.loads((root / "graph.json").read_text()), {"node": {}})
            self.assertEqual(json.loads((root / "history.json").read_text()), history)

    def test_service_client_requires_result_from_current_history(self):
        from core.comfyui import ComfyUIClient
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run").mkdir()
            stale = root / "run/start_image_00001.png"
            stale.write_bytes(b"stale")
            client = ComfyUIClient("http://localhost:8188", root)
            client.run = lambda *args: {"outputs": {}}
            context = ImageRequest(prompt="", seed=0, output_prefix="run/start_image", sub_dir="run", debug_dir=root)
            with self.assertRaisesRegex(RuntimeError, "result not found"):
                client.execute({}, "image", {".png"}, context)
            client.run = lambda *args: {"outputs": {"save": {"images": [{"filename": stale.name, "subfolder": "run"}]}}}
            self.assertEqual(client.execute({}, "image", {".png"}, context), stale)



if __name__ == "__main__":
    unittest.main()
