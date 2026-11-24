"""마크다운 문서의 상대 경로 링크가 실제 파일을 가리키는지 검사한다.

외부 링크(http)는 검사하지 않는다. 사이트 상태에 따라 CI가 흔들리지 않게 하기 위함.
사용법: python .github/scripts/check_md_links.py [파일 또는 폴더 ...]
"""

import re
import sys
from pathlib import Path
from urllib.parse import unquote

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "build", "dist"}


def md_files(paths):
    for p in map(Path, paths):
        if p.is_dir():
            yield from (f for f in p.rglob("*.md") if not SKIP_DIRS & set(f.parts))
        elif p.suffix == ".md":
            yield p


def broken_links(md: Path):
    in_code = False
    for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_code = not in_code
        if in_code:
            continue
        for target in LINK.findall(line):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path = unquote(target.split("#", 1)[0])
            if path and not (md.parent / path).exists():
                yield lineno, target


def main():
    failed = 0
    for md in md_files(sys.argv[1:] or ["."]):
        for lineno, target in broken_links(md):
            print(f"{md}:{lineno}: 깨진 링크 → {target}")
            failed += 1
    if failed:
        print(f"\n깨진 링크 {failed}개")
        sys.exit(1)
    print("링크 이상 없음")


if __name__ == "__main__":
    main()
