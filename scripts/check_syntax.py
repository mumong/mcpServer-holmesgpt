"""Compile maintained Python sources in memory, without imports or bytecode."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    sources = list(ROOT.glob("*.py"))
    for directory in ("servers", "scripts", "tests"):
        sources.extend((ROOT / directory).rglob("*.py"))
    for source in sorted(sources):
        compile(source.read_bytes(), str(source.relative_to(ROOT)), "exec")
    print(f"Syntax OK: {len(sources)} maintained Python files (no bytecode written)")


if __name__ == "__main__":
    main()
