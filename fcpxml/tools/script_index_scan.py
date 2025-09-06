import os
import ast
import json
from pathlib import Path


def scan_repo(root: Path) -> list[dict]:
    files: list[Path] = []
    for base, _, names in os.walk(root):
        for n in names:
            if n.endswith(".py"):
                files.append(Path(base) / n)
    results: list[dict] = []
    cwd = Path.cwd().resolve()
    for p in sorted(files, key=lambda x: str(x).lower()):
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            text = ""
        doc = ""
        parser_descs: list[str] = []
        add_args: list[dict] = []
        subcmds: list[dict] = []
        has_main = ("if __name__" in text and "__main__" in text)
        try:
            tree = ast.parse(text)
            doc = (ast.get_docstring(tree) or "").strip()

            class V(ast.NodeVisitor):
                def visit_Call(self, node: ast.Call):
                    fn = node.func
                    name = None
                    if isinstance(fn, ast.Name):
                        name = fn.id
                    elif isinstance(fn, ast.Attribute):
                        name = fn.attr
                    # argparse.ArgumentParser(... description=...)
                    if name == "ArgumentParser":
                        for kw in node.keywords:
                            if kw.arg == "description":
                                v = kw.value
                                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                                    parser_descs.append(v.value.strip())
                    # *.add_argument(...)
                    if isinstance(fn, ast.Attribute) and fn.attr == "add_argument":
                        rec = {"flags": [], "help": None, "action": None, "type": None, "choices": None, "default": None}
                        for a in node.args:
                            if isinstance(a, ast.Constant) and isinstance(a.value, str):
                                rec["flags"].append(a.value)
                        for kw in node.keywords:
                            if kw.arg in rec:
                                v = kw.value
                                if isinstance(v, ast.Constant):
                                    rec[kw.arg] = v.value
                                elif isinstance(v, ast.Name):
                                    rec[kw.arg] = v.id
                                elif isinstance(v, ast.Attribute):
                                    rec[kw.arg] = getattr(v, "attr", None)
                        add_args.append(rec)
                    # subparser: *.add_parser(name, help=...)
                    if isinstance(fn, ast.Attribute) and fn.attr == "add_parser":
                        nm = None
                        hp = None
                        if node.args:
                            a0 = node.args[0]
                            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                                nm = a0.value
                        for kw in node.keywords:
                            if kw.arg == "help" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                                hp = kw.value.value
                        subcmds.append({"name": nm, "help": hp})
                    self.generic_visit(node)

            V().visit(tree)
        except Exception:
            pass
        results.append(
            {
                "file": str(p).replace("\\", "/"),
                "path": str(p.resolve()).replace("\\", "/"),
                "in_cwd": p.resolve().parent == cwd,
                "doc": doc,
                "parser_descriptions": parser_descs,
                "add_arguments": add_args,
                "subcommands": subcmds,
                "has_main": has_main,
            }
        )
    return results


def main() -> None:
    data = scan_repo(Path("."))
    Path("script_index.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Also emit a compact text summary for direct printing
    lines: list[str] = []
    for r in data:
        lines.append(f"## {Path(r['file']).name}")
        lines.append(f"- Path: {r['path']}")
        if r["in_cwd"]:
            lines.append("- In current dir: yes")
        if r["doc"]:
            first = r["doc"].splitlines()[0].strip()
            if first:
                lines.append(f"- Docstring: {first}")
        if r["parser_descriptions"]:
            lines.append(f"- CLI: {r['parser_descriptions'][0]}")
        if r["subcommands"]:
            subs = ", ".join(
                [s["name"] for s in r["subcommands"] if s.get("name")]
            )
            if subs:
                lines.append(f"- Subcommands: {subs}")
        if r["add_arguments"]:
            flags = []
            for a in r["add_arguments"]:
                if a["flags"]:
                    flags.append("/".join(a["flags"]))
            if flags:
                # Dedup and keep short
                uniq = []
                for f in flags:
                    if f not in uniq:
                        uniq.append(f)
                lines.append("- Arguments: " + ", ".join(uniq[:20]))
        lines.append("")
    Path("script_index_compact.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote script_index.json and script_index_compact.txt for {len(data)} files")


if __name__ == "__main__":
    main()

