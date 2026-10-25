"""Анализ геометрии для подготовки печати (чистый Python 3.5, без numpy).

* `mesh` — контейнер сетки, чтение STL (бинарный и ASCII) и OBJ, статистика за один проход;
* `threemf` — чтение 3MF (элементы build, компоненты, преобразования, единицы) и поворот
  узла, который применяет читатель 3MF слайсера при загрузке;
* `symmetry` — поиск оси вращения, материал на оси, диаметр отверстия;
* `orientation` — свесы и площадь контакта со столом для каждой позы, преобразование сцены Cura;
* `analysis` — отчёт для инструментов: геометрия, ось, рекомендуемый режим печати
  (classic / cylindrical / cylindrical_full), позы для 3D и постановка для 5D (ось
  вертикально через центр стола).

Объекты, которые уже на сцене: `analysis.analyze_triangles(coords, frame="scene")`
принимает их сетку без поворота узла (с масштабом); tools/geometry_tools.py получает её
через адаптер слайсера.
"""

from .analysis import (DEFAULT_SUPPORT_ANGLE, MODE_LABELS, analyze_file, analyze_mesh,  # noqa: F401
                       analyze_triangles)
from .mesh import Mesh, MeshError, mesh_from_triangles, read_mesh, read_obj, read_stl  # noqa: F401
