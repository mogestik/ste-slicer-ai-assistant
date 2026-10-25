"""Самопроверка сохранённых результатов (проект 3MF и G-code) без слайсера, только stdlib, Python 3.5+.

    from agent.verify import verify
    report = verify("/path/project.3mf", expected={"printing_mode": "classic"}, gcode_path="/path/x.gcode")

Урезанная версия devtools/ste3mf.py и devtools/check_gcode.py.
"""

from .check import companion_gcode, newest_file, verify, verify_gcode  # noqa: F401
from .project3mf import Project, ProjectError, load_project  # noqa: F401
