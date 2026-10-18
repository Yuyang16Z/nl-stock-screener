"""演示视频流水线：Playwright 录屏（record_demo.py，页面内叠加两行字幕与高亮）→ ffmpeg 编码 mp4（无旁白）。

依赖：pip install playwright imageio-ffmpeg && python -m playwright install chromium
用法：python make_video.py <BASE_URL> <WORK_DIR> <OUT_MP4>   例：python make_video.py http://localhost:8000 /tmp/demo ../ScreenLens_demo.mp4
建议先对同一股票池执行一次筛选与回测，让录制时命中缓存。
"""
import json
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

HERE = Path(__file__).resolve().parent
BASE, WORK, OUT = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])


def main() -> None:
    out_dir = WORK / "out"
    subprocess.run([sys.executable, str(HERE / "record_demo.py"), BASE, str(out_dir)], check=True)
    tl = json.load(open(out_dir / "timeline.json"))
    video = next(out_dir.glob("*.webm"))
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-loglevel", "error", "-y", "-i", str(video), "-an",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "26", "-pix_fmt", "yuv420p", "-r", "25",
                    "-t", f"{tl['total']:.2f}", "-movflags", "+faststart", str(OUT)], check=True)
    print("written", OUT, "duration", tl["total"])


if __name__ == "__main__":
    main()
