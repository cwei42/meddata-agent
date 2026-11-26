"""Public-dataset evaluation: grades, pie charts and the results page.

Writes docs/img/eval_light.png, docs/img/eval_dark.png and docs/public-eval.md from the grades below.
Grades are by hand, per answer: 1 = correct, 0.5 = partly correct, 0 = wrong or no answer.
Run: python docs/make_eval_chart.py
"""
import os
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))

DATASETS = {
    "03": ("AAPM TG-244 IMRT/VMAT cases", "6 patients, CT + RTSTRUCT (prostate, abdomen, lung, anal, head and neck, thorax)",
           "https://www.aapm.org/pubs/MPPG/TPS/datasets/IMRT-VMAT/"),
    "04": ("DICOM-RT collection on Zenodo", "3 patients, CT, RTPLAN, RTSTRUCT, RTDOSE, one zip inside a zip, references to missing objects",
           "https://zenodo.org/records/5147737"),
    "05": ("dicompyler-core test data", "1 patient, breast boost, CT with one slice, RTSTRUCT, RTPLAN, RTDOSE",
           "https://github.com/dicompyler/dicompyler-core"),
    "06": ("AAPM TG-236 DVH validation", "1 patient, vaginal cylinder brachytherapy, two structure sets on one CT, RTDOSE",
           "https://zenodo.org/records/8339239"),
    "07": ("pydicom RT test files", "4 single objects (CT, RTPLAN, RTDOSE, RTSTRUCT) that belong to 4 different patients",
           "https://github.com/pydicom/pydicom"),
}

QUESTIONS = [
    ("H1", "How many patients, studies and series are in this dataset, and which modalities?"),
    ("H2", "Which RT objects are there (structure sets, plans, doses), and what does each refer to?"),
    ("H3", "List the structures of each structure set with their TG-263 names. Which names could not be mapped?"),
    ("H4", "What is the volume of the PTV (or the largest target) in each structure set?"),
    ("H5", "If there is a dose: what are the PTV D95 and Dmean, using the dose of the same plan?"),
    ("H6", "If there is a dose: which organ at risk gets the highest mean dose?"),
    ("H7", "Is anything wrong or incomplete in this dataset (missing references, single slices, duplicates)?"),
    ("H8", "Does the dataset contain patient names, IDs or birth dates?"),
    ("H9", "Compare the GTV (or CTV) with the PTV: Dice and volumes."),
    ("H10", "Export the structures as MHA masks."),
]

# category -> questions
CATEGORIES = [
    ("Data organization", ["H1", "H2", "H7"]),
    ("Anonymization / identifiers", ["H8"]),
    ("TG-263 names", ["H3"]),
    ("Structure analysis", ["H4", "H9"]),
    ("Dose", ["H5", "H6"]),
    ("MHA export", ["H10"]),
]

# run -> (label, note, {dataset: [H1..H10]})
RUNS = [
    ("0.11.1", "step limit 10, select='OARS' / 'TARGETS', \"can you\" as dry run", {
        "03": [1, 1, 1, .5, 1, 1, 1, 1, 1, 1],
        "04": [1, 1, 1, .5, 1, 1, .5, 1, 1, 1],
        "05": [1, 1, 1, .5, 1, .5, 1, 1, .5, 1],
        "06": [1, 1, 1, 1, 1, .5, .5, 1, 1, 1],
        "07": [.5, 1, .5, 1, 1, 1, .5, 1, 1, 1]}),
]

# why an answer of the current version is only partly correct
NOTES_CURRENT = {
    ("03", "H4"): "P006 left out, although it has a PTV",
    ("04", "H4"): "said P001 has no structure set (it has one, with only PROSTATE)",
    ("04", "H7"): "listed the unresolved references, missed the RTSTRUCT whose image is not in the data",
    ("05", "H4"): "took the helper structure Tumor Bed Block for the target",
    ("05", "H6"): "called the breast an organ at risk in a breast boost plan",
    ("05", "H9"): "offered to compare the tumor bed with itself",
    ("06", "H6"): "called the vagina an organ at risk in a vaginal cylinder plan",
    ("06", "H7"): "said nothing is wrong, did not mention the second structure set on the same CT",
    ("07", "H1"): "reported one study instead of four",
    ("07", "H3"): "wrote External as the original name (it is 'patient')",
    ("07", "H7"): "did not notice that the four objects belong to four different patients",
}

STATUS = [("correct", "#0ca30c"), ("partly correct", "#fab219"), ("wrong / no answer", "#d03b3b")]
THEMES = {"light": dict(bg="#fcfcfb", ink="#0b0b0b", ink2="#52514e"),
          "dark": dict(bg="#1a1a19", ink="#f0efec", ink2="#c3c2b7")}
QIDX = {q: i for i, (q, _) in enumerate(QUESTIONS)}


def counts(runs, qs):
    c = [0, 0, 0]
    for _, _, grades in runs:
        for g in grades.values():
            for q in qs:
                v = g[QIDX[q]]
                c[0 if v == 1 else 1 if v == .5 else 2] += 1
    return c


def pie(ax, title, c, th):
    parts = [(n, col) for n, (_, col) in zip(c, STATUS) if n]
    ax.pie([n for n, _ in parts], colors=[col for _, col in parts], startangle=90, counterclock=False,
           wedgeprops=dict(edgecolor=th["bg"], linewidth=2))
    n = sum(c)
    ax.text(0, -1.3, f"{c[0]} of {n} correct\n{c[1]} partly, {c[2]} wrong", ha="center", va="top",
            fontsize=8.5, color=th["ink2"], linespacing=1.4)
    ax.set_title(title, fontsize=9.5, color=th["ink"], pad=4)
    ax.set_aspect("equal")


def draw(mode):
    th = THEMES[mode]
    fig = plt.figure(figsize=(11, 3.5), facecolor=th["bg"])
    rows = [("5 public datasets, 10 questions each (50 answers)", RUNS)]
    for r, (head, runs) in enumerate(rows):
        y = 0.86
        fig.text(0.015, y, head, fontsize=12, weight="bold", color=th["ink"])
        for k, (cat, qs) in enumerate(CATEGORIES):
            pie(fig.add_axes([0.01 + k * 0.165, y - 0.62, 0.145, 0.48]), cat, counts(runs, qs), th)
    for j, (name, col) in enumerate(STATUS):
        x = 0.6 + j * 0.13
        fig.patches.append(matplotlib.patches.Rectangle((x, 0.935), 0.012, 0.034, color=col,
                                                        transform=fig.transFigure, figure=fig))
        fig.text(x + 0.017, 0.952, name, fontsize=9, va="center", color=th["ink2"])
    out = os.path.join(HERE, "img", f"eval_{mode}.png")
    fig.savefig(out, dpi=150, facecolor=th["bg"])
    plt.close(fig)
    return out


def mark(v):
    return {1: "✓", .5: "½", 0: "✗"}[v]


def page():
    L = ["# Evaluation on public datasets", "",
         "Every answer the agent gave on public datasets, graded by hand: ✓ correct, ½ partly correct, ✗ wrong or "
         "no answer. The grades, the charts and this page are generated by "
         "[make_eval_chart.py](make_eval_chart.py).", "",
         "The 10 questions were written on 2026-11-25, before any of these datasets was downloaded or opened. "
         "Datasets 06 and 07 were never used for tuning. Datasets 03 to 05 were also used during development.", "",
         "## Datasets", "", "| Case | Dataset | Content | Source |", "|---|---|---|---|"]
    for k, (name, what, url) in DATASETS.items():
        L.append(f"| {k} | {name} | {what} | {url} |")
    L += ["", "## Questions and categories", "", "| Id | Question | Category |", "|---|---|---|"]
    cat_of = {q: c for c, qs in CATEGORIES for q in qs}
    for q, text in QUESTIONS:
        L.append(f"| {q} | {text} | {cat_of[q]} |")
    label, note, grades = RUNS[0]
    tot = sum(sum(g) for g in grades.values())
    L += ["", "## Score per category", "", "| Category | Correct | Partly correct | Wrong |", "|---|---|---|---|"]
    for cat, qs in CATEGORIES:
        c = counts(RUNS, qs)
        L.append(f"| {cat} | {c[0]} of {sum(c)} | {c[1]} | {c[2]} |")
    L += ["", "## Every answer", "", f"Version {label}. Total {tot:g} of {10 * len(grades)} points "
          "(correct = 1, partly correct = ½).", "",
          "| Case | " + " | ".join(q for q, _ in QUESTIONS) + " | Score |", "|---|" + "---|" * 11]
    for k, g in grades.items():
        L.append(f"| {k} | " + " | ".join(mark(v) for v in g) + f" | {sum(g):g} |")
    L += ["", "## Why answers are only partly correct", "", "| Case | Question | What went wrong |",
          "|---|---|---|"]
    for (k, q), text in NOTES_CURRENT.items():
        L.append(f"| {k} | {q} | {text} |")
    with open(os.path.join(HERE, "public-eval.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(L) + "\n")


if __name__ == "__main__":
    for m in THEMES:
        print(draw(m))
    page()
    for cat, qs in CATEGORIES:
        print(cat, counts(RUNS, qs))
