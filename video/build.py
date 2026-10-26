"""合成两人对白（ElevenLabs TTS）+ 配乐混音，并导出页面用的 timeline.js。

用法：python build.py <工作目录> <输出 wav>
  - 读取 timeline.json：每段场景的聊天对白、两个角色的 ElevenLabs 声音和语速
  - API key 从项目根目录 .env 的 ELEVENLABS_API_KEY 读取；生成过的句子缓存在 video/.tts-cache/，改台词才会重新计费
  - 每句先合成，再按实际时长排进场景；场景太短就拉长（段内动画线性拉伸）
  - 配乐在有人说话时自动压低（ducking）
  - timeline.js 给 index.html：时间映射、气泡台词、两个角色逐帧口型音量
"""
import hashlib
import json
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from elevenlabs import ElevenLabs, VoiceSettings

from music import SR, make_music

HERE = Path(__file__).resolve().parent
CACHE = HERE / ".tts-cache"
FPS = 30


def read_wav(path):
    with wave.open(str(path)) as f:
        return np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype(float) / 32768


def synth(client, work, name, text, model, v, tempo):
    key = hashlib.sha256(json.dumps([model, v, text], sort_keys=True).encode()).hexdigest()[:16]
    mp3, wav = CACHE / f"{key}.mp3", work / f"{name}.wav"
    if not mp3.exists():
        audio = client.text_to_speech.convert(
            text=text,
            voice_id=v["voice_id"],
            model_id=model,
            voice_settings=VoiceSettings(stability=v["stability"], similarity_boost=v["similarity_boost"]),
            output_format="mp3_44100_128",
        )
        mp3.write_bytes(b"".join(audio))
    # atempo 加快语速但不变调（eleven_v4 不支持 speed 参数）
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp3, "-af", f"atempo={tempo}", "-ar", str(SR), "-ac", "1", wav], check=True)
    x = read_wav(wav)
    loud = np.nonzero(np.abs(x) > 0.02)[0]
    if len(loud):
        x = x[max(0, loud[0] - int(0.03 * SR)) : loud[-1] + int(0.05 * SR)]
    return x


def main(work, out_wav):
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(exist_ok=True)
    load_dotenv(HERE.parent / ".env")
    # SDK 在 import 时读取默认 key，所以加载 .env 之后显式从环境变量传入
    client = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])
    tl = json.loads((HERE / "timeline.json").read_text())
    anim = tl["anim"]

    # 先合成所有台词，再决定每段时长
    clips = []
    for i, seg in enumerate(tl["segments"]):
        row = []
        for j, line in enumerate(seg):
            v = tl["voices"][line["who"]]
            row.append(synth(client, work, f"s{i}_{j}", line.get("say", line["text"]), tl["model"], v, tl.get("tempo", 1.0)))
        clips.append(row)

    video = []
    for i, row in enumerate(clips):
        talk = tl["lead"] + sum(len(x) / SR for x in row) + tl["gap"] * (len(row) - 1) + tl["tail"]
        video.append(round(max(anim[i + 1] - anim[i], talk), 3))
    vstart = np.concatenate([[0], np.cumsum(video)]).tolist()
    dur = vstart[-1]

    def to_video(ta):
        for i in range(len(video)):
            if ta <= anim[i + 1] or i == len(video) - 1:
                return vstart[i] + (ta - anim[i]) * video[i] / (anim[i + 1] - anim[i])

    n_total = int(SR * dur)
    tracks = {who: np.zeros(n_total) for who in tl["voices"]}
    cues = []
    for i, (seg, row) in enumerate(zip(tl["segments"], clips)):
        t = vstart[i] + tl["lead"]
        for line, x in zip(seg, row):
            i0 = int(t * SR)
            tracks[line["who"]][i0 : i0 + len(x)] += x[: n_total - i0]
            cues.append({"who": line["who"], "s": round(t, 3), "e": round(t + len(x) / SR, 3), "text": line["text"]})
            t += len(x) / SR + tl["gap"]

    voice = sum(tracks.values())
    music = make_music(to_video, dur)
    win = int(0.25 * SR)
    env = np.convolve(np.abs(voice), np.ones(win) / win, mode="same")
    duck = 1 - 0.55 * np.clip(env / 0.03, 0, 1)
    # 两个角色左右略微分开，和画面站位一致
    pan = {"alex": -0.25, "xguard": 0.25}
    mix = music * duck[:, None] * 1.1
    for who, tr in tracks.items():
        mix += np.stack([tr * (1 - pan[who]), tr * (1 + pan[who])], axis=1) * 0.85
    mix = np.tanh(mix * 1.1)
    mix /= np.abs(mix).max() / 0.89
    with wave.open(str(out_wav), "wb") as f:
        f.setnchannels(2)
        f.setsampwidth(2)
        f.setframerate(SR)
        f.writeframes((mix * 32767).astype("<i2").tobytes())

    # 逐帧口型音量（0–1），每个角色一条
    hop = SR // FPS
    amp = {}
    for who, tr in tracks.items():
        a = np.array([np.sqrt(np.mean(tr[k : k + hop] ** 2)) for k in range(0, n_total, hop)])
        amp[who] = np.round(np.clip(a / (a.max() or 1) * 1.4, 0, 1), 2).tolist()

    data = {"anim": anim, "vstart": vstart, "video": video, "dur": dur, "fps": FPS, "amp": amp, "cues": cues}
    (HERE / "timeline.js").write_text("// 由 build.py 生成，勿手改\nwindow.TL = " + json.dumps(data, separators=(",", ":")) + ";\n")
    print(f"duration {dur:.2f}s, segments {video}")


if __name__ == "__main__":
    main(*sys.argv[1:3])
