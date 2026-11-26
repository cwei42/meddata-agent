"""Memory use on the Orin Nano while the model server runs (README figure).

Measured on 2026-11-26 with the model loaded (Qwen3.5-4B Q4_K_M + vision projector, 12k context):
MemTotal 7.67 GB, llama-server VmRSS 5.60 GB, of which 1.43 GB in its own address space (smaps) and the rest
in GPU buffers, MemAvailable 1.24 GB. Values in GB (10^9 bytes).
Run: python docs/make_memory_chart.py
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
TOTAL = 7.67
PARTS = [  # (label, GB, light colour, dark colour, text on light, text on dark)
    ("Model, GPU buffers\n(weights, vision, context cache)", 4.17, "#2a78d6", "#3987e5", "#ffffff", "#ffffff"),
    ("Model, CPU side\n(prompt cache, runtime)", 1.43, "#86b6ef", "#184f95", "#0b0b0b", "#f0efec"),
    ("System, console\nand DICOM tools", 0.83, "#898781", "#6b6a65", "#ffffff", "#f0efec"),
    ("Free", 1.24, "#e1e0d9", "#2c2c2a", "#0b0b0b", "#f0efec"),
]
THEMES = {"light": dict(bg="#fcfcfb", ink="#0b0b0b", ink2="#52514e", i=0),
          "dark": dict(bg="#1a1a19", ink="#f0efec", ink2="#c3c2b7", i=1)}


def draw(mode):
    th = THEMES[mode]
    fig, ax = plt.subplots(figsize=(10, 2.3), facecolor=th["bg"])
    ax.set_facecolor(th["bg"])
    x = 0
    for label, gb, cl, cd, tl, td in PARTS:
        col, txt = (cl, tl) if th["i"] == 0 else (cd, td)
        ax.barh(0, gb - 0.03, left=x, height=0.62, color=col)
        ax.text(x + gb / 2, 0, f"{gb:.1f} GB", ha="center", va="center", fontsize=11, weight="bold", color=txt)
        ax.text(x + gb / 2, -0.48, label, ha="center", va="top", fontsize=8.5, color=th["ink2"], linespacing=1.3)
        x += gb
    model = PARTS[0][1] + PARTS[1][1]
    ax.annotate("", xy=(0, 0.44), xytext=(model - 0.03, 0.44),
                arrowprops=dict(arrowstyle="<->", color=th["ink"], lw=1))
    ax.text(model / 2, 0.52, f"model server {model:.1f} GB", ha="center", va="bottom", fontsize=10.5,
            color=th["ink"])
    ax.text(0, 0.98, f"Memory of the Orin Nano ({TOTAL:.1f} GB usable, shared by CPU and GPU) with the model loaded",
            transform=ax.transAxes, fontsize=11.5, weight="bold", color=th["ink"], va="bottom")
    ax.set_xlim(0, TOTAL)
    ax.set_ylim(-1.15, 0.8)
    ax.axis("off")
    fig.subplots_adjust(left=0.01, right=0.99, top=0.86, bottom=0.02)
    out = os.path.join(HERE, "img", f"memory_{mode}.png")
    fig.savefig(out, dpi=150, facecolor=th["bg"])
    plt.close(fig)
    return out


if __name__ == "__main__":
    for m in THEMES:
        print(draw(m))
