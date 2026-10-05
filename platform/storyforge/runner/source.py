"""Deterministic chapter splitting and persistent paragraph addresses."""
import re

from storyforge.config import SflError
from storyforge.store import Store, serialize

CHAPTER = re.compile(r"^(?:#{1,3}\s+.+|第[零〇一二三四五六七八九十百千万两\d]+[章节回卷].*|Chapter\s+\d+.*)$", re.I)


def split(text: str, maximum=12000):
    if not isinstance(text, str) or not text.strip():
        raise SflError("Novel text is empty")
    chapters, heading, lines = [], "正文", []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if CHAPTER.fullmatch(line.strip()):
            if any(s.strip() for s in lines):
                chapters.append((heading, lines))
            heading, lines = line.strip().lstrip("# "), []
        else:
            lines.append(line)
    if any(s.strip() for s in lines):
        chapters.append((heading, lines))
    if not chapters:
        raise SflError("Novel has chapter headings but no paragraphs")
    index = {"paragraphs":{},"chunks":[]}
    for chapter, (title, lines) in enumerate(chapters, 1):
        joined = "\n".join(lines).strip()
        paragraphs = re.split(r"\n\s*\n", joined) if re.search(r"\n\s*\n", joined) else joined.splitlines()
        group, size, groups = [], 0, []
        for number, paragraph in enumerate((p.strip() for p in paragraphs if p.strip()), 1):
            ref = f"ch{chapter:03d}:p{number:04d}"
            entry = {"ref":ref,"chapter":chapter,"paragraph":number,"title":title,"text":paragraph}
            index["paragraphs"][ref] = entry
            if group and size + len(paragraph) > maximum:
                groups.append(group)
                group, size = [], 0
            group.append(entry)
            size += len(paragraph)
        if group:
            groups.append(group)
        for number, group in enumerate(groups, 1):
            index["chunks"].append({"id":f"ch{chapter:03d}_{number:03d}","chapter":chapter,"title":title,"paragraphs":group})
    return index


def import_chunks(store: Store, stage="A1", maximum=12000):
    if store.current("source/index.json"):
        return store.json("source/index.json")
    binding = store.binding("source/novel.txt")
    index = split(store.selected(binding), maximum)
    outputs = {"source/index.json":serialize(index)+"\n"}
    for chunk in index["chunks"]:
        outputs[f"source/chunks/{chunk['id']}.json"] = serialize(chunk)+"\n"
        outputs[f"source/chunks/{chunk['id']}.md"] = f"# {chunk['id']} · {chunk['title']}\n\n" + "\n\n".join(f"[{p['ref']}] {p['text']}" for p in chunk["paragraphs"]) + "\n"
    job = store.start_job(stage, "project", [binding])
    if not store.accept(job, stage, "project", [binding], outputs):
        raise SflError("Novel changed during import; retry")
    for path in store.path("source/chunks").glob("*"):
        if path.is_file() and path.relative_to(store.root).as_posix() not in outputs:
            path.unlink()
    return index
