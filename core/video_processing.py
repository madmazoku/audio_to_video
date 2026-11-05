"""Shared image processing for release videos and subtitle previews.

Sources, image processing, subtitles and encoding are separate steps. A future
frame processor can replace the image-processing executor with decode -> Python
-> encode pipes, while retaining the source/settings contract and subtitle tail.
No frame transport or plugin framework is needed for the current FFmpeg effects.
"""
from dataclasses import asdict, dataclass
from functools import lru_cache
import json
from pathlib import Path
import subprocess
from typing import Callable, Optional, Sequence


VISUALIZATION_MODES = ("no", "bar", "bar-bloom")
PROCESSING_VERSION = 3


@dataclass(frozen=True)
class VideoProcessing:
    audio_visualization: str = "no"

    def __post_init__(self):
        if self.audio_visualization not in VISUALIZATION_MODES:
            raise ValueError(f"Unknown audio visualization: {self.audio_visualization}")

    def metadata(self, width: int, height: int, fps: int) -> dict:
        bloom = self.audio_visualization == "bar-bloom"
        return {
            **asdict(self), "version": PROCESSING_VERSION,
            "width": width, "height": height, "fps": fps,
            "spectrum": None if self.audio_visualization == "no" else {
                "bar_width": max(3, round(width * .05)),
                "fft_size": 4096, "frequency_scale": "log",
                "amplitude_scale": "log", "averaging": 3,
                "color": "0x50C8FF", "opacity": .25,
                "tip_width": 2, "tip_opacity": 1.0,
                "bloom_sigma": 8 if bloom else 0,
                "bloom_spread": 3 if bloom else 0,
                "bloom_opacity": 1.0 if bloom else 0,
            },
        }


def subtitle_filter(path: Path) -> str:
    # Escape filter option characters, then quote for the filtergraph parser.
    value = path.resolve().as_posix()
    value = value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    value = value.replace("'", "'\\''")
    return f"subtitles='{value}'"


def build_processing_graph(settings: VideoProcessing, width: int, height: int,
                           fps: int, subtitles: Sequence[Path] = ()) -> str:
    """Inputs 0:v and 1:a; output [processed]. Audio remains separately mapped.

    This function builds image processing independently of source selection and
    encoding. Subtitle overlays are always applied after artistic processing.
    """
    graph = [f"[0:v]fps={fps},setpts=PTS-STARTPTS[base]"]
    current = "base"
    spec = settings.metadata(width, height, fps)["spectrum"]
    if spec is not None:
        graph.append("[1:a]aformat=channel_layouts=stereo,asetpts=PTS-STARTPTS,"
                     "channelsplit=channel_layout=stereo[left_audio][right_audio]")
        for side in ("left", "right"):
            mirror = ",hflip" if side == "right" else ""
            graph.append(
                f"[{side}_audio]showfreqs=s={height}x{spec['bar_width']}:r={fps}:"
                f"mode=bar:fscale={spec['frequency_scale']}:ascale={spec['amplitude_scale']}:"
                f"win_size={spec['fft_size']}:averaging={spec['averaging']}:"
                # Remove the silent baseline and the extra rounding pixel left
                # by temporal averaging, restoring width with transparent pad.
                f"colors={spec['color']},crop=iw:ih-2:0:0,"
                "pad=iw:ih+2:0:0:color=black@0,transpose=clock,vflip,format=gbrap,"
                # Find the inward end of each filled row from the same FFT
                # mask. No second FFT, independently animated dots or extra
                # video layer is needed. Mirror only after the tip is defined.
                "geq=r=80:g=200:b=255:"
                f"a='if(gt(alpha(X,Y),0),if(gte(X,W-{spec['tip_width']})+"
                f"lt(alpha(X+{spec['tip_width']},Y),1),255*{spec['tip_opacity']},"
                f"255*{spec['opacity']}),0)':interpolation=nearest,"
                f"format=rgba{mirror}[{side}_raw]"
            )
            layers = [f"{side}_raw"]
            if settings.audio_visualization == "bar-bloom":
                # Add inward padding before blur, preserving the outer anchor.
                padding = 3 * spec['bloom_sigma'] + spec['bloom_spread']
                offset = padding if side == "right" else 0
                graph.append(f"[{side}_raw]pad=iw+{padding}:ih:{offset}:0:color=black@0,"
                             f"split=2[{side}_core][{side}_light]")
                # Emit light from the opaque tips, independently of body opacity.
                # Widen the emitter before blurring: a two-pixel tip alone loses
                # almost all intensity under a broad Gaussian blur. Constant RGB
                # and alpha-only morphology/blur avoid dark fringes.
                spread = "dilation=threshold0=0:threshold1=0:threshold2=0:threshold3=255,"
                graph.append(f"[{side}_light]lutrgb=r=100:g=230:b=255:"
                             "a='if(gte(val,254),255,0)',format=gbrap,"
                             + spread * spec['bloom_spread'] +
                             f"gblur=sigma={spec['bloom_sigma']}:planes=8,format=rgba,"
                             f"colorchannelmixer=aa={spec['bloom_opacity']}[{side}_glow]")
                layers = [f"{side}_glow", f"{side}_core"]
            for layer in layers:
                output = f"with_{layer}"
                x = "0" if side == "left" else "W-w"
                graph.append(f"[{current}][{layer}]overlay=x={x}:y=0:"
                             f"eof_action=pass:repeatlast=0:format=auto[{output}]")
                current = output
    tail = ",".join([*(subtitle_filter(p) for p in subtitles), "format=yuv420p"])
    graph.append(f"[{current}]{tail}[processed]")
    return ";\n".join(graph)


@lru_cache(maxsize=8)
def _available_filters(ffmpeg: str) -> frozenset:
    result = subprocess.run([ffmpeg, "-hide_banner", "-filters"], check=True,
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    return frozenset(parts[1] for line in result.stdout.splitlines()
                     if len(parts := line.split()) >= 3 and "->" in parts[2])


def check_processing_support(ffmpeg: str, settings: VideoProcessing) -> None:
    required = {"fps", "setpts", "format", "subtitles"}
    if settings.audio_visualization != "no":
        required.update({"aformat", "asetpts", "channelsplit", "showfreqs",
                         "crop", "pad", "transpose", "vflip", "hflip", "overlay", "geq"})
    if settings.audio_visualization == "bar-bloom":
        required.update({"pad", "split", "lutrgb", "dilation", "gblur", "colorchannelmixer"})
    missing = required - _available_filters(ffmpeg)
    if missing:
        raise RuntimeError(f"FFmpeg {ffmpeg} lacks required filters for "
                           f"{settings.audio_visualization}: {', '.join(sorted(missing))}")


def preview_signature(settings: VideoProcessing, width: int, height: int, fps: int,
                      duration: float, inputs: Sequence[Path]) -> dict:
    return {"processing": settings.metadata(width, height, fps), "duration": duration,
            "inputs": [{"path": str(p.resolve()), "size": p.stat().st_size,
                        "mtime_ns": p.stat().st_mtime_ns} for p in inputs]}


def preview_cache_matches(output: Path, signature: dict) -> bool:
    try:
        return output.is_file() and output.stat().st_size > 0 and json.loads(
            output.with_suffix(".render.json").read_text(encoding="utf-8")) == signature
    except (OSError, ValueError):
        return False


def render_processed_video(*, video: Optional[Path], audio: Path, subtitles: Sequence[Path],
                           output: Path, width: int, height: int, fps: int,
                           settings: VideoProcessing, ffmpeg: str,
                           run: Callable[[list], None], duration: Optional[float] = None) -> None:
    """Select source, process image, add subtitles, then encode once.

    video=None selects the black preview source and requires an explicit duration.
    The runner owns orchestration; this executor owns the FFmpeg implementation.
    """
    check_processing_support(ffmpeg, settings)
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y"]
    if video is None:
        if duration is None or duration <= 0:
            raise ValueError("A black preview source needs a positive duration")
        command += ["-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r={fps}:d={duration:.9f}"]
    else:
        command += ["-i", str(video)]
    graph = build_processing_graph(settings, width, height, fps, subtitles)
    debug = output.parent / "work" / "debug"
    debug.mkdir(parents=True, exist_ok=True)
    graph_path = debug / f"{output.stem}_filters.txt"
    graph_path.write_text(graph, encoding="utf-8")
    command += ["-i", str(audio), "-filter_complex_script", str(graph_path),
                "-map", "[processed]", "-map", "1:a:0", "-shortest"]
    if duration is not None:
        command += ["-t", f"{duration:.9f}"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                "-b:a", "192k", "-movflags", "+faststart", str(output)]
    run(command)
