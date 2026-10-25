"""compare_print_strategies на 7 моделях (без LLM и слайсера): нагрузки так, как их вынул бы из ТЗ агент.

Запуск из корня: python devtools/strength_table.py [--out strength_table.md]
"""

import argparse
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "plugin", "AIAssistant"))

from agent.tools import build_registry  # noqa: E402
from agent.tools.registry import ToolContext  # noqa: E402

# префикс папки -> (маска файла, нагрузки, material_id, об/мин); loads None -> инструмент не вызывается
CASES = [
    ("1.", "Шестерня.stl", [{"type": "gear_tooth"}, {"type": "torsion"}], "fiberpart_abspa_g8", None),
    ("2.", "Колесо.STL", [{"type": "radial_load"}, {"type": "torsion"}], "enduse_tpu_d70", 450),
    ("3.", None, None, None, None),
    ("4.", None, None, None, None),
    ("5.", "Рабочее_колесо*.STL", [{"type": "centrifugal"}], "fiberpart_pp_g30", 2900),
    ("6.", "Крыльчатка*.STL", [{"type": "centrifugal"}], "enduse_pa6", 2950),
    ("7.", "Ось вращения*.STL", [{"type": "shaft_bending"}, {"type": "torsion"}], "enduse_pa6", None),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args()
    registry = build_registry()
    ctx = ToolContext(session_id="strength", emit=lambda *a, **k: None, services={})
    lines = ["| Модель | Режим по геометрии | 3D (лучшая поза) | 5D | Рекомендация | Решает |",
             "|---|---|---|---|---|---|"]
    for prefix, pattern, loads, material, rpm in CASES:
        folder = [p for p in glob.glob(os.path.join(ROOT, "models", prefix + "*")) if os.path.isdir(p)][0]
        name = os.path.basename(folder)
        if loads is None:
            lines.append("| %s | — | — | — | инструмент не вызывается (нет нагрузки) | — |" % name)
            continue
        path = glob.glob(os.path.join(folder, pattern))[0]
        args = {"path": path, "loads": loads, "material_id": material}
        if rpm:
            args["rpm"] = rpm
        result = registry.call("compare_print_strategies", args, ctx)
        if not result.ok:
            lines.append("| %s | ОШИБКА: %s |||||" % (name, result.text))
            continue
        data = json.loads(result.text)
        if options.json:
            print(json.dumps(data, ensure_ascii=False, indent=1))
        rows = data["variants"]
        row3 = rows[0]
        rows5 = [r for r in rows if r.get("printing_mode")]
        five = "; ".join("%s %.2f (худш. %s)" % (r["variant"].split(" на ")[0].split(" (")[0], r["index"], r["worst_load"])
                         for r in rows5) or "неприменимо"
        standing = [r for r in rows if "ось детали вертикально" in r["variant"]]
        if standing:
            five += "; 3D стоя %.2f" % standing[0]["index"]
        rec = data["recommended"]
        lines.append("| %s | %s | %.2f (%s; поддержки %s, худш. %s) | %s | %s | %s |" % (
            name, rec["geometry_mode"], row3["index"], row3["variant"][4:60], row3["supports"], row3["worst_load"],
            five, rec["label"], rec["decided_by"]))
        lines.append("|  | _%s_ |||||" % data["why"])
    text = "\n".join(lines)
    print(text)
    if options.out:
        with open(options.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")


if __name__ == "__main__":
    main()
