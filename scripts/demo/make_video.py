"""演示视频流水线：旁白合成（macOS say）→ Playwright 录屏（record_demo.py）→ ffmpeg 按每步时间点混音并编码 mp4。

依赖：pip install playwright imageio-ffmpeg && python -m playwright install chromium（macOS 自带 say/afinfo）
用法：python make_video.py <BASE_URL> <WORK_DIR> <OUT_MP4>   例：python make_video.py http://localhost:8000 /tmp/demo ../ScreenLens_demo.mp4
建议先对同一股票池执行一次筛选与回测，让录制时命中缓存。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import imageio_ffmpeg

HERE = Path(__file__).resolve().parent
BASE, WORK, OUT = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
VOICE, RATE = "Flo (中文（中国大陆）)", "225"
FF = imageio_ffmpeg.get_ffmpeg_exe()


def main() -> None:
    audio = WORK / "audio"
    audio.mkdir(parents=True, exist_ok=True)
    steps = json.load(open(HERE / "narration.json", encoding="utf-8"))
    for s in steps:  # 1) 旁白
        f = audio / f"{s['id']}.aiff"
        subprocess.run(["say", "-v", VOICE, "-r", RATE, "-o", str(f), s["say"]], check=True)
        info = subprocess.run(["afinfo", str(f)], capture_output=True, text=True).stdout
        s["dur"] = round(float(re.search(r"estimated duration: ([\d.]+)", info).group(1)), 2)
    json.dump(steps, open(audio / "durations.json", "w", encoding="utf-8"), ensure_ascii=False)
    out_dir = WORK / "out"  # 2) 录屏（字幕、高亮由 record_demo.py 注入页面）
    subprocess.run([sys.executable, str(HERE / "record_demo.py"), BASE, str(audio), str(out_dir)], check=True)
    tl = json.load(open(out_dir / "timeline.json"))
    video = next(out_dir.glob("*.webm"))
    ins = sum((["-i", str(audio / f"{s['id']}.aiff")] for s in tl["steps"]), [])
    parts = [f"[{i}:a]aresample=44100,aformat=channel_layouts=mono,adelay={int(round((s['t'] + 0.15) * 1000))}:all=1[a{i}]"
             for i, s in enumerate(tl["steps"], 1)]
    fc = ";".join(parts) + ";" + "".join(f"[a{i}]" for i in range(1, len(parts) + 1)) + \
        f"amix=inputs={len(parts)}:normalize=0:duration=longest,volume=1.25,alimiter=limit=0.95[aud]"
    subprocess.run([FF, "-loglevel", "error", "-y", "-i", str(video), *ins, "-filter_complex", fc, "-map", "0:v", "-map", "[aud]",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "27", "-pix_fmt", "yuv420p", "-r", "25", "-c:a", "aac", "-b:a", "128k",
                    "-t", f"{tl['total']:.2f}", "-movflags", "+faststart", str(OUT)], check=True)  # 3) 混音编码
    print("written", OUT, "duration", tl["total"])


if __name__ == "__main__":
    main()
