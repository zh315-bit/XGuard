"""为介绍视频合成背景配乐（A 小调，120 BPM）。

转场和重音按 index.html 的动画时间书写，经 to_video 映射到成片时间（旁白会把部分场景拉长）。
由 build.py 调用 make_music()。
"""
import numpy as np

SR = 44100
BPM = 120
BEAT = 60 / BPM
STEP = BEAT / 4
# A 小调：Am – F – C – G，每个和弦 2 小节（4 s）
CHORDS = [[57, 60, 64], [53, 57, 60], [48, 52, 55], [55, 59, 62]]
ROOTS = [45, 41, 48, 43]
SEG = 8 * BEAT


def midi(n):
    return 440.0 * 2 ** ((n - 69) / 12)


def tone(freq, dur, harmonics=4, detune=0.0):
    t = np.arange(int(dur * SR)) / SR
    out = np.zeros_like(t)
    for h in range(1, harmonics + 1):
        out += np.sin(2 * np.pi * freq * h * t * (1 + detune)) / h ** 1.6
    return out


def env(n, attack, release):
    e = np.ones(n)
    a, r = int(attack * SR), int(release * SR)
    if a:
        e[:a] = np.linspace(0, 1, a)
    if r:
        e[-r:] *= np.linspace(1, 0, r)
    return e


def make_music(to_video, dur):
    """返回 (N, 2) 的立体声数组；to_video 把动画时间映射到成片时间。"""
    n_total = int(SR * dur)
    L = np.zeros(n_total)
    R = np.zeros(n_total)
    rng = np.random.default_rng(7)

    def add(sig, t0, gain=1.0, pan=0.0):
        i0 = int(t0 * SR)
        if i0 >= n_total or i0 < 0:
            return
        sig = sig[: n_total - i0]
        L[i0 : i0 + len(sig)] += sig * gain * (1 - pan) / 2 ** 0.5
        R[i0 : i0 + len(sig)] += sig * gain * (1 + pan) / 2 ** 0.5

    def bandnoise(d, lo, hi):
        n = int(d * SR)
        spec = np.fft.rfft(rng.standard_normal(n))
        f = np.fft.rfftfreq(n, 1 / SR)
        spec[(f < lo) | (f > hi)] = 0
        x = np.fft.irfft(spec, n)
        return x / (np.abs(x).max() + 1e-9)

    def pluck(freq, d=0.35):
        s = tone(freq, d, harmonics=3)
        return s * np.exp(-np.arange(len(s)) / SR * 11)

    def kick():
        n = int(0.45 * SR)
        tt = np.arange(n) / SR
        f = 45 + 110 * np.exp(-tt * 28)
        return np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt * 7)

    def boom(gain):
        n = int(1.6 * SR)
        tt = np.arange(n) / SR
        f = 38 + 80 * np.exp(-tt * 10)
        return gain * np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-tt * 2.5)

    # 段落边界（成片时间）：开场轻、痛点半速、正文满、收尾收
    t_problem, t_main, t_outro, t_logo = to_video(5.4), to_video(11.6), to_video(57), to_video(59.8)
    end = dur - 1.0

    def level(t):
        if t < t_problem:
            return 0
        if t < t_main:
            return 1
        if t < t_outro:
            return 2
        return 3

    # ---------- Pad ----------
    t, k = 0.0, 0
    while t < dur:
        notes = CHORDS[k % 4]
        for n in notes + [notes[0] + 12]:
            for det, pan in ((-0.003, -0.6), (0.003, 0.6)):
                s = tone(midi(n), SEG + 1.0, harmonics=5, detune=det)
                s *= env(len(s), 1.2, 1.4)
                add(s, t, 0.045, pan)
        t += SEG
        k += 1

    # ---------- 鼓、贝斯、琶音（16 分音符网格） ----------
    KICK = kick()
    HAT = bandnoise(0.08, 7000, 16000)
    HAT *= np.exp(-np.arange(len(HAT)) / SR * 60)
    for i in range(int(dur / STEP)):
        tt = i * STEP
        lv = level(tt)
        if lv == 0 or tt > end:
            continue
        beat_i, sub = divmod(i, 4)
        chord = CHORDS[int(tt // SEG) % 4]
        root = ROOTS[int(tt // SEG) % 4]
        fade = 1.0 if tt < t_logo else max(0.0, 1 - (tt - t_logo) / 2)
        if sub == 0 and (lv == 2 or (lv == 1 and beat_i % 2 == 0) or (lv == 3 and tt < t_logo)):
            add(KICK, tt, 0.55 * fade)
        if lv == 2 and sub == 2:
            add(HAT, tt, 0.10, 0.3)
        if lv == 2 and sub in (1, 3):
            add(HAT, tt, 0.04, -0.3)
        if lv in (1, 2) and sub in (0, 2):
            b = tone(midi(root), STEP * 2, harmonics=6)
            b *= np.exp(-np.arange(len(b)) / SR * 6) * env(len(b), 0.005, 0.03)
            add(b, tt, 0.16 if lv == 2 else 0.12)
        if lv >= 2:
            pattern = [0, 1, 2, 1, 0, 2, 1, 2]
            n = chord[pattern[i % 8]] + 12 + (12 if (i // 8) % 2 else 0)
            p = pluck(midi(n))
            add(p, tt, 0.07 * fade, -0.4)
            add(p, tt + STEP * 3, 0.03 * fade, 0.5)  # 回声

    # ---------- 转场 whoosh：在每个场景开始前上扬 ----------
    for ts in [5.4, 11.6, 19.6, 27.6, 36.0, 44.6, 52.0, 57.0]:
        d = 1.2
        w = np.concatenate([bandnoise(d / 12, 300 + j * 500, (300 + j * 500) * 3) for j in range(12)])
        w *= np.linspace(0, 1, len(w)) ** 2
        add(w, to_video(ts) - d + 0.1, 0.10)

    # ---------- 重音：HOLD 印章、签名完成、未授权告警、片尾 Logo ----------
    add(boom(0.7), to_video(24.6))
    add(boom(0.6), to_video(47.9))
    add(bandnoise(0.6, 2000, 9000) * np.exp(-np.arange(int(0.6 * SR)) / SR * 8), to_video(47.9), 0.12)
    for j, n in enumerate([76, 81, 88]):
        add(pluck(midi(n), 0.8), to_video(33.75) + j * 0.08, 0.10, (j - 1) * 0.4)
    t_end_logo = to_video(60.0)
    add(boom(0.8), t_end_logo)
    for n in [57, 64, 69, 72, 76]:
        s = tone(midi(n), 3.0, harmonics=4) * np.exp(-np.arange(3 * SR) / SR * 1.2)
        add(s, t_end_logo, 0.06)

    # ---------- 淡入淡出 ----------
    master = np.ones(n_total)
    fi, fo = int(1.0 * SR), int(2.5 * SR)
    master[:fi] = np.linspace(0, 1, fi)
    master[-fo:] = np.linspace(1, 0, fo)
    return np.stack([L, R], axis=1) * master[:, None]
