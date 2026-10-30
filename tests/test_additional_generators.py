"""Offline checks for phase-two graph assembly and AUTOMATIC1111 execution."""
import base64
from dataclasses import replace
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

from core.automatic1111 import Automatic1111Client
from core.generator import ImageRequest, VideoRequest, create_generator

ROOT = Path(__file__).resolve().parents[1]


class AdditionalGeneratorTests(unittest.TestCase):
    def test_runner_startup_accepts_wan_through_common_contract(self):
        import aligned_song_video_runner as runner
        class ReachedAlignment(Exception):
            pass
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            input_dir = root / "input"
            input_dir.mkdir()
            (input_dir / "video_style.txt").write_text("Illustrated space captain", encoding="utf-8")
            (input_dir / "config.json").write_text(json.dumps({
                "image_generation": {"template": "sdxl_comix_b3"},
                "video_generation": {"template": "wan22_i2v_lightx2v"}}), encoding="utf-8")
            with patch("sys.argv", ["runner", "--input-dir", str(input_dir), "--output-dir", str(root / "output"), "--limit", "1"]), \
                 patch.object(runner, "resolve_ffmpeg_command", return_value="ffmpeg"), \
                 patch.object(runner, "resolve_ffprobe_command", return_value="ffprobe"), \
                 patch.object(runner, "resolve_stable_ts_command", return_value="stable-ts"), \
                 patch.object(runner, "log"), \
                 patch.object(runner, "ensure_alignment_artifact", side_effect=ReachedAlignment), \
                 patch("requests.Session.post", side_effect=AssertionError("unexpected service call")):
                with self.assertRaises(ReachedAlignment):
                    runner.main()
            metadata = json.loads((root / "output/work/debug/generation_settings.json").read_text())
            self.assertEqual(metadata["video"]["type"], "wan")
            self.assertEqual(metadata["image"]["type"], "sdxl")

    def generator(self, kind, template, **overrides):
        config = json.loads((ROOT / "data/config.json").read_text())
        config[f"{kind}_generation"] = {"template": template, **overrides}
        catalog = json.loads((ROOT / "data/model_templates.json").read_text())
        return create_generator(kind, catalog, config, ROOT / "workflows")

    def image(self):
        return ImageRequest(prompt="captain", negative_prompt="blur", seed=123,
                            output_prefix="run/start_image", sub_dir="run", debug_dir=ROOT)

    def video(self, seconds=5):
        return VideoRequest(prompt="captain turns", negative_prompt="blur", seed=123,
                            refine_seed=456, output_prefix="run/video", sub_dir="run",
                            debug_dir=ROOT, start_image=Path("start.png"), seconds=seconds)

    def test_flux1_loras_connect_both_model_and_text_encoder(self):
        gen = self.generator("image", "flux1_dev", loras=[
            {"name": "first.safetensors", "weight": .7},
            {"name": "second.safetensors", "weight": .5}])
        before = gen.metadata()
        graph = gen.build_workflow(replace(self.image(), width=768, height=512))
        self.assertEqual(graph["generation_lora_1"]["inputs"]["model"], ["generation_lora_0", 0])
        self.assertEqual(graph["generation_lora_1"]["inputs"]["clip"], ["generation_lora_0", 1])
        self.assertEqual(graph["ksampler"]["inputs"]["model"], ["generation_lora_1", 0])
        self.assertEqual(graph["positive_text"]["inputs"]["clip"], ["generation_lora_1", 1])
        self.assertEqual(graph["ksampler"]["inputs"]["seed"], 123)
        self.assertEqual(graph["empty_latent"]["inputs"]["width"], 768)
        self.assertEqual(gen.metadata(), before)

    def test_flux1_without_loras_and_krea_checkpoint(self):
        gen = self.generator("image", "flux1_krea")
        graph = gen.build_workflow(self.image())
        self.assertFalse(any(node["class_type"].startswith("LoraLoader") for node in graph.values()))
        self.assertEqual(graph["ksampler"]["inputs"]["model"], ["unet_loader", 0])
        self.assertEqual(graph["unet_loader"]["inputs"]["unet_name"], "flux1-krea-dev_fp8_scaled.safetensors")
        self.assertEqual(graph["ksampler"]["inputs"]["positive"], ["guidance", 0])

    def test_wan_stage_chains_and_noise_boundaries(self):
        gen = self.generator("video", "wan22_i2v_standard", loras=[
            {"name": "high", "weight": .7, "stage": "high"},
            {"name": "both", "weight": .5, "stage": "both"}])
        graph = gen.build_workflow(self.video())
        self.assertEqual(graph["generation_lora_high_lora_1"]["inputs"]["model"], ["generation_lora_high", 0])
        self.assertEqual(graph["generation_lora_low"]["inputs"]["lora_name"], "both")
        high, low = (graph[node]["inputs"] for node in ("sampler_stage1", "sampler_stage2"))
        self.assertEqual((high["start_at_step"], high["end_at_step"], low["start_at_step"], low["end_at_step"]), (0, 20, 20, 40))
        self.assertEqual((high["add_noise"], low["add_noise"]), ("enable", "disable"))
        self.assertEqual((high["return_with_leftover_noise"], low["return_with_leftover_noise"]), ("enable", "disable"))
        self.assertEqual((high["noise_seed"], low["noise_seed"]), (123, 123))

    def test_wan_duration_rounds_up_to_four_k_plus_one(self):
        gen = self.generator("video", "wan22_i2v_lightx2v")
        for seconds in (.01, .25, 1, 5.25, 20):
            with self.subTest(seconds=seconds):
                graph = gen.build_workflow(self.video(seconds))
                frames = graph["wan_i2v"]["inputs"]["length"]
                self.assertEqual((frames - 1) % 4, 0)
                self.assertGreaterEqual(frames / 24, seconds)
                self.assertEqual(graph["create_video"]["inputs"]["fps"], 24)
        self.assertEqual(gen.build_workflow(self.video(20))["wan_i2v"]["inputs"]["length"], 481)

    def test_wan_invalid_settings_and_request_fail_before_submission(self):
        for overrides in ({"loras": []}, {"split_step": 4}, {"loras": [{"name": "bad", "weight": 1}]}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.generator("video", "wan22_i2v_lightx2v", **overrides)
        gen = self.generator("video", "wan22_i2v_standard")
        gen.client.upload, gen.client.execute = Mock(), Mock()
        for request in (replace(self.video(), seconds=0), replace(self.video(), width=776)):
            with self.assertRaises(ValueError):
                gen.generate(request)
        gen.client.upload.assert_not_called()
        gen.client.execute.assert_not_called()
        self.assertEqual(gen.configured_graph["sampling_low"]["inputs"]["model"], ["unet_low", 0])

    def test_wan_vfi_algorithms_preserve_sampler_and_connect_after_decode(self):
        from core.generator import WanGenerator, WanVfiGenerator
        for algorithm, node_type in (("film", "FILM VFI"), ("rife", "RIFE VFI")):
            with self.subTest(algorithm=algorithm):
                gen = self.generator("video", f"wan22_i2v_lightx2v_{algorithm}_x2")
                self.assertIsInstance(gen, WanVfiGenerator)
                self.assertIsInstance(gen, WanGenerator)
                graph = gen.build_workflow(self.video(7.64))
                self.assertEqual(graph["vfi"]["class_type"], node_type)
                self.assertEqual(graph["vfi"]["inputs"]["frames"], ["vae_decode", 0])
                self.assertEqual(graph["vfi"]["inputs"]["multiplier"], 2)
                self.assertEqual(graph["create_video"]["inputs"]["images"], ["vfi", 0])
                self.assertEqual(graph["create_video"]["inputs"]["fps"], 24)
                self.assertEqual(graph["wan_i2v"]["inputs"]["length"], 93)
                self.assertEqual(graph["sampler_stage1"]["inputs"]["steps"], 4)

    def test_wan_vfi_rounding_covers_duration_including_exact_native_boundaries(self):
        for algorithm in ("film", "rife"):
            gen = self.generator("video", f"wan22_i2v_lightx2v_{algorithm}_x2")
            before = gen.metadata()
            for fps in (16, 24, 29.97):
                for seconds in (.01, 1, 65 / 12, 7.64, 10, 15.28, 20):
                    with self.subTest(algorithm=algorithm, fps=fps, seconds=seconds):
                        graph = gen.build_workflow(replace(self.video(seconds), fps=fps))
                        frames = graph["wan_i2v"]["inputs"]["length"]
                        self.assertEqual((frames - 1) % 4, 0)
                        self.assertGreaterEqual((2 * frames - 1) / fps, seconds)
                        self.assertEqual(graph["create_video"]["inputs"]["fps"], fps)
                        if frames > 5:
                            self.assertLess((2 * (frames - 4) - 1) / fps, seconds)
            self.assertEqual(gen.metadata(), before)

    def test_wan_vfi_settings_fail_before_upload(self):
        for overrides in ({"type": "unknown"}, {"checkpoint": ""},
                          {"clear_cache_after_n_frames": 0}, {"clear_cache_after_n_frames": 1.5}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.generator("video", "wan22_i2v_lightx2v_film_x2", interpolation=overrides)
        for overrides in ({"dtype": "bad"}, {"batch_size": 65}, {"ensemble": 1}, {"scale_factor": .75}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.generator("video", "wan22_i2v_lightx2v_rife_x2", interpolation=overrides)

    def test_vfi_changes_artifact_signature_and_regular_wan_stays_unchanged(self):
        original = self.generator("video", "wan22_i2v_lightx2v")
        film = self.generator("video", "wan22_i2v_lightx2v_film_x2")
        rife = self.generator("video", "wan22_i2v_lightx2v_rife_x2")
        self.assertEqual(len({g.metadata()["signature"] for g in (original, film, rife)}), 3)
        self.assertNotIn("vfi", original.build_workflow(self.video()))
        self.assertEqual(original.build_workflow(self.video(7.64))["wan_i2v"]["inputs"]["length"], 185)

    def test_sdxl_payload_and_independent_contract(self):
        gen = self.generator("image", "sdxl_comix_b3", loras=[{"name": "style.safetensors", "weight": .7}])
        payload = gen.build_payload(replace(self.image(), width=768, height=512))
        self.assertFalse(hasattr(gen, "build_workflow"))
        self.assertEqual(payload["prompt"], "captain <lora:style:0.7>")
        self.assertEqual(payload["negative_prompt"], "blur")
        self.assertEqual((payload["sampler_name"], payload["scheduler"]), ("DPM++ 2M", "Karras"))
        self.assertEqual(payload["override_settings"]["samples_format"], "png")
        self.assertFalse(payload["override_settings_restore_afterwards"])
        self.assertEqual((payload["width"], payload["height"]), (768, 512))
        gen.client.generate = Mock(return_value=Path("image.png"))
        self.assertEqual(gen.generate(self.image()).metadata["service"], "automatic1111")

    def test_sdxl_recipes_select_requested_checkpoints_and_allow_scheduler_override(self):
        realistic = self.generator("image", "sdxl_cyberrealistic")
        cartoon = self.generator("image", "sdxl_comix_b3", scheduler="Exponential")
        self.assertEqual(realistic.settings["checkpoint"], "cyberrealisticXL_v100.safetensors")
        self.assertEqual(cartoon.settings["checkpoint"], "comix_b3.safetensors")
        self.assertEqual(cartoon.build_payload(self.image())["scheduler"], "Exponential")

    def test_factory_coordinates_both_services_only_when_sdxl_selected(self):
        config = json.loads((ROOT / "data/config.json").read_text())
        catalog = json.loads((ROOT / "data/model_templates.json").read_text())
        config["image_generation"] = {"template": "sdxl_comix_b3"}
        with patch("requests.Session.get", side_effect=AssertionError("unexpected HTTP")), patch("requests.Session.post", side_effect=AssertionError("unexpected HTTP")):
            llm = create_generator("llm", catalog, config, ROOT / "workflows")
            video = create_generator("video", catalog, config, ROOT / "workflows")
            image = create_generator("image", catalog, config, ROOT / "workflows")
        self.assertIsInstance(llm.client.before_generate.__self__, Automatic1111Client)
        self.assertIsInstance(video.client.before_generate.__self__, Automatic1111Client)
        self.assertEqual(image.client.before_generate.__name__, "release_memory")
        self.assertIsNone(self.generator("video", "wan22_i2v_standard").client.before_generate)


class Automatic1111Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.release = Mock()
        self.client = Automatic1111Client("http://localhost:7860", self.root / "images", self.release)
        self.session = Mock()
        self.client.session = self.session
        def get(url, **kwargs):
            data = {"sd-models": [{"title": "model [hash]", "model_name": "model", "filename": "C:\\models\\model.safetensors"}],
                    "samplers": [{"name": "DPM++ 2M"}], "schedulers": [{"name": "karras", "label": "Karras"}], "loras": [{"name": "style", "alias": "alias"}]}
            return Mock(json=Mock(return_value=data[url.rsplit("/", 1)[-1]]))
        self.session.get.side_effect = get
        self.png = b"\x89PNG\r\n\x1a\n" + b"\0\0\0\rIHDR" + struct.pack(">II", 768, 512)
        self.session.post.return_value = Mock(json=Mock(return_value={"images": [base64.b64encode(self.png).decode()], "info": "ok"}))
        self.payload = {"prompt": "captain <lora:style:0.7>", "sampler_name": "DPM++ 2M", "scheduler": "Karras", "width": 768, "height": 512,
                        "override_settings": {"sd_model_checkpoint": "model.safetensors", "sd_vae": "Automatic"}}

    def test_generation_resolves_checkpoint_saves_debug_and_releases_gpu(self):
        path = self.client.generate(self.payload, "run/start_image", self.root / "debug")
        self.assertEqual(path.read_bytes(), self.png)
        posted = [call.args[0].rsplit("/", 1)[-1] for call in self.session.post.call_args_list]
        self.assertEqual(posted, ["options", "reload-checkpoint", "txt2img", "unload-checkpoint"])
        self.release.assert_called_once_with()
        sent = self.session.post.call_args_list[2].kwargs["json"]
        self.assertEqual(sent["override_settings"]["sd_model_checkpoint"], "model [hash]")
        self.assertEqual(self.payload["override_settings"]["sd_model_checkpoint"], "model.safetensors")
        self.assertNotIn("images", json.loads((self.root / "debug/image_response.json").read_text()))

    def test_unknown_assets_fail_without_loading_any_model(self):
        for field, value in (("sampler_name", "missing"), ("scheduler", "missing"), ("prompt", "<lora:missing:1>")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.client.generate({**self.payload, field: value}, "image", self.root)
        self.payload["override_settings"]["sd_model_checkpoint"] = "missing"
        with self.assertRaises(ValueError):
            self.client.generate(self.payload, "image", self.root)
        self.session.post.assert_not_called()
        self.release.assert_not_called()

    def test_bad_output_releases_model_without_saving_image(self):
        self.session.post.return_value.json.return_value = {"images": [base64.b64encode(b"jpeg").decode()]}
        with self.assertRaisesRegex(RuntimeError, "PNG"):
            self.client.generate(self.payload, "image", self.root)
        self.assertTrue(self.session.post.call_args.args[0].endswith("/unload-checkpoint"))
        self.assertFalse((self.root / "images/image.png").exists())

    def test_wrong_size_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "dimensions"):
            self.client.generate({**self.payload, "width": 512}, "image", self.root)

    def test_cleanup_error_preserves_generation_error(self):
        def post(url, **kwargs):
            if url.endswith("txt2img"):
                raise RuntimeError("generation failed")
            if url.endswith("unload-checkpoint"):
                raise RuntimeError("cleanup failed")
            return Mock()
        self.session.post.side_effect = post
        with self.assertLogs(level="ERROR"), self.assertRaisesRegex(RuntimeError, "generation failed"):
            self.client.generate(self.payload, "image", self.root)

    def test_output_prefix_cannot_escape_service_directory(self):
        with self.assertRaises(ValueError):
            self.client.generate(self.payload, "../escape", self.root)
        self.session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
