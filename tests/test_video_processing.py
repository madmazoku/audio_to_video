"""Real FFmpeg checks for channel routing, alpha, bloom and render caching."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from core.video_processing import (
    VideoProcessing, preview_cache_matches, preview_signature, render_processed_video,
)


def run(command):
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise AssertionError(result.stderr.decode("utf-8", errors="replace"))


class ProcessingCacheTests(unittest.TestCase):
    def test_preview_tracks_mode_sources_and_render_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "audio.wav"
            source.write_bytes(b"audio")
            output = root / "preview.mp4"
            output.write_bytes(b"video")
            settings = VideoProcessing("bar")
            signature = preview_signature(settings, 640, 360, 24, 1, [source])
            self.assertFalse(preview_cache_matches(output, signature))
            output.with_suffix(".render.json").write_text(json.dumps(signature), encoding="utf-8")
            self.assertTrue(preview_cache_matches(output, signature))
            for changed in [
                preview_signature(VideoProcessing("bar-bloom"), 640, 360, 24, 1, [source]),
                preview_signature(settings, 1280, 720, 24, 1, [source]),
                preview_signature(settings, 640, 360, 30, 1, [source]),
                preview_signature(settings, 640, 360, 24, 2, [source]),
            ]:
                self.assertFalse(preview_cache_matches(output, changed))
            source.write_bytes(b"changed audio")
            self.assertFalse(preview_cache_matches(
                output, preview_signature(settings, 640, 360, 24, 1, [source])))
            output.with_suffix(".render.json").write_text("broken", encoding="utf-8")
            self.assertFalse(preview_cache_matches(output, signature))

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            VideoProcessing("unknown")


FFMPEG = shutil.which("ffmpeg")


@unittest.skipUnless(FFMPEG, "FFmpeg is not installed")
class FFmpegProcessingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.audio = cls.root / "stereo.wav"
        cls.video = cls.root / "source.mp4"
        run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
             "aevalsrc=0.7*sin(2*PI*220*t)|0.7*sin(2*PI*8000*t):s=48000:d=1.25",
             "-c:a", "pcm_s16le", str(cls.audio)])
        run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
             "color=c=0x204020:s=640x360:r=24:d=1.25", "-c:v", "libx264", str(cls.video)])
        cls.ass = cls.root / "test subtitle.ass"
        cls.ass.write_text(
            "[Script Info]\nScriptType: v4.00+\nPlayResX: 640\nPlayResY: 360\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
            "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
            "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Arial,30,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
            "0,0,0,0,100,100,0,0,1,1,0,5,0,0,0,1\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
            "Dialogue: 0,0:00:00.00,0:00:01.25,Default,,0,0,0,,Test\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def frame(self, path, time="0.5"):
        return subprocess.check_output([FFMPEG, "-v", "error", "-i", str(path),
                                        "-ss", time, "-frames:v", "1", "-f", "rawvideo",
                                        "-pix_fmt", "rgb24", "pipe:1"])

    def render(self, mode, video=None, audio=None, subtitles=()):
        output = self.root / f"{mode}-{len(list(self.root.glob('*.mp4')))}.mp4"
        render_processed_video(video=video, audio=audio or self.audio, subtitles=subtitles,
                               output=output, width=640, height=360, fps=24,
                               duration=1.25, settings=VideoProcessing(mode),
                               ffmpeg=FFMPEG, run=run)
        return output

    def test_all_modes_preview_and_final_preserve_center_and_burn_subtitles(self):
        for mode in ("no", "bar", "bar-bloom"):
            for video in (None, self.video):
                with self.subTest(mode=mode, video=video):
                    output = self.render(mode, video, subtitles=[self.ass, self.ass])
                    frame = self.frame(output)
                    self.assertEqual(len(frame), 640 * 360 * 3)
                    # Center away from text retains the original background.
                    pixel = frame[(80 * 640 + 320) * 3: (80 * 640 + 320) * 3 + 3]
                    if video is None:
                        self.assertLess(max(pixel), 5)
                    else:
                        self.assertGreater(pixel[1], pixel[0])
                    center = [frame[(y * 640 + x) * 3] for y in range(140, 220) for x in range(270, 370)]
                    self.assertGreater(max(center), 200, "Subtitles were not burned")
                    frames = subprocess.check_output([FFMPEG, "-v", "error", "-i", str(output),
                                                     "-map", "0:v", "-f", "framemd5", "pipe:1"])
                    self.assertEqual(sum(not line.startswith(b"#") for line in frames.splitlines()), 30)

    def test_stereo_routing_frequency_order_and_bloom(self):
        plain = self.frame(self.render("bar"))
        glow = self.frame(self.render("bar-bloom"))
        def row_peak(frame, xs):
            scores = [sum(frame[(y * 640 + x) * 3 + 2] for x in xs) for y in range(360)]
            return max(range(360), key=scores.__getitem__)
        self.assertGreater(row_peak(plain, range(0, 32)), row_peak(plain, range(608, 640)) + 50)
        self.assertGreater(sum(glow), sum(plain), "Bloom should add a visible halo")
        # Require actual light beyond the maximum bar envelope, rather than
        # accepting an almost imperceptible brightness increase inside the bars.
        halo = [glow[(y * 640 + x) * 3 + 2]
                for y in range(360) for x in (*range(34, 48), *range(592, 606))]
        self.assertGreater(max(halo), 20, "Bloom should visibly extend beyond the spectrum")
        for xs in (range(0, 32), range(608, 640)):
            y = row_peak(plain, xs)
            blue = [plain[(y * 640 + x) * 3 + 2] for x in xs]
            self.assertGreater(max(blue), 180, "Tips should remain opaque")
            self.assertTrue(any(40 <= value <= 90 for value in blue),
                            "Bar bodies should have approximately 25% opacity")

    def test_silence_is_transparent(self):
        silence = self.root / "silence.wav"
        run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
             "anullsrc=r=48000:cl=mono", "-t", "1.25", str(silence)])
        frame = self.frame(self.render("bar-bloom", audio=silence))
        self.assertLessEqual(max(frame), 2)

    def test_subtitle_path_with_apostrophe_and_filter_delimiters(self):
        special = self.root / "it's [a], subtitle.ass"
        special.write_bytes(self.ass.read_bytes())
        self.render("bar", subtitles=[special])

    def test_pulse_stays_in_left_channel_and_returns_to_silence(self):
        pulse = self.root / "pulse.wav"
        run([FFMPEG, "-v", "error", "-f", "lavfi", "-i",
             "aevalsrc=0.7*sin(2*PI*440*t)*between(t\\,0.4\\,0.55)|0:s=48000:d=1.25",
             str(pulse)])
        output = self.render("bar-bloom", audio=pulse)
        self.assertLessEqual(max(self.frame(output, "0.15")), 2)
        active = self.frame(output, "0.5")
        self.assertGreater(max(active), 50)
        right = [active[(y * 640 + x) * 3 + 2] for y in range(360) for x in range(580, 640)]
        self.assertLessEqual(max(right), 2, "Left channel leaked into right visualization")
        # The lossy H.264 encode can leave faint residuals after a bright tip.
        self.assertLessEqual(max(self.frame(output, "1.15")), 8)


if __name__ == "__main__":
    unittest.main()
