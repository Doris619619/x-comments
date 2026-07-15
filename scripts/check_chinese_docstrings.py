"""
本文件检查 Python 源文件、类和函数是否包含中文说明。

它属于质量检查脚本，只读取源码 AST，不修改项目文件或执行应用代码。
"""

import ast
import re
from pathlib import Path

SOURCE_ROOTS = (Path("app"), Path("scripts"), Path("tests"), Path("alembic"))
CHINESE_PATTERN = re.compile(r"[\u4e00-\u9fff]")


def iter_python_files() -> list[Path]:
    """
    返回质量检查范围内的全部 Python 文件。

    无输入，返回排序路径；文件系统错误向上抛出；只读取目录元数据。
    """

    return sorted(path for root in SOURCE_ROOTS for path in root.rglob("*.py"))


def check_file(path: Path) -> list[str]:
    """
    检查单个 Python 文件的模块、类和函数中文 Docstring。

    输入源码路径，返回问题列表；语法/读取错误向上抛出；无写入副作用。
    """

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    issues: list[str] = []
    module_doc = ast.get_docstring(tree) or ""
    if not CHINESE_PATTERN.search(module_doc):
        issues.append(f"{path}:1 模块缺少中文说明")
    for node in ast.walk(tree):
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node) or ""
        if not CHINESE_PATTERN.search(doc):
            issues.append(f"{path}:{node.lineno} {node.name} 缺少中文说明")
    return issues


def main() -> None:
    """
    执行全项目中文 Docstring 检查并以退出码报告结果。

    无输入输出对象；发现问题抛出 SystemExit；只读取源码并打印摘要。
    """

    files = iter_python_files()
    issues = [issue for path in files for issue in check_file(path)]
    if issues:
        raise SystemExit("\n".join(issues))
    print(f"中文说明检查通过：{len(files)} 个 Python 文件")


if __name__ == "__main__":
    main()
