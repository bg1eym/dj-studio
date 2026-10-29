"""从 PDF 里取带坐标的文字（pypdf，纯 Python）。

打碟谱是 ReportLab 生成的：每个表格单元格、每行正文都是一个独立的文字块，有自己的起点坐标；
同一块里中英文字体切换会拆成几段，但段与段的发出顺序就是阅读顺序。这里按 (页, y, x) 把段拼回块。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class PdfError(ValueError):
    pass


@dataclass
class Cell:
    page: int  # 从 1 起
    y: float  # 页面坐标，越大越靠上
    x: float
    text: str
    size: float = 0.0


def read_cells(path: Path) -> list[list[Cell]]:
    """每页一个列表，按从上到下、从左到右排好。"""
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover
        raise PdfError("没有安装 pypdf（pip install pypdf）") from e
    try:
        reader = PdfReader(str(path))
    except Exception as e:  # noqa: BLE001
        raise PdfError(f"读不了这个 PDF：{e}") from e
    pages: list[list[Cell]] = []
    for pno, page in enumerate(reader.pages, 1):
        blocks: dict[tuple[float, float], list[str]] = {}
        sizes: dict[tuple[float, float], float] = {}

        def visit(text, cm, tm, font, size):  # type: ignore[no-untyped-def]
            if not text:
                return
            x = cm[0] * tm[4] + cm[2] * tm[5] + cm[4]
            y = cm[1] * tm[4] + cm[3] * tm[5] + cm[5]
            key = (round(y, 1), round(x, 1))
            blocks.setdefault(key, []).append(text)
            sizes[key] = max(sizes.get(key, 0.0), float(size) * abs(float(tm[0]) or 1.0))

        page.extract_text(visitor_text=visit)
        cells = []
        for (y, x), parts in blocks.items():
            t = " ".join("".join(parts).split())
            if t:
                cells.append(Cell(pno, y, x, t, sizes[(y, x)]))
        cells.sort(key=lambda c: (-c.y, c.x))
        pages.append(cells)
    if not any(pages):
        raise PdfError("PDF 里没有文字层（扫描件？）——打碟谱需要是可选中文字的 PDF")
    return pages


def lines(cells: list[Cell], tol: float = 1.0) -> list[list[Cell]]:
    """同一 y（±tol）的块归成一行，行内按 x 排序。"""
    out: list[list[Cell]] = []
    for c in cells:
        if out and abs(out[-1][0].y - c.y) <= tol:
            out[-1].append(c)
        else:
            out.append([c])
    for ln in out:
        ln.sort(key=lambda c: c.x)
    return out


def _cjk(ch: str) -> bool:
    return ord(ch) > 0x2E7F


def join(a: str, b: str) -> str:
    """拼接换行的单元格：两边都是中文直接接上，否则补一个空格（原文换行处的空格被吃掉了）。"""
    if not a:
        return b
    if not b:
        return a
    return a + b if _cjk(a[-1]) and _cjk(b[0]) else a + " " + b


@dataclass
class Table:
    columns: list[str]
    rows: list[dict[str, str]]  # 列名 → 文字（多行单元格已按行拼接）
    end_y: float  # 表格最后一行的 y


def table(cells: list[Cell], header: list[str], anchor: str, stop: list[str] | None = None,
          row_ok=None) -> Table | None:  # type: ignore[no-untyped-def]
    """在一页里找表头（一行里依次出现 header 的每一项，前缀匹配），按表头的 x 分列。

    anchor：每行第一个非空的列名（这一列出现新文字 = 新的一行）；其余列的续行拼到当前行。
    stop：遇到以这些文字开头的块就结束。row_ok(text) 判断 anchor 列的文字是不是一行的开头。
    """
    stop = stop or []
    lns = lines(cells)
    hdr_i = None
    for i, ln in enumerate(lns):
        texts = [c.text for c in ln]
        if len(texts) >= len(header) and all(any(t.startswith(h) for t in texts) for h in header):
            hdr_i = i
            break
    if hdr_i is None:
        return None
    hdr = lns[hdr_i]
    cols: list[tuple[str, float]] = []
    for h in header:
        c = next(c for c in hdr if c.text.startswith(h))
        cols.append((h, c.x))
    xs = [x for _, x in cols]

    def col_of(x: float) -> str | None:
        best = min(range(len(xs)), key=lambda k: abs(xs[k] - x))
        return cols[best][0] if abs(xs[best] - x) < 25 else None

    rows: list[dict[str, str]] = []
    end_y = hdr[0].y
    started = False
    for ln in lns[hdr_i + 1:]:
        if any(ln[0].text.startswith(s) for s in stop):
            break
        mapped = [(col_of(c.x), c) for c in ln]
        if any(k is None for k, _ in mapped):
            if started:
                break  # 表格外的正文
            continue  # 表头的第二行（如「拍」「Loop」）之类
        anchor_cell = next((c for k, c in mapped if k == anchor), None)
        if anchor_cell is not None and (row_ok is None or row_ok(anchor_cell.text)):
            rows.append({k: c.text for k, c in mapped})  # type: ignore[misc]
            started = True
        elif rows:
            for k, c in mapped:
                rows[-1][k] = join(rows[-1].get(k, ""), c.text)  # type: ignore[index]
        else:
            continue
        end_y = ln[0].y
    return Table([h for h, _ in cols], rows, end_y)
