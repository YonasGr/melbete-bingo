"""Guards against a method being defined twice in one class: the later definition silently
overrides the earlier one (this once shadowed the atomic withdrawal approve/reject)."""
import ast
from collections import Counter
from pathlib import Path

SRC = Path(__file__).resolve().parents[1]


def test_no_class_defines_the_same_method_twice():
    problems = []
    for path in list(SRC.glob("services/*.py")) + list(SRC.glob("repositories/*.py")) + list(SRC.glob("api/**/*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            names = Counter(
                f.name for f in cls.body
                if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not any(isinstance(d, ast.Attribute) and d.attr in ("setter", "getter", "deleter") for d in f.decorator_list)
            )
            problems += [f"{path.name}:{cls.name}.{n}" for n, c in names.items() if c > 1]
    assert not problems, f"duplicate method definitions: {problems}"
