"""
Парсер выписки Озон-банка (PDF -> операции) без сторонних библиотек.

Работает так же, как ручная сверка: читает PDF, разбирает поток контента,
достаёт текст через карту /ToUnicode шрифтов и склеивает операции.

Использование:
  python pdf_parser.py "D:/Репетиторство DSH/Пудов_Д_С_...pdf" --out data/ops.csv
  python pdf_parser.py выписка.pdf --json data/ops.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import zlib
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# --------------------------------------------------------------------------- #
# Мини-парсер PDF (объекты, потоки, ToUnicode)
# --------------------------------------------------------------------------- #
OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj(.*?)endobj", re.S)


def _objects(data: bytes) -> dict[int, bytes]:
    out = {}
    for m in re.finditer(rb"(\d+)\s+\d+\s+obj(.*?)endobj", data, re.S):
        out[int(m.group(1))] = m.group(2)  # при повторе номера берётся последний
    return out


def _stream_data(obj: bytes) -> bytes | None:
    m = re.search(rb"stream\r?\n(.*?)\r?\nendstream", obj, re.S)
    if not m:
        return None
    raw = m.group(1)
    if b"/FlateDecode" in obj:
        try:
            return zlib.decompress(raw)
        except zlib.error:
            try:
                return zlib.decompressobj().decompress(raw)
            except zlib.error:
                return None
    return raw


def _parse_tounicode(cmap: bytes) -> dict[int, str]:
    mapping: dict[int, str] = {}

    def hex_str(h: bytes) -> str:
        return bytes.fromhex(h.decode()).decode("utf-16-be", errors="replace")

    for block in re.findall(rb"beginbfchar(.*?)endbfchar", cmap, re.S):
        for src, dst in re.findall(rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            mapping[int(src, 16)] = hex_str(dst)
    for block in re.findall(rb"beginbfrange(.*?)endbfrange", cmap, re.S):
        for lo, hi, dst in re.findall(
                rb"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block):
            lo_i, hi_i, base = int(lo, 16), int(hi, 16), int(dst, 16)
            for i in range(lo_i, min(hi_i, lo_i + 65535) + 1):
                mapping[i] = chr(base + (i - lo_i))
    return mapping


def _font_maps(objs: dict[int, bytes]) -> dict[int, dict[int, str]]:
    """Номер объекта шрифта -> карта кодов."""
    maps = {}
    for num, body in objs.items():
        if b"/ToUnicode" in body and b"/Font" in body:
            m = re.search(rb"/ToUnicode\s+(\d+)\s+0\s+R", body)
            if m:
                cmap = _stream_data(objs.get(int(m.group(1)), b""))
                if cmap:
                    maps[num] = _parse_tounicode(cmap)
    return maps


def extract_text(path: Path) -> str:
    data = path.read_bytes()
    objs = _objects(data)
    fontmaps = _font_maps(objs)

    # карта /F1 -> номер объекта шрифта (из объектов ресурсов)
    res_font: dict[str, int] = {}
    for body in objs.values():
        m = re.search(rb"/Font\s+(\d+)\s+0\s+R", body)
        if m:
            fobj = objs.get(int(m.group(1)), b"")
            for name, num in re.findall(rb"/(F\d+)\s+(\d+)\s+0\s+R", fobj):
                res_font[name.decode()] = int(num)
        m2 = re.search(rb"/Font\s*<<(.*?)>>", body, re.S)
        if m2:
            for name, num in re.findall(rb"/(F\d+)\s+(\d+)\s+0\s+R", m2.group(1)):
                res_font.setdefault(name.decode(), int(num))

    lines: list[str] = []
    cur_font = None
    for body in objs.values():
        content = _stream_data(body)
        if not content or b" Td " not in content and b"TJ" not in content:
            continue
        for bt in re.findall(rb"BT(.*?)ET", content, re.S):
            buf: list[str] = []
            for tok in re.finditer(
                    rb"/(F\d+)\s+[\d.]+\s+Tf|<([0-9A-Fa-f]+)>\s*Tj|\[(.*?)\]\s*TJ|([\d.-]+\s+[\d.-]+\s+)(Td|TD|T\*)",
                    bt, re.S):
                if tok.group(1):
                    cur_font = tok.group(1).decode()
                elif tok.group(2):
                    buf.append(_decode_hex(tok.group(2), fontmaps.get(res_font.get(cur_font, -1), {})))
                elif tok.group(3):
                    for hx in re.findall(rb"<([0-9A-Fa-f]+)>", tok.group(3)):
                        buf.append(_decode_hex(hx, fontmaps.get(res_font.get(cur_font, -1), {})))
                else:
                    if buf:
                        lines.append("".join(buf))
                        buf = []
            if buf:
                lines.append("".join(buf))
    return "\n".join(ln for ln in lines if ln.strip())


def _decode_hex(hx: bytes, cmap: dict[int, str]) -> str:
    raw = bytes.fromhex(hx.decode())
    if not cmap:
        return raw.decode("latin-1")
    # сначала пробуем двухбайтовые коды (TrueType/Identity), потом однобайтовые
    two = "".join(cmap.get((raw[i] << 8) | raw[i + 1], "") for i in range(0, len(raw) - 1, 2))
    if two:
        return two
    one = "".join(cmap.get(b, "") for b in raw)
    return one or two


# --------------------------------------------------------------------------- #
# Операции из текста
# --------------------------------------------------------------------------- #
OP_RE = re.compile(
    r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})\s+.*?([+-])\s*([\d\s\u00a0]+[,.]\d{2})")
DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})\s+(\d{2}:\d{2}:\d{2})")
SUM_RE = re.compile(r"([+-])\s*([\d\s\u00a0]+[,.](\d{2}))")


def parse_ops(text: str) -> list[dict]:
    ops: list[dict] = []
    lines = [ln.strip() for ln in text.splitlines()]
    cur = None
    for ln in lines:
        m = DATE_RE.search(ln)
        if m:
            if cur:
                ops.append(cur)
            cur = {"date": m.group(1), "time": m.group(2), "sum": None, "who": ""}
            # сумма может быть в этой же строке
        s = SUM_RE.search(ln)
        if cur and s and cur["sum"] is None:
            val = s.group(2).replace(" ", "").replace("\u00a0", "").replace(",", ".")
            sign = -1 if s.group(1) == "-" else 1
            try:
                cur["sum"] = sign * int(round(float(val)))
            except ValueError:
                pass
        if cur and ("Отправитель:" in ln or "Плательщик:" in ln):
            cur["who"] = ln.split(":", 1)[1].strip()
        if cur and ln and cur["sum"] is None and re.fullmatch(r"[\d\s\u00a0]+[,.]\d{2}", ln):
            val = ln.replace(" ", "").replace("\u00a0", "").replace(",", ".")
            try:
                cur["sum"] = int(round(float(val)))
            except ValueError:
                pass
    if cur:
        ops.append(cur)
    return [o for o in ops if o["sum"] is not None]


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default=None, help="CSV для reconcile.py")
    ap.add_argument("--json", default=None)
    ap.add_argument("--text", action="store_true", help="только вывести текст")
    args = ap.parse_args()

    text = extract_text(Path(args.pdf))
    if args.text:
        print(text)
        return

    ops = parse_ops(text)
    print(f"Найдено операций: {len(ops)}")
    for o in ops[:10]:
        print(o)

    if args.out:
        rows = []
        for o in ops:
            d, t = o["date"], o["time"]
            dd, mm, yy = d.split(".")
            year = int(yy)
            if year < 100:
                year += 2000
            rows.append(f"{year:04d}-{mm}-{dd} {t[:5]};{o['sum']};{o['who']}")
        Path(args.out).write_text("\n".join(rows), encoding="utf-8")
        print(f"Записано в {args.out}")
    if args.json:
        Path(args.json).write_text(json.dumps([
            {"dt": f"{o['date'][6:]}-{o['date'][3:5]}-{o['date'][:2]} {o['time'][:5]}",
             "sum": o["sum"], "who": o["who"]} for o in ops], ensure_ascii=False, indent=1),
            encoding="utf-8")
        print(f"Записано в {args.json}")


if __name__ == "__main__":
    main()
