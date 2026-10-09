#!/usr/bin/env python3
"""Read-only repository structure audit. No user data, files or binaries are modified."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = (
    "photo_curator.py", "desktop_app.py", "catalog.py", "db_runtime.py",
    "background_tasks.py", "runtime_paths.py", "raw_loader.py",
    "exact_duplicates.py", "photo_dedup_batch.py",
    "photo_ranking_v3.py",
    "quality_annotations.py", "scene_labels.py",
)
LAUNCHERS = {
    "一键安装并启动.bat": "PhotoCurator-Install.cmd",
    "启动照片筛选.bat": "PhotoCurator-Start.cmd",
    "打开日志文件夹.bat": "PhotoCurator-Logs.cmd",
    "浏览器兼容模式.bat": "PhotoCurator-Browser.cmd",
    "环境自检.bat": "PhotoCurator-Check.cmd",
    "调试运行.bat": "PhotoCurator-Debug.cmd",
}


def audit():
    errors, warnings = [], []
    definitions = {}
    routes = {}
    sizes = {}
    for name in PRODUCTION:
        path = ROOT / name
        if not path.is_file():
            errors.append(f"Missing production module: {name}")
            continue
        source = path.read_text(encoding="utf-8-sig")
        sizes[name] = (len(source.splitlines()), path.stat().st_size)
        try:
            module = ast.parse(source, filename=name)
        except SyntaxError as exc:
            errors.append(f"Syntax error in {name}: {exc}")
            continue
        names = [node.name for node in module.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        for symbol, count in Counter(names).items():
            if count > 1:
                errors.append(f"Repeated top-level definition: {name}:{symbol} ({count})")
        definitions[name] = len(names)
        for node in module.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not (isinstance(func, ast.Attribute) and func.attr == "route"):
                    continue
                if not decorator.args or not isinstance(decorator.args[0], ast.Constant):
                    continue
                route = decorator.args[0].value
                if not isinstance(route, str):
                    continue
                route_key = (name, route)
                if route_key in routes:
                    errors.append(f"Duplicate route: {name}:{route}, "
                                  f"{routes[route_key]} and {node.name}")
                routes[route_key] = node.name

    # Include root-level test and utility files in the syntax inventory. This
    # catches files omitted from hard-coded CI compile lists without importing
    # modules, accessing personal data or executing application code.
    python_files = sorted(ROOT.glob("*.py")) + sorted((ROOT / "tests").glob("*.py"))
    for path in python_files:
        try:
            source = path.read_text(encoding="utf-8-sig")
            compile(source, str(path), "exec")
        except (SyntaxError, UnicodeError) as exc:
            errors.append(f"Python source cannot compile: {path.name}: {exc}")

    if not (ROOT / "tests" / "__init__.py").exists():
        errors.append("tests package initializer missing")

    for alias, target in LAUNCHERS.items():
        path = ROOT / alias
        target_path = ROOT / target
        if not path.exists() or not target_path.exists():
            errors.append(f"Launcher pair missing: {alias} -> {target}")
            continue
        content = path.read_text(encoding="utf-8-sig")
        if target.lower() not in content.lower():
            errors.append(f"Launcher does not delegate to its canonical script: {alias}")

    version_sources = []
    for name in ("photo_curator.py", "desktop_app.py"):
        path = ROOT / name
        if path.exists():
            match = re.search(r'^APP_VERSION\s*=\s*["\']([^"\']+)["\']',
                              path.read_text(encoding="utf-8-sig"), re.M)
            if match:
                version_sources.append((name, match.group(1)))
            else:
                errors.append(f"APP_VERSION missing in {name}")
    if len({version for _, version in version_sources}) > 1:
        errors.append(f"Version drift: {version_sources}")

    main = ROOT / "photo_curator.py"
    if main.exists():
        source = main.read_text(encoding="utf-8-sig")
        html = re.search(r"HTML\s*=\s*r'''([\s\S]*?)'''", source)
        if html:
            warnings.append(f"Embedded frontend in photo_curator.py: "
                            f"{len(html.group(1)):,} chars; extraction needs packaging and test review")
        if len(source.splitlines()) > 5000:
            warnings.append("Main module exceeds 5,000 lines; review separation of responsibilities")

    print("PhotoCurator repository structure audit")
    print(f"Modules checked: {len(sizes)}/{len(PRODUCTION)}")
    print(f"Root Python files syntax-checked: {len(python_files)}")
    print(f"Routes checked: {len(routes)}")
    print(f"Launcher aliases checked: {len(LAUNCHERS)}")
    for name, (lines, nbytes) in sorted(sizes.items(), key=lambda kv: -kv[1][1])[:5]:
        print(f"Large module: {name}: {lines} lines / {nbytes} bytes")
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}")
    print(f"Result: {len(errors)} errors, {len(warnings)} architectural warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    sys.exit(audit())
