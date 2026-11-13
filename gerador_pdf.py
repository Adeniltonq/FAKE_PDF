#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gerador de PDFs Aleatórios
==========================

Programa 100%% Python (somente biblioteca padrão) que:

  1) Embaralha/redistribui páginas de PDFs existentes;
  2) Gera PDFs usando SEUS textos e SUAS fotos;
  3) Gera PDFs totalmente aleatórios (textos criados na hora).

Funciona em Linux, Windows e macOS.
  * Interface gráfica com Tkinter:      python gerador_pdf.py
  * Modo terminal interativo:           python gerador_pdf.py --terminal
  * Linha de comando:                   python gerador_pdf.py --cli ...
  * Ajuda:                              python gerador_pdf.py --cli ajuda

Nenhum pacote externo é necessário (nem pypdf, nem reportlab, nem Pillow).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import datetime
import os
import random
import re
import struct
import sys
import time
import zlib

# =============================================================================
# 1. MODELO DE OBJETOS PDF
# =============================================================================


class PDFError(Exception):
    """Erro ao ler/gravar um PDF."""


class Name(str):
    """Nome PDF (ex.: /Type, /Page). Guardado sem a barra."""

    __slots__ = ()


class Ref:
    """Referência indireta: '12 0 R'."""

    __slots__ = ("num", "gen")

    def __init__(self, num, gen=0):
        self.num = int(num)
        self.gen = int(gen)

    def __repr__(self):  # pragma: no cover - ajuda na depuração
        return "%d %d R" % (self.num, self.gen)

    def __eq__(self, other):
        return isinstance(other, Ref) and other.num == self.num and other.gen == self.gen

    def __hash__(self):
        return hash((self.num, self.gen))


class Stream:
    """Stream PDF: dicionário + bytes crus (ainda com os filtros originais)."""

    __slots__ = ("dict", "data", "compress")

    def __init__(self, d, data, compress=False):
        self.dict = d
        self.data = data
        self.compress = bool(compress)

    def __repr__(self):  # pragma: no cover
        return "Stream(%r, %d bytes)" % (self.dict, len(self.data))


# =============================================================================
# 2. LEITOR DE PDF (tokens, objetos, xref, filtros)
# =============================================================================

_WS = b"\x00\t\n\x0c\r "
_DELIMS = b"()<>[]{}/%"
_NUM_RE = re.compile(rb"[+-]?(?:\d+\.\d*|\.\d+|\d+)")


def _is_ws(b):
    return b in (0, 9, 10, 12, 13, 32)


class Lexer:
    """Analisador sintático de valores PDF."""

    def __init__(self, data, doc=None):
        self.data = data
        self.doc = doc
        self.pos = 0

    # -- utilidades ---------------------------------------------------------
    def skip_ws(self):
        data = self.data
        n = len(data)
        p = self.pos
        while p < n:
            c = data[p]
            if _is_ws(c):
                p += 1
            elif c == 37:  # '%' comentário
                e = data.find(b"\n", p)
                p = n if e < 0 else e + 1
            else:
                break
        self.pos = p

    def parse_value_at(self, pos):
        self.pos = pos
        return self.parse_value()

    # -- valores ------------------------------------------------------------
    def parse_value(self):
        self.skip_ws()
        data = self.data
        p = self.pos
        if p >= len(data):
            raise PDFError("fim inesperado do arquivo PDF")
        c = data[p]

        if c == 0x2F:  # '/'
            return self.parse_name()
        if c == 0x28:  # '('
            return self.parse_literal_string()
        if c == 0x3C:  # '<'
            if data[p:p + 2] == b"<<":
                return self.parse_dict()
            return self.parse_hex_string()
        if c == 0x5B:  # '['
            return self.parse_array()
        if c in b"+-.0123456789":
            return self.parse_number_or_ref()

        m = re.match(rb"[A-Za-z]+", data[p:p + 32])
        if m:
            word = m.group(0)
            self.pos = p + len(word)
            if word == b"true":
                return True
            if word == b"false":
                return False
            if word == b"null":
                return None
            return Name(word.decode("latin-1"))

        raise PDFError("token inesperado na posição %d: %r" % (p, data[p:p + 20]))

    def parse_name(self):
        data = self.data
        p = self.pos + 1
        out = bytearray()
        n = len(data)
        while p < n:
            c = data[p]
            if c in _DELIMS or _is_ws(c):
                break
            if c == 35 and p + 2 < n:  # '#xx'
                try:
                    out.append(int(data[p + 1:p + 3], 16))
                    p += 3
                    continue
                except ValueError:
                    pass
            out.append(c)
            p += 1
        self.pos = p
        return Name(out.decode("latin-1"))

    def parse_literal_string(self):
        data = self.data
        p = self.pos + 1
        n = len(data)
        depth = 1
        out = bytearray()
        while p < n:
            c = data[p]
            if c == 92:  # '\'
                p += 1
                if p >= n:
                    break
                e = data[p]
                if e == 110:
                    out += b"\n"; p += 1
                elif e == 114:
                    out += b"\r"; p += 1
                elif e == 116:
                    out += b"\t"; p += 1
                elif e == 98:
                    out += b"\b"; p += 1
                elif e == 102:
                    out += b"\f"; p += 1
                elif e in (40, 41, 92):
                    out.append(e); p += 1
                elif e in (10, 13):
                    if e == 13 and p + 1 < n and data[p + 1] == 10:
                        p += 1
                    p += 1
                elif 48 <= e <= 55:
                    oct_digits = bytearray()
                    while p < n and len(oct_digits) < 3 and 48 <= data[p] <= 55:
                        oct_digits.append(data[p])
                        p += 1
                    out.append(int(oct_digits, 8) & 0xFF)
                else:
                    out.append(e); p += 1
            elif c == 40:
                depth += 1
                out.append(c)
                p += 1
            elif c == 41:
                depth -= 1
                if depth == 0:
                    p += 1
                    break
                out.append(c)
                p += 1
            else:
                out.append(c)
                p += 1
        self.pos = p
        return bytes(out)

    def parse_hex_string(self):
        data = self.data
        p = self.pos + 1
        e = data.find(b">", p)
        if e < 0:
            e = len(data)
        raw = re.sub(rb"[^0-9A-Fa-f]", b"", data[p:e])
        if len(raw) % 2:
            raw += b"0"
        self.pos = e + 1
        try:
            return bytes.fromhex(raw.decode("ascii"))
        except ValueError:
            return b""

    def parse_array(self):
        self.pos += 1
        out = []
        while True:
            self.skip_ws()
            if self.pos >= len(self.data):
                raise PDFError("array sem fim")
            if self.data[self.pos] == 0x5D:  # ']'
                self.pos += 1
                return out
            out.append(self.parse_value())

    def parse_dict(self):
        self.pos += 2
        out = {}
        while True:
            self.skip_ws()
            data = self.data
            if self.pos >= len(data):
                raise PDFError("dicionário sem fim")
            if data.startswith(b">>", self.pos):
                self.pos += 2
                return out
            key = self.parse_name()
            value = self.parse_value()
            out[str(key)] = value

    def parse_number_or_ref(self):
        data = self.data
        m = _NUM_RE.match(data, self.pos)
        if not m:
            raise PDFError("número inválido")
        token = m.group(0)
        self.pos = m.end()
        if b"." in token:
            return float(token)
        value = int(token)

        # Pode ser "N G R"
        save = self.pos
        self.skip_ws()
        m2 = re.match(rb"\d+", data[self.pos:self.pos + 16])
        if m2 and value >= 0:
            gen = int(m2.group(0))
            p2 = self.pos + m2.end()
            while p2 < len(data) and _is_ws(data[p2]):
                p2 += 1
            if p2 < len(data) and data[p2] == 82:  # 'R'
                nxt = p2 + 1
                if nxt >= len(data) or data[nxt] in _DELIMS or _is_ws(data[nxt]):
                    self.pos = nxt
                    return Ref(value, gen)
        self.pos = save
        return value


# --------------------------- filtros de stream -------------------------------


def _flate_decode(data):
    try:
        return zlib.decompress(data)
    except zlib.error:
        # alguns PDFs têm lixo antes/depois ou zlib sem cabeçalho
        try:
            return zlib.decompressobj().decompress(data)
        except zlib.error:
            try:
                return zlib.decompress(data, -15)
            except zlib.error:
                raise PDFError("não foi possível descomprimir stream (FlateDecode)")


def _asciihex_decode(data):
    data = data.split(b">")[0]
    data = re.sub(rb"[^0-9A-Fa-f]", b"", data)
    if len(data) % 2:
        data += b"0"
    return binascii.unhexlify(data)


def _ascii85_decode(data):
    data = re.sub(rb"\s", b"", data)
    if data.startswith(b"<~"):
        data = data[2:]
    if data.endswith(b"~>"):
        data = data[:-2]
    out = bytearray()
    group = []
    for c in data:
        if c == 122 and not group:  # 'z'
            out += b"\x00\x00\x00\x00"
            continue
        if not 33 <= c <= 117:
            continue
        group.append(c - 33)
        if len(group) == 5:
            n = 0
            for g in group:
                n = n * 85 + g
            out += n.to_bytes(4, "big", signed=False)
            group = []
    if group:
        k = len(group)
        group += [84] * (5 - k)
        n = 0
        for g in group:
            n = n * 85 + g
        out += n.to_bytes(4, "big")[:k - 1]
    return bytes(out)


def _runlength_decode(data):
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        length = data[i]
        i += 1
        if length == 128:
            break
        if length < 128:
            out += data[i:i + length + 1]
            i += length + 1
        else:
            if i < n:
                out += bytes([data[i]]) * (257 - length)
                i += 1
    return bytes(out)


def _lzw_decode(data):
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [b"", b""]
    code_len = 9
    prev = b""
    bitbuf = 0
    bits = 0
    i = 0
    n = len(data)
    while i < n or bits >= code_len:
        while bits < code_len and i < n:
            bitbuf = (bitbuf << 8) | data[i]
            i += 1
            bits += 8
        if bits < code_len:
            break
        bits -= code_len
        code = (bitbuf >> bits) & ((1 << code_len) - 1)
        if code == 256:
            table = [bytes([j]) for j in range(256)] + [b"", b""]
            code_len = 9
            prev = b""
            continue
        if code == 257:
            break
        if code < len(table):
            entry = table[code]
        elif code == len(table) and prev:
            entry = prev + prev[:1]
        else:
            raise PDFError("stream LZW inválido")
        out += entry
        if prev:
            table.append(prev + entry[:1])
            if len(table) + 1 >= (1 << code_len) and code_len < 12:
                code_len += 1
        prev = entry
    return bytes(out)


def _apply_predictor(data, parms):
    """Desfaz predição PNG/TIFF (usada em xref streams e algumas imagens)."""
    if not parms:
        return data
    predictor = int(parms.get("Predictor", 1) or 1)
    if predictor <= 1:
        return data
    colors = int(parms.get("Colors", 1) or 1)
    bpc = int(parms.get("BitsPerComponent", 8) or 8)
    columns = int(parms.get("Columns", 1) or 1)
    rowlen = (columns * colors * bpc + 7) // 8
    if rowlen <= 0:
        return data
    bpp = max(1, (colors * bpc + 7) // 8)

    if predictor == 2:  # TIFF predictor
        out = bytearray()
        for start in range(0, len(data), rowlen):
            row = bytearray(data[start:start + rowlen])
            for i in range(bpp, len(row)):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
            out += row
        return bytes(out)

    # PNG: cada linha começa com um byte de filtro
    out = bytearray()
    prev = bytearray(rowlen)
    p = 0
    n = len(data)
    while p < n:
        ftype = data[p]
        p += 1
        row = bytearray(data[p:p + rowlen])
        if len(row) < rowlen:
            row.extend(b"\x00" * (rowlen - len(row)))
        p += rowlen
        if ftype == 1:
            for i in range(bpp, rowlen):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(rowlen):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(rowlen):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(rowlen):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa = abs(b - c)
                pb = abs(a - c)
                pc = abs(a + b - 2 * c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                row[i] = (row[i] + pr) & 0xFF
        out += row
        prev = row
    return bytes(out)


# =============================================================================
# 3. DOCUMENTO PDF (leitura)
# =============================================================================


class Page:
    """Uma página de um PDF de origem."""

    __slots__ = ("doc", "num", "dict", "inherited", "label")

    def __init__(self, doc, ref, page_dict, inherited):
        self.doc = doc
        self.num = ref.num if isinstance(ref, Ref) else None
        self.dict = page_dict
        self.inherited = inherited
        self.label = ""

    def size(self):
        box = self.dict.get("MediaBox") or self.inherited.get("MediaBox") or [0, 0, 612, 792]
        try:
            box = self.doc.resolve(box)
            return float(box[2]) - float(box[0]), float(box[3]) - float(box[1])
        except Exception:
            return 612.0, 792.0

    def rotation(self):
        try:
            return int(self.doc.resolve(self.dict.get("Rotate", self.inherited.get("Rotate", 0))) or 0)
        except Exception:
            return 0

    def __repr__(self):  # pragma: no cover
        return "<Page %s>" % self.num


class PDFDocument:
    """Leitor de PDF com suporte a xref clássico, xref streams e object streams."""

    INHERITABLE = ("Resources", "MediaBox", "CropBox", "Rotate")

    def __init__(self, data, source=""):
        if isinstance(data, bytearray):
            data = bytes(data)
        if not isinstance(data, bytes):
            raise TypeError("PDFDocument espera bytes")
        self.data = data
        self.source = source
        self.xref = {}
        self.trailer = {}
        self.cache = {}
        self._deleted = set()
        self._loading = set()
        self._objstm_cache = {}
        self._read_xref()
        root = self.trailer.get("Root")
        if root is None:
            raise PDFError("PDF sem catálogo (/Root)")
        if "Encrypt" in self.trailer:
            raise PDFError(
                "PDF criptografado/protegido por senha não é suportado. "
                "Remova a proteção antes de usar."
            )

    # -- construtor a partir de arquivo -------------------------------------
    @classmethod
    def open(cls, path):
        with open(path, "rb") as fh:
            return cls(fh.read(), source=path)

    # -- xref ---------------------------------------------------------------
    def _skip_ws_at(self, p):
        data = self.data
        n = len(data)
        while p < n:
            c = data[p]
            if _is_ws(c):
                p += 1
            elif c == 37:
                e = data.find(b"\n", p)
                p = n if e < 0 else e + 1
            else:
                break
        return p

    def _read_xref(self):
        start = self._find_startxref()
        if start is None:
            self._rebuild_by_scan()
            return
        seen = set()
        offset = start
        try:
            while offset is not None and offset not in seen and 0 <= offset < len(self.data):
                seen.add(offset)
                entries, trailer = self._read_xref_section(offset)
                self._merge_entries(entries)
                for key, value in trailer.items():
                    if key == "Prev" or key == "XRefStm":
                        continue
                    if key not in self.trailer:
                        self.trailer[key] = value
                prev = trailer.get("Prev")
                if isinstance(prev, Ref):
                    prev = self.resolve(prev)
                offset = int(prev) if isinstance(prev, (int, float)) else None
        except Exception:
            # arquivo danificado: tenta reconstruir
            self.xref = {}
            self.trailer = {}
            self._deleted = set()
            self._rebuild_by_scan()
            return
        if not self.trailer.get("Root"):
            self._rebuild_by_scan()

    def _find_startxref(self):
        pos = self.data.rfind(b"startxref")
        if pos < 0:
            return None
        m = re.match(rb"startxref\s+(\d+)", self.data[pos:])
        return int(m.group(1)) if m else None

    def _merge_entries(self, entries):
        for num, entry in entries.items():
            if entry is None:
                self._deleted.add(num)
                continue
            if num in self._deleted:
                continue
            if num not in self.xref:
                self.xref[num] = entry

    def _read_xref_section(self, offset):
        data = self.data
        p = self._skip_ws_at(offset)
        if data.startswith(b"xref", p):
            return self._parse_xref_table(p)
        num, obj = self.parse_indirect(p)
        if not isinstance(obj, Stream) or str(obj.dict.get("Type")) != "XRef":
            raise PDFError("xref inválido")
        entries = self._parse_xref_stream(obj)
        return entries, obj.dict

    def _parse_xref_table(self, p):
        data = self.data
        p += 4
        entries = {}
        trailer = {}
        while True:
            p = self._skip_ws_at(p)
            if data.startswith(b"trailer", p):
                lex = Lexer(data, self)
                trailer = lex.parse_value_at(p + 7)
                break
            m = re.match(rb"(\d+)\s+(\d+)", data[p:p + 32])
            if not m:
                raise PDFError("tabela xref inválida")
            first = int(m.group(1))
            count = int(m.group(2))
            p += m.end()
            p = self._skip_ws_at(p)
            for i in range(count):
                chunk = data[p:p + 20]
                m2 = re.match(rb"(\d{1,10})\s+(\d{1,5})\s+([nf])", chunk)
                if not m2:
                    raise PDFError("entrada xref inválida")
                if m2.group(3) == b"n":
                    entries[first + i] = ("o", int(m2.group(1)), int(m2.group(2)))
                p += 20
        # xref híbrido: o stream tem os objetos comprimidos
        stm = trailer.get("XRefStm")
        if isinstance(stm, (int, float)):
            try:
                _, obj = self.parse_indirect(int(stm))
                if isinstance(obj, Stream):
                    for num, ent in self._parse_xref_stream(obj).items():
                        entries.setdefault(num, ent)
            except Exception:
                pass
        return entries, trailer

    def _parse_xref_stream(self, stream):
        d = stream.dict
        raw = self.stream_data(stream)
        widths = d.get("W")
        if not isinstance(widths, list):
            raise PDFError("xref stream sem /W")
        widths = [int(self.resolve(x) or 0) for x in widths]
        size = int(self.resolve(d.get("Size", 0)) or 0)
        index = self.resolve(d.get("Index"))
        if not isinstance(index, list):
            index = [0, size]
        index = [int(self.resolve(x) or 0) for x in index]
        rec = sum(widths)
        entries = {}
        pos = 0
        for k in range(0, len(index) - 1, 2):
            first, count = index[k], index[k + 1]
            for j in range(count):
                if pos + rec > len(raw):
                    break
                fields = []
                for w in widths:
                    if w:
                        fields.append(int.from_bytes(raw[pos:pos + w], "big"))
                    else:
                        fields.append(None)
                    pos += w
                typ = fields[0] if widths[0] else 1
                num = first + j
                if typ == 0:
                    entries[num] = None
                elif typ == 1:
                    entries[num] = ("o", fields[1] or 0, fields[2] or 0)
                elif typ == 2:
                    entries[num] = ("c", fields[1] or 0, fields[2] or 0)
        return entries

    def _rebuild_by_scan(self):
        """Reconstrói a tabela xref procurando 'N G obj' no arquivo inteiro."""
        self.xref = {}
        self.cache = {}
        self._deleted = set()
        for m in re.finditer(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b", self.data):
            self.xref[int(m.group(1))] = ("o", m.start(), int(m.group(2)))
        # procura trailer
        for m in reversed(list(re.finditer(rb"trailer", self.data))):
            try:
                lex = Lexer(self.data, self)
                t = lex.parse_value_at(m.end())
                if isinstance(t, dict) and t.get("Root"):
                    for key, value in t.items():
                        self.trailer.setdefault(key, value)
                    break
            except Exception:
                continue
        # procura catálogo
        if not self.trailer.get("Root"):
            for num in sorted(self.xref):
                try:
                    obj = self.get(num)
                except Exception:
                    continue
                if isinstance(obj, dict) and str(obj.get("Type")) == "Catalog":
                    self.trailer["Root"] = Ref(num, 0)
                    break
        if not self.trailer.get("Root"):
            raise PDFError("PDF inválido: catálogo não encontrado")

    # -- acesso a objetos ---------------------------------------------------
    def parse_indirect(self, offset):
        data = self.data
        lex = Lexer(data, self)
        lex.pos = offset
        lex.skip_ws()
        m = re.match(rb"(\d+)\s+(\d+)\s+obj\b", data[lex.pos:lex.pos + 64])
        if not m:
            raise PDFError("objeto indireto inválido em %d" % offset)
        num = int(m.group(1))
        lex.pos += m.end()
        obj = lex.parse_value()
        lex.skip_ws()
        if isinstance(obj, dict) and data.startswith(b"stream", lex.pos):
            obj = self._read_stream(lex, obj)
        return num, obj

    def _read_stream(self, lex, d):
        data = self.data
        p = lex.pos + 6  # depois de "stream"
        if data[p:p + 2] == b"\r\n":
            p += 2
        elif data[p:p + 1] in (b"\r", b"\n"):
            p += 1
        length = None
        if "Length" in d:
            try:
                length = int(self.resolve(d["Length"]))
            except Exception:
                length = None
        if length is not None and 0 <= length <= len(data) - p:
            end = data.find(b"endstream", p + length)
            if end != -1 and data[p + length:end].strip() == b"":
                lex.pos = end + 9
                return Stream(d, data[p:p + length])
        # plano B: procura "endstream"
        end = data.find(b"endstream", p)
        if end < 0:
            end = len(data)
        raw = data[p:end]
        if raw.endswith(b"\r\n"):
            raw = raw[:-2]
        elif raw.endswith((b"\r", b"\n")):
            raw = raw[:-1]
        lex.pos = end + 9
        return Stream(d, raw)

    def get(self, num):
        num = int(num)
        if num in self.cache:
            return self.cache[num]
        if num in self._loading:
            return None
        entry = self.xref.get(num)
        if entry is None:
            return None
        self._loading.add(num)
        try:
            if entry[0] == "o":
                try:
                    _, obj = self.parse_indirect(entry[1])
                except Exception:
                    obj = None
                self.cache[num] = obj
            else:
                try:
                    obj = self._get_from_objstm(entry[1], entry[2])
                except Exception:
                    obj = None
                self.cache[num] = obj
        finally:
            self._loading.discard(num)
        return self.cache[num]

    def _get_from_objstm(self, container, index):
        entry = self._objstm_cache.get(container)
        if entry is None:
            stm = self.get(container) if not isinstance(container, Stream) else container
            if not isinstance(stm, Stream):
                raise PDFError("object stream inválido")
            raw = self.stream_data(stm)
            first = int(self.resolve(stm.dict.get("First", 0)) or 0)
            count = int(self.resolve(stm.dict.get("N", 0)) or 0)
            numbers = raw[:first].split()
            pairs = []
            for i in range(count):
                if 2 * i + 1 < len(numbers):
                    pairs.append((int(numbers[2 * i]), int(numbers[2 * i + 1])))
            entry = (raw, first, pairs)
            self._objstm_cache[container] = entry
        raw, first, pairs = entry
        if index < 0 or index >= len(pairs):
            raise PDFError("índice inválido em object stream")
        objnum, rel = pairs[index]
        lex = Lexer(raw, self)
        lex.pos = first + rel
        return lex.parse_value()

    def resolve(self, obj):
        depth = 0
        while isinstance(obj, Ref):
            depth += 1
            if depth > 64:
                raise PDFError("referência circular")
            obj = self.get(obj.num)
        return obj

    def stream_data(self, stream):
        data = stream.data
        filters = self.resolve(stream.dict.get("Filter"))
        parms = self.resolve(stream.dict.get("DecodeParms"))
        if filters is None:
            return data
        if not isinstance(filters, list):
            filters = [filters]
            parms = [parms]
        else:
            if not isinstance(parms, list):
                parms = [parms] * len(filters)
        for i, filt in enumerate(filters):
            filt = str(filt)
            parm = self.resolve(parms[i]) if i < len(parms) else None
            if filt in ("FlateDecode", "Fl"):
                data = _flate_decode(data)
                data = _apply_predictor(data, parm if isinstance(parm, dict) else None)
            elif filt in ("ASCIIHexDecode", "AHx"):
                data = _asciihex_decode(data)
            elif filt in ("ASCII85Decode", "A85"):
                data = _ascii85_decode(data)
            elif filt in ("RunLengthDecode", "RL"):
                data = _runlength_decode(data)
            elif filt in ("LZWDecode", "LZW"):
                data = _lzw_decode(data)
                data = _apply_predictor(data, parm if isinstance(parm, dict) else None)
            elif filt in ("Crypt",):
                raise PDFError("PDF criptografado não suportado")
            else:
                raise PDFError("filtro de stream não suportado: %s" % filt)
        return data

    # -- páginas ------------------------------------------------------------
    def pages(self):
        root = self.resolve(self.trailer.get("Root"))
        if not isinstance(root, dict):
            raise PDFError("catálogo inválido")
        pages_ref = root.get("Pages")
        result = []
        seen = set()

        def walk(ref, inherited, depth):
            if depth > 64:
                return
            num = ref.num if isinstance(ref, Ref) else None
            if num is not None:
                if num in seen:
                    return
                seen.add(num)
            node = self.resolve(ref)
            if not isinstance(node, dict):
                return
            inh = dict(inherited)
            for key in self.INHERITABLE:
                if key in node:
                    inh[key] = node[key]
            kids = self.resolve(node.get("Kids"))
            typ = str(node.get("Type", ""))
            if typ == "Page" or (kids is None and "Contents" in node):
                result.append(Page(self, ref, node, inh))
            elif isinstance(kids, list):
                for kid in kids:
                    walk(kid, inh, depth + 1)

        walk(pages_ref, {}, 0)
        for i, page in enumerate(result, 1):
            page.label = "página %d" % i
        return result

    @property
    def page_count(self):
        try:
            return len(self.pages())
        except PDFError:
            return 0


# =============================================================================
# 4. ESCRITOR DE PDF
# =============================================================================


def _fmt_num(value):
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, int):
        return str(value).encode("ascii")
    if isinstance(value, float):
        if value == int(value):
            return str(int(value)).encode("ascii")
        text = ("%.6f" % value).rstrip("0").rstrip(".")
        if text in ("", "-0"):
            text = "0"
        return text.encode("ascii")
    return str(value).encode("ascii")


def _escape_name(name):
    out = []
    for ch in str(name):
        code = ord(ch)
        if 33 <= code <= 126 and ch not in "()<>[]{}/%#":
            out.append(ch)
        else:
            out.append("#%02X" % (code & 0xFF))
    return "".join(out)


def pdf_literal(text):
    """Converte texto Python em string literal PDF (codificação WinAnsi)."""
    if isinstance(text, str):
        raw = text.encode("cp1252", "replace")
    else:
        raw = bytes(text)
    out = bytearray(b"(")
    for ch in raw:
        if ch in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([ch])
        elif ch == 0x0A:
            out += b"\\n"
        elif ch == 0x0D:
            out += b"\\r"
        elif ch == 0x09:
            out += b"\\t"
        elif ch < 32 or ch > 126:
            out += b"\\%03o" % ch
        else:
            out.append(ch)
    out += b")"
    return bytes(out)


def _serialize(value):
    if value is None:
        return b"null"
    if value is True:
        return b"true"
    if value is False:
        return b"false"
    if isinstance(value, Ref):
        return b"%d %d R" % (value.num, value.gen)
    if isinstance(value, Name):
        return b"/" + _escape_name(value).encode("latin-1")
    if isinstance(value, str):
        return pdf_literal(value)
    if isinstance(value, (bytes, bytearray)):
        return pdf_literal(bytes(value))
    if isinstance(value, (int, float)):
        return _fmt_num(value)
    if isinstance(value, (list, tuple)):
        return b"[" + b" ".join(_serialize(item) for item in value) + b" ]"
    if isinstance(value, dict):
        parts = []
        for key, val in value.items():
            parts.append(b"/" + _escape_name(key).encode("latin-1") + b" " + _serialize(val))
        return b"<<" + b" ".join(parts) + b" >>"
    raise PDFError("tipo não serializável em PDF: %r" % (type(value),))


class PDFWriter:
    """Gera um arquivo PDF simples (xref clássico)."""

    def __init__(self):
        self.objects = []

    def reserve(self):
        self.objects.append(None)
        return len(self.objects)

    def set(self, num, obj):
        self.objects[num - 1] = obj

    def add(self, obj):
        num = self.reserve()
        self.objects[num - 1] = obj
        return num

    def save(self, path, root_ref, info_ref=None):
        buf = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0] * (len(self.objects) + 1)
        for i, obj in enumerate(self.objects, 1):
            if obj is None:
                obj = None
            offsets[i] = len(buf)
            buf += b"%d 0 obj\n" % i
            if isinstance(obj, Stream):
                d = dict(obj.dict)
                raw = obj.data
                if getattr(obj, "compress", False):
                    raw = zlib.compress(raw, 6)
                d["Length"] = len(raw)
                buf += _serialize(d) + b"\nstream\n" + raw + b"\nendstream"
            else:
                buf += _serialize(obj)
            buf += b"\nendobj\n"
        xref_pos = len(buf)
        total = len(self.objects) + 1
        buf += b"xref\n0 %d\n" % total
        buf += b"0000000000 65535 f \n"
        for i in range(1, total):
            buf += b"%010d 00000 n \n" % offsets[i]
        trailer = {"Size": total, "Root": root_ref}
        if info_ref is not None:
            trailer["Info"] = info_ref
        buf += b"trailer\n" + _serialize(trailer) + b"\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
        parent = os.path.dirname(os.path.abspath(path))
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        tmp = str(path) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(buf)
        os.replace(tmp, path)


class Importer:
    """Copia objetos de um PDF de origem para o PDF de destino."""

    def __init__(self, doc, writer):
        self.doc = doc
        self.writer = writer
        self.memo = {}
        self.failed = []

    def ref(self, ref):
        num = ref.num
        if num in self.memo:
            return Ref(self.memo[num], 0)
        new_num = self.writer.reserve()
        self.memo[num] = new_num
        try:
            obj = self.doc.get(num)
        except Exception:
            obj = None
        if obj is None:
            self.writer.set(new_num, None)
        else:
            try:
                self.writer.set(new_num, self.value(obj))
            except Exception:
                self.writer.set(new_num, None)
        return Ref(new_num, 0)

    def value(self, obj):
        if isinstance(obj, Ref):
            return self.ref(obj)
        if isinstance(obj, Stream):
            d = {}
            for key, val in obj.dict.items():
                if key == "Length":
                    continue
                d[key] = self.value(val)
            d["Length"] = len(obj.data)
            return Stream(d, obj.data, getattr(obj, "compress", False))
        if isinstance(obj, dict):
            return {key: self.value(val) for key, val in obj.items()}
        if isinstance(obj, list):
            return [self.value(val) for val in obj]
        return obj

    def add_page(self, page):
        d = dict(page.dict)
        for key, val in page.inherited.items():
            d.setdefault(key, val)
        d.pop("Parent", None)
        d.pop("Annots", None)   # evita referências circulares e formulários quebrados
        d.pop("B", None)        # article beads
        d["Type"] = Name("Page")
        if "MediaBox" not in d:
            d["MediaBox"] = [0, 0, 612, 792]
        d.setdefault("Resources", {})
        new_dict = self.value(d)
        new_num = self.writer.add(new_dict)
        return Ref(new_num, 0), new_dict


# =============================================================================
# 5. MONTAGEM DE NOVOS PDFs
# =============================================================================


def pdf_date(when=None):
    when = when or datetime.datetime.now()
    return when.strftime("D:%Y%m%d%H%M%S")


def build_output(path, page_list, title="Documento"):
    """page_list: [(PDFDocument, Page), ...] já na ordem desejada."""
    writer = PDFWriter()
    importers = {}
    kids = []
    dicts = []
    for doc, page in page_list:
        key = id(doc)
        imp = importers.get(key)
        if imp is None:
            imp = Importer(doc, writer)
            importers[key] = imp
        pref, pdoc = imp.add_page(page)
        kids.append(pref)
        dicts.append(pdoc)
    pages_num = writer.reserve()
    pages_obj = {"Type": Name("Pages"), "Kids": kids, "Count": len(kids)}
    for pdoc in dicts:
        pdoc["Parent"] = Ref(pages_num, 0)
    writer.set(pages_num, pages_obj)
    catalog = {
        "Type": Name("Catalog"),
        "Pages": Ref(pages_num, 0),
        "Lang": "pt-BR",
    }
    catalog_num = writer.add(catalog)
    info_num = writer.add({
        "Producer": "Gerador de PDFs Aleatorios (Python)",
        "Creator": "Gerador de PDFs Aleatorios",
        "Title": title,
        "CreationDate": pdf_date(),
        "ModDate": pdf_date(),
    })
    writer.save(path, Ref(catalog_num, 0), Ref(info_num, 0))
    return path


def merge_pdfs(paths, out_path, pages_per_pdf=1, shuffle=True, repeat=True,
               progress=None, title="Documento"):
    """Modo 1: sorteia páginas de vários PDFs e monta um novo."""
    docs = []
    for p in paths:
        if progress:
            progress("Lendo %s ..." % os.path.basename(p))
        docs.append(PDFDocument.open(p))
    pool = []
    for doc in docs:
        pool.extend((doc, page) for page in doc.pages())
    if not pool:
        raise PDFError("os PDFs de origem não têm páginas")
    count = int(pages_per_pdf)
    if count <= 0:
        raise PDFError("número de páginas inválido")
    if count <= len(pool):
        chosen = random.sample(pool, count)
    elif repeat:
        chosen = [random.choice(pool) for _ in range(count)]
    else:
        chosen = list(pool)
        random.shuffle(chosen)
        chosen = chosen[:count]
    if not shuffle:
        chosen.sort(key=lambda item: (id(item[0]), item[1].num or 0))
    return build_output(out_path, chosen, title)


# =============================================================================
# 6. GERAÇÃO DE CONTEÚDO ALEATÓRIO (texto, fotos, layout)
# =============================================================================

# =============================================================================
# 6. IMAGENS (JPG e PNG) SEM BIBLIOTECAS EXTERNAS
# =============================================================================

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_IMAGE_CACHE = {}


def load_image(path):
    """Lê JPG/PNG e devolve pixels brutos + metadados para embutir no PDF."""
    try:
        stat = os.stat(path)
        key = (os.path.abspath(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        key = (os.path.abspath(path), 0, 0)
    cached = _IMAGE_CACHE.get(key)
    if cached is not None:
        return cached
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] == _PNG_SIG:
        info = _load_png(data)
    elif data[:2] == b"\xff\xd8":
        info = _load_jpeg(data)
    else:
        raise PDFError(
            "formato de imagem não suportado (use JPG ou PNG): %s" % os.path.basename(path)
        )
    info["path"] = path
    _IMAGE_CACHE[key] = info
    return info


def _load_jpeg(data):
    width = height = None
    components = 3
    i = 2
    n = len(data)
    while i < n - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            components = data[i + 9]
            break
        i += 2 + max(length, 2)
    if not width or not height:
        raise PDFError("JPEG inválido")
    colorspace = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(components)
    if colorspace is None:
        raise PDFError("JPEG com %d componentes não suportado" % components)
    return {
        "width": width,
        "height": height,
        "colorspace": colorspace,
        "filter": "DCTDecode",
        "data": data,
    }


def _paeth(a, b, c):
    pa = abs(b - c)
    pb = abs(a - c)
    pc = abs(a + b - 2 * c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_unfilter(raw, width, height, bit_depth, color_type, channels):
    if bit_depth == 8:
        bpp = channels
        rowlen = width * channels
    elif bit_depth == 16:
        bpp = channels * 2
        rowlen = width * channels * 2
    else:
        bpp = 1
        rowlen = (width * color_type_bits(color_type) * bit_depth + 7) // 8
    out = bytearray()
    prev = bytearray(rowlen)
    pos = 0
    for _ in range(height):
        if pos >= len(raw):
            break
        ftype = raw[pos]
        pos += 1
        row = bytearray(raw[pos:pos + rowlen])
        if len(row) < rowlen:
            row.extend(b"\x00" * (rowlen - len(row)))
        pos += rowlen
        if ftype == 1:
            for i in range(bpp, rowlen):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(rowlen):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(rowlen):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(rowlen):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + _paeth(a, b, c)) & 0xFF
        out += row
        prev = row
    return out


def color_type_bits(color_type):
    return {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type, 1)


def _unpack_bits(row, width, bit_depth):
    """Expande pixels de 1/2/4 bits para uma lista de índices."""
    if bit_depth == 8:
        return list(row[:width])
    per_byte = 8 // bit_depth
    mask = (1 << bit_depth) - 1
    out = []
    for byte in row:
        for k in range(per_byte):
            shift = 8 - bit_depth * (k + 1)
            out.append((byte >> shift) & mask)
            if len(out) >= width:
                return out
    return out


def _load_png(data):
    pos = 8
    width = height = 0
    bit_depth = 8
    color_type = 0
    interlace = 0
    palette = None
    trns = None
    idat = bytearray()
    n = len(data)
    while pos + 8 <= n:
        length = int.from_bytes(data[pos:pos + 4], "big")
        ctype = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width = int.from_bytes(chunk[0:4], "big")
            height = int.from_bytes(chunk[4:8], "big")
            bit_depth = chunk[8]
            color_type = chunk[9]
            interlace = chunk[12]
        elif ctype == b"PLTE":
            palette = chunk
        elif ctype == b"tRNS":
            trns = chunk
        elif ctype == b"IDAT":
            idat += chunk
        elif ctype == b"IEND":
            break
    if not width or not height:
        raise PDFError("PNG inválido")
    if interlace:
        raise PDFError("PNG entrelaçado não é suportado; salve como PNG normal")
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error:
        raise PDFError("PNG com dados corrompidos")

    if color_type not in (0, 2, 3, 4, 6):
        raise PDFError("PNG com tipo de cor não suportado")
    channels = color_type_bits(color_type)
    if bit_depth not in (1, 2, 4, 8, 16):
        raise PDFError("PNG com profundidade de bits não suportada")
    if color_type in (2, 4, 6) and bit_depth not in (8, 16):
        raise PDFError("PNG RGB com profundidade inválida")

    raw = _png_unfilter(raw, width, height, bit_depth, color_type, channels)
    rowlen = len(raw) // height if height else 0

    out = bytearray()
    if color_type == 0:
        # tons de cinza
        for y in range(height):
            row = raw[y * rowlen:(y + 1) * rowlen]
            if bit_depth == 8:
                out += row
            elif bit_depth == 16:
                out += row[0::2]
            else:
                values = _unpack_bits(row, width, bit_depth)
                scale = 255 // ((1 << bit_depth) - 1)
                out += bytes(min(255, v * scale) for v in values)
        return {"width": width, "height": height, "colorspace": "DeviceGray",
                "filter": "FlateDecode", "data": bytes(out)}

    if color_type == 3:
        # paleta -> RGB
        if not palette:
            raise PDFError("PNG com paleta ausente")
        entries = [(palette[i], palette[i + 1], palette[i + 2])
                   for i in range(0, len(palette) - 2, 3)]
        alphas = list(trns) if trns else []
        for y in range(height):
            row = raw[y * rowlen:(y + 1) * rowlen]
            indexes = _unpack_bits(row, width, bit_depth)
            for idx in indexes:
                r, g, b = entries[idx] if idx < len(entries) else (0, 0, 0)
                a = alphas[idx] if idx < len(alphas) else 255
                if a < 255:
                    r = (r * a + 255 * (255 - a)) // 255
                    g = (g * a + 255 * (255 - a)) // 255
                    b = (b * a + 255 * (255 - a)) // 255
                out += bytes((r, g, b))
        return {"width": width, "height": height, "colorspace": "DeviceRGB",
                "filter": "FlateDecode", "data": bytes(out)}

    # tipos 2, 4 e 6: RGB / cinza+alfa / RGBA
    step = 2 if bit_depth == 16 else 1
    for y in range(height):
        row = raw[y * rowlen:(y + 1) * rowlen]
        if color_type == 2:
            if bit_depth == 16:
                out += bytes(row[0::2])
            else:
                out += row
        elif color_type == 4:
            for x in range(width):
                g = row[x * 2 * step]
                a = row[x * 2 * step + step]
                g = (g * a + 255 * (255 - a)) // 255
                out += bytes((g, g, g))
        else:  # 6
            for x in range(width):
                base = x * 4 * step
                r = row[base]
                g = row[base + step]
                b = row[base + 2 * step]
                a = row[base + 3 * step]
                if a < 255:
                    r = (r * a + 255 * (255 - a)) // 255
                    g = (g * a + 255 * (255 - a)) // 255
                    b = (b * a + 255 * (255 - a)) // 255
                out += bytes((r, g, b))
    colorspace = "DeviceGray" if color_type == 4 else "DeviceRGB"
    return {"width": width, "height": height, "colorspace": colorspace,
            "filter": "FlateDecode", "data": bytes(out)}


# =============================================================================
# 7. TEXTO ALEATÓRIO EM PORTUGUÊS
# =============================================================================

_SUJEITOS = [
    "a equipe técnica", "o setor responsável", "a coordenação do projeto",
    "o fornecedor", "a comissão de análise", "o relatório", "o sistema",
    "a diretoria", "o grupo de trabalho", "a auditoria",
    "o departamento financeiro", "a área de qualidade", "o responsável técnico",
    "a empresa contratada", "o setor de compras", "a unidade regional",
    "o núcleo de planejamento", "a secretaria de administração",
]
_VERBOS = [
    "avaliou", "verificou", "registrou", "analisou", "encaminhou", "aprovou",
    "revisou", "confirmou", "identificou", "considerou", "destacou",
    "informou", "solicitou", "concluiu", "apresentou", "acompanha",
    "documentou", "comparou", "validou", "atualizou",
]
_OBJETOS = [
    "os documentos do projeto", "as informações apresentadas",
    "o cronograma de atividades", "os resultados da última medição",
    "as pendências identificadas", "o orçamento previsto",
    "os requisitos contratuais", "as evidências coletadas", "o plano de ação",
    "os indicadores de desempenho", "as normas aplicáveis",
    "o escopo do serviço", "os registros de atendimento",
    "as metas estabelecidas", "o inventário de materiais",
    "a documentação técnica", "os prazos acordados",
    "as condições de segurança",
]
_ADVERBIOS = [
    "durante a reunião", "no período analisado", "conforme o cronograma",
    "após a revisão", "de forma detalhada", "em conjunto com a equipe",
    "nos termos do contrato", "com base nos dados disponíveis",
    "previamente", "de maneira objetiva",
]
_CONECTORES = [
    "Além disso", "Dessa forma", "Por outro lado", "Contudo", "Portanto",
    "Assim", "No entanto", "Ademais", "Em contrapartida", "Nesse contexto",
]
_COMPLEMENTOS = [
    "sem prejuízo das demais cláusulas", "conforme orientação superior",
    "respeitando os prazos estabelecidos", "de acordo com a legislação vigente",
    "observando os procedimentos internos", "para fins de registro",
    "com base nas normas técnicas", "mediante autorização prévia",
]
_SECOES = [
    "Introdução", "Objetivo", "Escopo", "Metodologia", "Análise",
    "Resultados", "Considerações", "Conclusão", "Recomendações",
    "Observações", "Cronograma", "Recursos", "Riscos", "Anexos",
    "Fundamentação", "Diagnóstico", "Plano de ação",
]
_TITULOS = [
    "Relatório de Atividades", "Relatório Técnico",
    "Contrato de Prestação de Serviços", "Laudo de Vistoria",
    "Memorando Interno", "Ata de Reunião", "Proposta Comercial",
    "Termo de Referência", "Plano de Trabalho", "Manual de Procedimentos",
    "Parecer Técnico", "Análise de Desempenho", "Termo de Compromisso",
    "Relatório de Auditoria", "Especificação Técnica",
]
_SUBTITULOS = [
    "Documento para fins de teste", "Versão preliminar", "Uso interno",
    "Confidencial", "Revisão 2", "Circulação restrita",
    "Documento de trabalho", "Minuta para aprovação",
]
_EMPRESAS = [
    "Aurora Soluções", "Nexo Consultoria", "Vetor Engenharia",
    "Prisma Sistemas", "Horizonte Serviços", "Atlas Projetos",
    "Meridiano Gestão", "Norte Administração", "Vértice Tecnologia",
]
_NOMES = [
    "Ana Paula Ferreira", "Carlos Eduardo Lima", "Mariana Souza Alves",
    "Rafael Nogueira", "Juliana Ramos", "Bruno Carvalho", "Patrícia Mendes",
    "Eduardo Martins", "Camila Rocha", "Fernando Oliveira",
]
_CIDADES = [
    "São Paulo", "Rio de Janeiro", "Belo Horizonte", "Curitiba", "Porto Alegre",
    "Salvador", "Recife", "Brasília", "Fortaleza", "Campinas",
]
_WATERMARKS = ["CONFIDENCIAL", "CÓPIA", "USO INTERNO", "RASCUNHO", "MINUTA"]


def _cap(text):
    return text[:1].upper() + text[1:] if text else text


def random_sentence(rng):
    s = rng.choice(_SUJEITOS)
    v = rng.choice(_VERBOS)
    o = rng.choice(_OBJETOS)
    r = rng.random()
    if r < 0.18:
        return "%s %s %s." % (_cap(s), v, o)
    if r < 0.35:
        return "%s, %s %s." % (_cap(rng.choice(_ADVERBIOS)), s, v + " " + o)
    if r < 0.50:
        s2 = rng.choice(_SUJEITOS)
        return "%s %s %s, %s %s %s %s." % (
            _cap(s), v, o, rng.choice(_CONECTORES).lower(),
            s2, rng.choice(_VERBOS), rng.choice(_OBJETOS),
        )
    if r < 0.63:
        return "%s %s %s %s." % (_cap(s), v, o, rng.choice(_COMPLEMENTOS))
    if r < 0.75:
        return "Foram identificados %d itens, o que representa aproximadamente %d%% do total analisado." % (
            rng.randint(3, 48), rng.randint(5, 95),
        )
    if r < 0.86:
        return "O prazo estimado para conclusão é de %d dias úteis, a contar de %02d/%02d/%04d." % (
            rng.randint(2, 45), rng.randint(1, 28), rng.randint(1, 12), rng.randint(2023, 2026),
        )
    return "%s %s %s, conforme registrado no protocolo nº %d/%d." % (
        _cap(s), v, o, rng.randint(100, 9999), rng.randint(2023, 2026),
    )


def random_paragraph(rng, sentences=None):
    n = sentences or rng.randint(2, 5)
    return " ".join(random_sentence(rng) for _ in range(n))


def random_heading(rng):
    r = rng.random()
    if r < 0.45:
        return "%d. %s" % (rng.randint(1, 9), rng.choice(_SECOES))
    if r < 0.8:
        return "%d.%d %s" % (rng.randint(1, 9), rng.randint(1, 6), rng.choice(_SECOES))
    return rng.choice(_SECOES).upper()


def random_bullet(rng):
    verb = rng.choice([
        "Verificar", "Revisar", "Encaminhar", "Registrar", "Atualizar",
        "Validar", "Documentar", "Acompanhar", "Solicitar",
    ])
    return "%s %s." % (verb, rng.choice(_OBJETOS)[2:])


def fake_protocol(rng):
    return "%04d.%02d.%02d-%04d" % (
        rng.randint(2020, 2026), rng.randint(1, 12), rng.randint(1, 28),
        rng.randint(1, 9999),
    )


def strip_accents(text):
    import unicodedata
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


# ----------------------------- métricas de fonte -----------------------------

_HELV_W = {
    " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
    "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
    ".": 278, "/": 278, ":": 278, ";": 278, "<": 584, "=": 584, ">": 584,
    "?": 556, "@": 1015, "A": 667, "B": 667, "C": 722, "D": 722, "E": 667,
    "F": 611, "G": 778, "H": 722, "I": 278, "J": 500, "K": 667, "L": 556,
    "M": 833, "N": 722, "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667,
    "T": 611, "U": 722, "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333, "a": 556,
    "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556,
    "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556, "o": 556,
    "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500,
    "w": 722, "x": 500, "y": 500, "z": 500, "{": 334, "|": 260, "}": 334,
    "~": 584,
}
for _i in range(10):
    _HELV_W[str(_i)] = 556


def char_width(ch):
    if ch in _HELV_W:
        return _HELV_W[ch]
    base = strip_accents(ch)[:1]
    if base and base in _HELV_W:
        return _HELV_W[base]
    return 556


def text_width(font, size, text):
    if font == "F5":
        return size * 0.6 * len(text)
    factor = 0.95 if font in ("F3", "F4") else 1.0
    total = sum(char_width(ch) for ch in text)
    return size * factor * total / 1000.0


def wrap_text(text, font, size, max_width):
    words = str(text).replace("\r", " ").replace("\n", " ").split()
    lines = []
    current = ""
    for word in words:
        if text_width(font, size, word) > max_width:
            # palavra gigante: quebra por caracteres
            if current:
                lines.append(current)
                current = ""
            piece = ""
            for ch in word:
                if piece and text_width(font, size, piece + ch) > max_width:
                    lines.append(piece)
                    piece = ""
                piece += ch
            current = piece
            continue
        trial = word if not current else current + " " + word
        if text_width(font, size, trial) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# =============================================================================
# 8. GERADOR DE PÁGINAS (texto, fotos, layouts, tamanhos)
# =============================================================================

PAGE_SIZES = {
    "A4": (595.28, 841.89),
    "A5": (419.53, 595.28),
    "A3": (841.89, 1190.55),
    "Letter": (612.0, 792.0),
    "Legal": (612.0, 1008.0),
    "Ficha (caderno 90x140mm)": (255.12, 396.85),
    "Quadrada 20x20cm": (566.93, 566.93),
    "Retrato 10x15cm": (283.46, 425.20),
    "Tira 10x25cm": (283.46, 708.66),
}

FONT_NAMES = {
    "F1": ("Helvetica", False, False),
    "F2": ("Helvetica-Bold", True, False),
    "F3": ("Helvetica-Oblique", False, True),
    "F4": ("Helvetica-BoldOblique", True, True),
    "F5": ("Courier", False, False),
    "F6": ("Times-Roman", False, False),
    "F7": ("Times-Bold", True, False),
    "F8": ("Times-Italic", False, True),
}

_BASE14_WIDTHS = {
    "Helvetica": {"Bold": dict(_HELV_W), "BoldItalic": dict(_HELV_W)},
}


def random_size(rng):
    """Tamanho de página aleatório, sempre em pé (retrato)."""
    choice = rng.random()
    if choice < 0.55:
        w, h = PAGE_SIZES[rng.choice(["A4", "A4", "A4", "Letter", "A5", "Legal"])]
    elif choice < 0.75:
        w = rng.randint(300, 620)
        h = rng.randint(400, 900)
    else:
        w = rng.uniform(250, 600)
        h = w * rng.uniform(1.30, 1.55)
    if h < w:
        w, h = h, w
    return float(round(w, 2)), float(round(h, 2))


def resolve_page_size(spec, rng):
    """Aceita 'A4', 'A4 retrato', 'A4 paisagem', '10x15cm', '210x297mm', '500x700' ou 'aleatorio'."""
    if spec is None:
        return random_size(rng)
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        return float(spec[0]), float(spec[1])
    text = str(spec).strip()
    low = strip_accents(text).lower()
    if low in ("", "aleatorio", "aleatoria", "random", "auto"):
        return random_size(rng)
    landscape = "paisagem" in low or "landscape" in low
    portrait = "retrato" in low or "portrait" in low
    for key in PAGE_SIZES:
        if strip_accents(key.split(" ")[0]).lower() in low:
            w, h = PAGE_SIZES[key]
            if landscape and not portrait:
                return h, w
            return w, h
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*x\s*(\d+(?:[.,]\d+)?)\s*(mm|cm|pt|in|pol)?", low)
    if m:
        w = float(m.group(1).replace(",", "."))
        h = float(m.group(2).replace(",", "."))
        unit = m.group(3) or "pt"
        factor = {"mm": 72 / 25.4, "cm": 72 / 2.54, "pt": 1.0, "in": 72.0, "pol": 72.0}[unit]
        w, h = w * factor, h * factor
        if landscape and not portrait and h > w:
            w, h = h, w
        return w, h
    raise PDFError("tamanho de página não reconhecido: %r" % text)


class PageSink:
    """Guarda páginas já desenhadas (tamanho, conteúdo, recursos) até gravar."""

    def __init__(self, title="Documento"):
        self.pages = []
        self.title = title
        self._images = {}

    def add(self, size, content, resources=None):
        self.pages.append((size, content, resources or {"Font": self.font_resource()}))
        return len(self.pages)

    @staticmethod
    def font_resource():
        fonts = {}
        for key, (base, bold, italic) in FONT_NAMES.items():
            fonts[key] = {"Type": Name("Font"), "Subtype": Name("Type1"),
                          "BaseFont": Name(base), "Encoding": Name("WinAnsiEncoding")}
        return fonts

    def image_ref(self, writer, info):
        key = (info.get("path"), info["width"], info["height"], info["filter"])
        cached = self._images.get(key)
        if cached is not None:
            return cached
        data = info["data"]
        d = {
            "Type": Name("XObject"),
            "Subtype": Name("Image"),
            "Width": info["width"],
            "Height": info["height"],
            "ColorSpace": Name(info["colorspace"]),
            "BitsPerComponent": 8,
            "Filter": Name(info["filter"]),
        }
        if info["filter"] == "DCTDecode":
            num = writer.add(Stream(d, data))
        else:
            # pixels crus comprimidos com zlib (sem preditor: PDF puro)
            d["Filter"] = Name("FlateDecode")
            num = writer.add(Stream(d, data, compress=True))
        ref = Ref(num, 0)
        self._images[key] = ref
        return ref

    def save(self, path, writer=None):
        if not self.pages:
            raise PDFError("nenhuma página gerada")
        writer = writer or PDFWriter()
        pages_num = writer.reserve()
        kids = []
        for size, content, res in self.pages:
            xobjs = {}
            for name, info in res.get("_images", {}).items():
                xobjs[name] = self.image_ref(writer, info)
            resources = dict(res)
            resources.pop("_images", None)
            if xobjs:
                resources["XObject"] = xobjs
            stream = Stream({"Length": len(content)}, content)
            contents_num = writer.add(stream)
            page = {
                "Type": Name("Page"),
                "Parent": Ref(pages_num, 0),
                "MediaBox": [0, 0, size[0], size[1]],
                "Resources": resources,
                "Contents": Ref(contents_num, 0),
            }
            kids.append(Ref(writer.add(page), 0))
        writer.set(pages_num, {"Type": Name("Pages"), "Kids": kids, "Count": len(kids)})
        catalog_num = writer.add({"Type": Name("Catalog"), "Pages": Ref(pages_num, 0),
                                  "Lang": "pt-BR"})
        info_num = writer.add({
            "Producer": "Gerador de PDFs Aleatorios (Python)",
            "Creator": "Gerador de PDFs Aleatorios",
            "Title": self.title,
            "CreationDate": pdf_date(),
            "ModDate": pdf_date(),
        })
        writer.save(path, Ref(catalog_num, 0), Ref(info_num, 0))
        return path


class PageBuilder:
    """Desenha textos e imagens numa página (coordenadas de cima para baixo)."""

    def __init__(self, width, height, margin=40.0):
        self.width = width
        self.height = height
        self.margin = margin
        self.ops = bytearray()
        self.images = {}
        self.fonts = PageSink.font_resource()
        self.y = height - margin
        self._extra_fonts = 0

    # -- primitivas ---------------------------------------------------------
    def font(self, key, size):
        if key not in self.fonts and key not in ("Helvetica", "Times-Roman"):
            self.fonts[key] = {"Type": Name("Font"), "Subtype": Name("Type1"),
                               "BaseFont": Name(key), "Encoding": Name("WinAnsiEncoding")}
        self.ops += ("/%s %.2f Tf" % (key, size)).encode("ascii") + b"\n"

    def color(self, rgb, stroke=False):
        cmd = b"RG" if stroke else b"rg"
        self.ops += ("%.3f %.3f %.3f " % rgb).encode("ascii") + cmd + b"\n"

    def text(self, x, y_top, s, key="Helvetica", size=11.0, rgb=(0, 0, 0), align="left", width=None):
        s = str(s)
        if not s:
            return
        if align in ("center", "right") and width is not None:
            w = text_width(key, size, s)
            if align == "center":
                x = x + (width - w) / 2.0
            else:
                x = x + width - w
        self.color(rgb)
        self.ops += b"BT\n"
        self.ops += ("1 0 0 1 %.2f %.2f Tm" % (x, y_top - size * 0.82)).encode("ascii") + b"\n"
        self.font(key, size)
        self.ops += pdf_literal(s) + b" Tj\nET\n"

    def line(self, x1, y1, x2, y2, rgb=(0.6, 0.6, 0.6), width=0.7):
        self.color(rgb, stroke=True)
        self.ops += ("%.2f w %.2f %.2f m %.2f %.2f l S\n" % (width, x1, y1, x2, y2)).encode("ascii")

    def rect(self, x, y_top, w, h, rgb=None, stroke=None, line_width=0.8):
        if rgb is not None:
            self.color(rgb)
            self.ops += ("%.2f %.2f %.2f %.2f re f\n" % (x, y_top - h, w, h)).encode("ascii")
        if stroke is not None:
            self.color(stroke, stroke=True)
            self.ops += ("%.2f w %.2f %.2f %.2f %.2f re S\n" % (line_width, x, y_top - h, w, h)).encode("ascii")

    def image(self, info, x, y_top, w, h):
        name = "Im%d" % (len(self.images) + 1)
        self.images[name] = info
        self.ops += b"q\n%.2f 0 0 %.2f %.2f %.2f cm\n/%s Do\nQ\n" % (w, h, x, y_top - h, name.encode())
        return name

    def fit_image(self, info, x, y_top, max_w, max_h):
        iw, ih = info["width"], info["height"]
        scale = min(max_w / float(iw), max_h / float(ih))
        w, h = iw * scale, ih * scale
        self.image(info, x + (max_w - w) / 2.0, y_top - (max_h - h) / 2.0, w, h)
        return w, h

    def resources(self):
        res = {"Font": self.fonts}
        if self.images:
            res["_images"] = self.images
        return res

    def content(self):
        return bytes(self.ops)


def _image_list(images):
    """Aceita caminhos, dicts do load_image ou listas."""
    out = []
    for item in images or []:
        if isinstance(item, dict) and "width" in item and "data" in item:
            out.append(item)
        else:
            out.append(load_image(str(item)))
    return out


def _content_pool(texts, rng):
    """Transforma entrada de textos em lista de parágrafos."""
    pool = []
    for t in texts or []:
        t = str(t)
        for chunk in re.split(r"\n\s*\n", t):
            chunk = " ".join(chunk.split())
            if chunk:
                pool.append(chunk)
    if not pool:
        pool = [random_paragraph(rng) for _ in range(6)]
    return pool


def draw_document_page(builder, rng, opts, is_first, pool, images):
    """Desenha uma página de documento (cabeçalho, parágrafos, listas, fotos)."""
    w, h = builder.width, builder.height
    m = builder.margin
    width = w - 2 * m
    body = "F6" if opts.get("serif") else "F1"
    size = float(opts.get("font_size", 11))
    f_bold = "F7" if opts.get("serif") else "F2"
    f_italic = "F8" if opts.get("serif") else "F3"
    spacing = 1.35
    body_rgb = (0.12, 0.12, 0.14)

    if is_first and opts.get("headings", True):
        tkey = f_bold
        tsize = size * 1.85
        builder.text(m, builder.y, opts.get("title") or rng.choice(_TITULOS), tkey, tsize,
                     align="center", width=width)
        builder.y -= tsize * 1.35
        if opts.get("org_line", True):
            builder.text(m, builder.y, rng.choice(_EMPRESAS), f_italic,
                         size * 0.95, align="center", width=width, rgb=(0.35, 0.35, 0.4))
            builder.y -= size * 1.4
            builder.text(m, builder.y, "%s • %02d/%02d/%04d • Protocolo %s" % (
                rng.choice(_CIDADES), rng.randint(1, 28), rng.randint(1, 12),
                rng.randint(2023, 2026), fake_protocol(rng)),
                "F5", size * 0.78, align="center", width=width, rgb=(0.4, 0.4, 0.45))
            builder.y -= size * 1.5
        builder.line(m, builder.y, w - m, builder.y, (0.55, 0.55, 0.6), 0.9)
        builder.y -= size * 1.2
    elif opts.get("headings", True) and rng.random() < 0.25:
        tkey = f_bold
        builder.text(m, builder.y, opts.get("title") or rng.choice(_TITULOS), tkey, size * 1.2,
                     rgb=(0.2, 0.2, 0.25))
        builder.y -= size * 1.9

    bottom_color = h * 0.07
    if opts.get("footer", True):
        bottom_color += size * 1.6
    target = max(int(opts.get("paras_per_page", rng.randint(3, 6))), 1)
    paras_done = 0
    images_left = list(images)
    use_images = opts.get("with_images", True) and images_left
    paras_done = 0
    if opts.get("with_image_first", True) and images_left and rng.random() < 0.45:
        info = images_left[0]
        max_h = min(builder.y - bottom_color, h * rng.uniform(0.25, 0.40))
        if max_h > 60:
            builder.fit_image(info, m, builder.y, width, max_h)
            builder.y -= max_h + size * 1.2
    while builder.y > bottom_color and paras_done < target:
        kind = rng.random()
        line_h = size * spacing
        heading_key = f_bold
        # foto
        if use_images and kind < 0.30 and images_left and builder.y > bottom_color + h * 0.30:
            info = rng.choice(images_left)
            max_h = min(builder.y - bottom_color, h * rng.uniform(0.22, 0.42))
            max_w = width * rng.uniform(0.55, 1.0)
            iw, ih = info["width"], info["height"]
            scale = min(max_w / iw, max_h / ih)
            if scale * ih + size * 2.2 > builder.y - bottom_color:
                break
            builder.fit_image(info, m, builder.y, width, max_h)
            builder.y -= scale * ih + size * 0.5
            if rng.random() < 0.8:
                caption = rng.choice([
                    "Figura %d — %s" % (rng.randint(1, 12), random_sentence(rng)),
                    "Fonte: arquivo interno (%s)" % fake_protocol(rng),
                    "Imagem anexada ao documento",
                ])
                caption = caption[:120]
                for line in wrap_text(caption, "F3", size * 0.75, width):
                    if builder.y < bottom_color + line_h:
                        break
                    builder.text(m, builder.y, line, f_italic,
                                 size * 0.75, align="center", width=width, rgb=(0.4, 0.4, 0.45))
                    builder.y -= size * 1.05
            builder.y -= size * 0.4
            paras_done += 1
            continue
        # título de seção
        if kind < 0.48 and opts.get("headings", True):
            heading = random_heading(rng)
            hsize = size * 1.18
            builder.y -= size * 0.35
            builder.text(m, builder.y, heading, heading_key, hsize, rgb=(0.15, 0.15, 0.2))
            builder.y -= hsize * 1.55
            builder.line(m, builder.y + hsize * 0.55, m + min(width, text_width(heading_key, hsize, heading)),
                         builder.y + hsize * 0.55, (0.7, 0.7, 0.75), 0.5)
            continue
        # lista com marcadores
        if kind < 0.60 and opts.get("lists", True):
            for _ in range(rng.randint(3, 5)):
                item = random_bullet(rng)
                lines = wrap_text(item, body, size, width - size * 1.4)
                if builder.y - len(lines) * line_h < bottom_color:
                    break
                builder.text(m, builder.y, "•", body, size)
                for i, line in enumerate(lines):
                    builder.text(m + size * 1.3, builder.y, line, body, size, rgb=body_rgb)
                    builder.y -= line_h
            builder.y -= size * 0.4
            paras_done += 1
            continue
        # tabelinha
        if kind < 0.68 and opts.get("tables", True):
            rows = rng.randint(3, 6)
            cols = rng.randint(2, 4)
            row_h = size * 1.5
            if builder.y - (rows + 1) * row_h < bottom_color:
                break
            col_w = width / cols
            for r in range(rows + 1):
                y = builder.y - r * row_h
                builder.rect(m, y, width, row_h,
                             rgb=(0.9, 0.9, 0.93) if r == 0 else None,
                             stroke=(0.6, 0.6, 0.65))
                for c in range(cols):
                    if r == 0:
                        cell = rng.choice(["Item", "Descrição", "Valor", "Data", "Status", "Qtd."])
                    else:
                        cell = rng.choice(["Concluído", "Pendente", "R$ %d,%02d" % (rng.randint(50, 9000), rng.randint(0, 99)),
                                           "%d" % rng.randint(1, 500), "%02d/%02d/%04d" % (rng.randint(1, 28), rng.randint(1, 12), rng.randint(2023, 2026)),
                                           "Etapa %d" % rng.randint(1, 20), "Em análise"])
                    builder.text(m + c * col_w + 4, y + (row_h - size * 0.82) * 0.55, cell, body,
                                 size * 0.82, rgb=body_rgb)
            builder.y -= (rows + 1) * row_h + size * 0.6
            paras_done += 1
            continue
        # parágrafo
        text = rng.choice(pool) if rng.random() < 0.75 else random_paragraph(rng)
        lines = wrap_text(text, body, size, width)
        if rng.random() < 0.25:
            lines[0] = "    " + lines[0]
        if builder.y - min(len(lines), 2) * line_h < bottom_color:
            break
        for line in lines:
            if builder.y < bottom_color:
                break
            builder.text(m, builder.y, line, body, size, rgb=body_rgb)
            builder.y -= line_h
        builder.y -= size * 0.55
        paras_done += 1


def generate_document_pages(sink, opts, rng):
    pool = _content_pool(opts.get("texts"), rng)
    images = _image_list(opts.get("images"))
    for idx in range(int(opts.get("pages", 1))):
        size = resolve_page_size(opts.get("page_size"), rng)
        builder = PageBuilder(size[0], size[1], margin=max(18.0, min(size) * 0.08))
        draw_document_page(builder, rng, opts, idx == 0, pool, images)
        if opts.get("watermark"):
            _watermark(builder, rng, opts["watermark"])
        sink.add(size, builder.content(), builder.resources())
    return sink


def _watermark(builder, rng, text):
    w, h = builder.width, builder.height
    if text is True or text == "":
        text = rng.choice(_WATERMARKS)
    size = min(w, h) * 0.085
    while size > 6 and text_width("F2", size, text) > w * 0.9:
        size *= 0.9
    tw = text_width("F2", size, text)
    # gira 45° em torno do centro e centraliza o texto
    builder.ops += b"q\n"
    builder.ops += b"0.86 0.86 0.89 rg\n"
    builder.ops += ("0.7071 0.7071 -0.7071 0.7071 %.2f %.2f cm\n" % (w / 2.0, h / 2.0)).encode()
    builder.ops += b"BT\n/F2 %.2f Tf\n" % size
    builder.ops += ("1 0 0 1 %.2f %.2f Tm\n" % (-tw / 2.0, -size * 0.35)).encode()
    builder.ops += pdf_literal(text) + b" Tj\nET\nQ\n"


def random_size_range(rng, size_min, size_max):
    """Sorteia largura e altura dentro da faixa (em pontos)."""
    lo = 150.0
    span = max(float(size_max) - float(size_min), 1.0)
    w = rng.uniform(lo, lo + span)
    h = w * rng.uniform(1.32, 1.50)
    return round(w, 2), round(h, 2)


def build_text_pdfs(out_dir, opts, progress=None):
    """Gera N PDFs com textos/fotos do usuário (modo 2)."""
    files = []
    rng = random.Random()
    total = int(opts.get("pdfs", 1))
    for idx in range(1, total + 1):
        local = dict(opts)
        if opts.get("random_size"):
            local["page_size"] = random_size_range(rng, opts.get("size_min", 300), opts.get("size_max", 700))
        else:
            local["page_size"] = resolve_page_size(opts.get("page_size"), rng)
        n_pages = int(opts.get("pages", 1))
        if opts.get("random_pages"):
            n_pages = rng.randint(max(1, int(opts.get("pages_min", 1))),
                                  max(1, int(opts.get("pages_max", n_pages))))
        local["pages"] = n_pages
        stem = opts.get("filename") or "documento"
        path = unique_path(os.path.join(out_dir, "%s_%03d.pdf" % (stem, idx)))
        sink = PageSink(local.get("title") or "Documento")
        generate_document_pages(sink, local, rng)
        sink.save(path)
        files.append(path)
        if progress:
            progress("Gravado %s (%d página%s)" % (os.path.basename(path), n_pages,
                                                   "" if n_pages == 1 else "s"))
    return files


def build_auto_pdfs(out_dir, opts, progress=None):
    """Gera N PDFs totalmente aleatórios (modo 3)."""
    files = []
    rng = random.Random()
    total = int(opts.get("pdfs", 1))
    for idx in range(1, total + 1):
        local = dict(opts)
        if opts.get("random_size", True):
            size = random_size_range(rng, opts.get("size_min", 300), opts.get("size_max", 700))
        else:
            size = resolve_page_size(opts.get("page_size"), rng)
        local["page_size"] = size
        if opts.get("random_pages", True):
            n_pages = rng.randint(max(1, int(opts.get("pages_min", 1))),
                                  max(1, int(opts.get("pages_max", 8))))
        else:
            n_pages = int(opts.get("pages", 1))
        local["pages"] = n_pages
        local["texts"] = []
        local["images"] = []
        if opts.get("watermark"):
            local["watermark"] = True
        stem = opts.get("filename") or "aleatorio"
        path = unique_path(os.path.join(out_dir, "%s_%03d.pdf" % (stem, idx)))
        sink = PageSink(local.get("title") or rng.choice(_TITULOS))
        generate_document_pages(sink, local, rng)
        sink.save(path)
        files.append(path)
        if progress:
            progress("Gravado %s — %dx%dpt, %d página%s, %.1f KB" % (
                os.path.basename(path), size[0], size[1], n_pages,
                "" if n_pages == 1 else "s", os.path.getsize(path) / 1024.0))
    return files


def unique_path(path):
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    k = 2
    while os.path.exists("%s_%d%s" % (stem, k, ext)):
        k += 1
    return "%s_%d%s" % (stem, k, ext)


def open_file(path):
    """Abre o PDF no visualizador padrão do sistema (se possível)."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
            return True
        import subprocess
        if sys.platform == "darwin":
            subprocess.Popen(["open", path])
            return True
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return False
        for cmd in ("xdg-open", "gio"):
            try:
                subprocess.Popen([cmd, path] if cmd == "xdg-open" else [cmd, "open", path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return True
            except FileNotFoundError:
                continue
    except Exception:
        pass
    return False


# =============================================================================
# 9. MODO TEXTO: LERAMES E SAÍDA
# =============================================================================

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
import unicodedata as _unicodedata


def _visual_len(text):
    length = 0
    for ch in text:
        if _unicodedata.combining(ch):
            continue
        length += 2 if _unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return length


def _pad(text, width, align="left"):
    gap = max(0, width - _visual_len(text))
    if align == "right":
        return " " * gap + text
    if align == "center":
        left = gap // 2
        return " " * left + text + " " * (gap - left)
    return text + " " * gap


def _truncate(text, width):
    if _visual_len(text) <= width:
        return text
    out = ""
    for ch in text:
        if _visual_len(out + ch) > width - 1:
            break
        out += ch
    return out + "…"


def _strip_ansi(text):
    return _ANSI.sub("", text)


def _base_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:  # pragma: no cover
        return os.getcwd()


def _has_tk():
    try:
        import tkinter  # noqa: F401
        return True
    except Exception:
        return False


def in_termux():
    return "com.termux" in os.environ.get("PREFIX", "") or os.path.isdir("/data/data/com.termux")


def terminal_ok():
    try:
        return sys.stdin.isatty() or sys.stdout.isatty()
    except Exception:
        return False


def _term_width():
    try:
        return max(40, min(100, os.get_terminal_size().columns))
    except Exception:
        return 78


class Style:
    def __init__(self, enabled=True, color=False):
        self.enabled = enabled
        self.color = color

    def __call__(self, code, text):
        if not self.color:
            return text
        return "\x1b[%sm%s\x1b[0m" % (code, text)

    def bold(self, text):
        return self("1", text)

    def title(self, text):
        return self("1;36", text)

    def ok(self, text):
        return self("32", text)

    def warn(self, text):
        return self("33", text)

    def err(self, text):
        return self("31", text)

    def dim(self, text):
        return self("2", text)

    def accent(self, text):
        return self("35", text)


class Session:
    """Estado compartilhado entre o modo terminal e a interface gráfica."""

    def __init__(self, out_dir=None):
        self.out_dir = os.path.abspath(out_dir or os.path.join(_base_dir(), "saida"))
        self.sources = []
        self.texts = []
        self.images = []
        self.last_files = []

    # -- origem de PDFs -----------------------------------------------------
    def add_sources(self, paths, expand_dirs=True):
        added = []
        for path in paths:
            path = os.path.abspath(os.path.expanduser(os.path.expandvars(path.strip().strip("'\""))))
            if not path:
                continue
            if os.path.isdir(path) and expand_dirs:
                for name in sorted(os.listdir(path)):
                    if name.lower().endswith(".pdf"):
                        full = os.path.join(path, name)
                        if full not in self.sources:
                            self.sources.append(full)
                            added.append(full)
                continue
            if not os.path.isfile(path):
                raise PDFError("arquivo não encontrado: %s" % path)
            if os.path.isdir(path) or path not in self.sources:
                self.sources.append(path)
                added.append(path)
        return added

    def page_summary(self):
        rows = []
        for path in self.sources:
            try:
                doc = PDFDocument.open(path)
                n = len(doc.pages())
                size = doc.pages()[0].size() if n else (0, 0)
                rows.append((path, n, size, "%dx%d" % (round(size[0]), round(size[1]))))
            except Exception as exc:
                rows.append((path, 0, (0, 0), "ERRO: %s" % exc))
        return rows

    def add_images(self, paths):
        added = []
        for path in paths:
            path = os.path.abspath(os.path.expanduser(path.strip().strip("'\"")))
            if not os.path.isfile(path):
                raise PDFError("imagem não encontrada: %s" % path)
            load_image(path)  # valida já na inclusão
            if path not in self.images:
                self.images.append(path)
                added.append(path)
        return added

    def read_text_file(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        with open(path, "rb") as fh:
            raw = fh.read()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:  # pragma: no cover
            text = raw.decode("latin-1", "replace")
        return text

    def add_text_file(self, path):
        text = self.read_text_file(path)
        self.texts.append(text)
        return text

    # -- execução -----------------------------------------------------------
    def run_mode1(self, pages, copies, shuffle=True, repeat=True, out_name=None):
        if not self.sources:
            raise PDFError("nenhum PDF de origem. Use 'adicionar' primeiro.")
        docs = []
        for path in self.sources:
            try:
                docs.append(PDFDocument.open(path))
            except Exception as exc:
                raise PDFError("%s: %s" % (os.path.basename(path), exc))
        pool = []
        for doc in docs:
            for page in doc.pages():
                pool.append((doc, page))
        if not pool:
            raise PDFError("os PDFs de origem não têm páginas legíveis")
        if pages <= 0:
            raise PDFError("o número de páginas deve ser maior que zero")
        if pages <= len(pool):
            chosen = random.sample(pool, pages)
        elif repeat:
            chosen = [random.choice(pool) for _ in range(pages)]
        else:
            chosen = list(pool)
            random.shuffle(chosen)
            chosen = chosen[:pages]
            if len(chosen) < pages:
                raise PDFError("não há páginas suficientes (%d disponíveis)" % len(pool))
        if not shuffle:
            chosen.sort(key=lambda item: (docs.index(item[0]), item[1].num or 0))
        os.makedirs(self.out_dir, exist_ok=True)
        files = []
        for k in range(max(1, copies)):
            name = out_name or "misturado"
            path = unique_path(os.path.join(self.out_dir, "%s_%03d.pdf" % (name, k + 1)))
            build_output(path, chosen, title="Documento misturado")
            files.append(path)
        self.last_files = files
        return files

    def run_mode2(self, opts):
        out = self.out_dir
        os.makedirs(out, exist_ok=True)
        opts = dict(opts)
        opts.setdefault("texts", self.texts)
        opts.setdefault("images", self.images)
        files = build_text_pdfs(out, opts)
        self.last_files = files
        return files

    def run_mode3(self, opts):
        out = self.out_dir
        os.makedirs(out, exist_ok=True)
        files = build_auto_pdfs(out, opts)
        self.last_files = files
        return files


EXIT_WORDS = {"sair", "exit", "quit", "q", ":q", "voltar"}


def _ask(style, prompt, default=None, cast=str, allowed=None, valid=None, max_len=None,
         allow_empty=False):
    """Pergunta simples com valor padrão, validação e 'voltar'."""
    while True:
        suffix = " [%s]" % default if default is not None else ""
        vis = _visual_len(prompt + suffix)
        if max_len and vis > max_len:
            print(style.dim("  ? " + prompt))
            raw = input("    > " if default is None else "    %s > " % default)
        else:
            raw = input("%s%s: " % (prompt, style.dim(suffix)))
        raw = raw.strip()
        if raw.lower() in EXIT_WORDS:
            return None
        if not raw:
            if default is not None:
                if cast is int and not isinstance(default, int):
                    try:
                        return int(default)
                    except (TypeError, ValueError):
                        pass
                if cast is float and not isinstance(default, float):
                    try:
                        return float(default)
                    except (TypeError, ValueError):
                        pass
                if cast is bool and not isinstance(default, bool):
                    return str(default).lower() in ("s", "sim", "y", "1", "true")
                return default
            if allow_empty:
                return ""
            print(style.warn("  informe um valor."))
            continue
        if valid is not None and not valid(raw):
            print(style.warn("  valor inválido, tente de novo."))
            continue
        if cast is int:
            try:
                value = int(raw)
            except ValueError:
                print(style.warn("  digite um número inteiro."))
                continue
            if allowed is not None and value not in allowed:
                print(style.warn("  escolha uma das opções."))
                continue
            return value
        if cast is float:
            try:
                return float(raw.replace(",", "."))
            except ValueError:
                print(style.warn("  digite um número."))
                continue
        if cast is bool:
            return raw.lower() in ("s", "sim", "y", "yes", "1", "true", "v")
        if allowed is not None and raw.lower() not in allowed:
            print(style.warn("  escolha uma das opções."))
            continue
        return cast(raw)


def _ask_many(style, prompt, max_len):
    print(style.dim(prompt))
    print(style.dim("  (uma por linha; linha vazia termina; ctrl+c cancela)"))
    lines = []
    while True:
        try:
            line = input("    > ")
        except EOFError:
            break
        if not line.strip():
            break
        lines.append(line)
    return lines


def _parse_size_arg(text):
    text = (text or "aleatorio").strip()
    if text.lower() in ("", "aleatorio", "aleatoria", "random"):
        return "aleatorio"
    return text


def save_report(session, style, files=None):
    files = files or session.last_files
    if not files:
        print(style.warn("nenhum PDF gerado ainda."))
        return None
    lines = []
    lines.append("=" * 60)
    lines.append("PDFs gerados em %s" % datetime.datetime.now().strftime("%d/%m/%Y %H:%M"))
    lines.append("=" * 60)
    for path in files:
        try:
            size_kb = os.path.getsize(path) / 1024.0
        except OSError:
            size_kb = 0
        pages = ""
        try:
            pages = "%d pg" % len(PDFDocument.open(path).pages())
        except Exception:
            pages = "?"
        lines.append("%s\n    %s | %.1f KB" % (os.path.basename(path), pages, size_kb))
    lines.append("")
    text = "\n".join(lines)
    print(text)
    path = unique_path(os.path.join(session.out_dir, "relatorio.txt"))
    os.makedirs(session.out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    print(style.dim("relatório salvo em: %s" % path))
    return path


# =============================================================================
# 10. MODO TERMINAL (teclado, funciona em Termux, SSH etc.)
# =============================================================================

def _term_menu(style, session):
    n = style.bold
    title = style.title
    width = _term_width()
    print(title("=" * width))
    print(title("    GERADOR DE PDFs ALEATÓRIOS"))
    print(title("=" * width))
    rows = [
        ("1", "Embaralhar páginas de PDFs", "pega pdfs existentes e monta novos"),
        ("2", "Meus textos + minhas fotos", "gera PDFs com o seu conteúdo"),
        ("3", "PDF aleatório automático", "textos criados na hora"),
    ]
    for key, name, desc in rows:
        print("  %s) %s" % (n(key), name))
        print("     %s" % style.dim(desc))
    print()
    print("  %s) Adicionar arquivos        %s" % (n("4"), style.dim("PDFs, fotos ou texto (.txt)")))
    print("  %s) Listar o que está carregado" % n("5"))
    print("  %s) Pasta de saída e últimos PDFs" % n("6"))
    print("  %s) Ajuda / modo gráfico" % n("7"))
    print("  %s) Sair" % n("0"))
    print()
    print(style.dim("  Pasta de saída: %s" % session.out_dir))
    print(style.dim("  Dica: em qualquer pergunta, digite 'voltar' ou aperte Ctrl+C."))


def _term_list(session, style):
    print()
    if not session.sources:
        print(style.warn("  nenhum PDF de origem carregado."))
    else:
        print(style.bold("  PDFs de origem:"))
        rows = session.page_summary()
        for i, (path, n, size, extra) in enumerate(rows, 1):
            print("   %2d. %s  %s" % (i, _truncate(os.path.basename(path), 46),
                                      style.dim("%d pg | %s" % (n, extra))))
    if session.images:
        print(style.bold("  Fotos:"))
        for i, path in enumerate(session.images, 1):
            print("   %2d. %s" % (i, _truncate(os.path.basename(path), 56)))
    if session.texts:
        print(style.bold("  Textos carregados:"))
        for i, text in enumerate(session.texts, 1):
            preview = " ".join(text.split())[:60]
            print("   %2d. %s..." % (i, preview))
    print()


def _term_add(style, session, max_len):
    print()
    print("  O que você quer adicionar?")
    print("   1) PDFs (um ou vários; também aceita uma pasta)")
    print("   2) Fotos (JPG ou PNG)")
    print("   3) Arquivo de texto (.txt / .md)")
    op = _ask(style, "  opção", max_len=max_len, allowed={"1", "2", "3"})
    if op is None:
        return
    print(style.dim("  Cole o caminho e Enter (Enter vazio termina). Aspas são opcionais."))
    paths = []
    while True:
        raw = input("    > ").strip()
        if not raw:
            break
        if raw.lower() in EXIT_WORDS:
            return
        paths.append(raw)
    if not paths:
        return
    try:
        if op == "1":
            added = session.add_sources(paths)
            print(style.ok("  %d PDF(s) adicionado(s). Total: %d" % (len(added), len(session.sources))))
        elif op == "2":
            added = session.add_images(paths)
            print(style.ok("  %d foto(s) adicionada(s). Total: %d" % (len(added), len(session.images))))
        else:
            for p in paths:
                session.add_text_file(p)
            print(style.ok("  %d texto(s) carregado(s)." % len(paths)))
    except Exception as exc:
        print(style.err("  erro: %s" % exc))


def _term_saved(style, files, session):
    print()
    print(style.ok("  Pronto! %d PDF(s) gerado(s):" % len(files)))
    for path in files:
        print("   - %s" % _truncate(path, _term_width() - 5))
    print(style.dim("  Pasta: %s" % session.out_dir))


def _term_mode1(style, session, max_len):
    if not session.sources:
        print(style.warn("  adicione PDFs primeiro (opção 4)."))
        return
    rows = session.page_summary()
    total = sum(r[1] for r in rows if r[1])
    print()
    print(style.bold("  Sorteando páginas de %d PDF(s) — %d página(s) disponível(is)."
                     % (len(rows), total)))
    for i, (path, n, size, extra) in enumerate(rows, 1):
        print("   %2d) %s %s" % (i, _truncate(os.path.basename(path), 44),
                                 style.dim("%d pg" % n)))
    only = _ask(style, "  usar só alguns? (ex.: 1,3 ou Enter = todos)", default="",
                max_len=max_len, allow_empty=True)
    if only == "":
        pool = session.sources
    else:
        picked = []
        try:
            for part in re.split(r"[,\s]+", only):
                k = int(part)
                if 1 <= k <= len(session.sources):
                    picked.append(session.sources[k - 1])
        except ValueError:
            picked = []
        if not picked:
            print(style.warn("  seleção inválida; usando todos."))
            picked = session.sources
        pool = picked
    pages = _ask(style, "  páginas no novo PDF", default=str(min(5, total or 1)),
                 cast=int, max_len=max_len)
    if pages is None or pages < 1:
        return
    copies = _ask(style, "  quantos PDFs iguais gerar", default="1", cast=int, max_len=max_len)
    if copies is None:
        return
    shuffle = _ask(style, "  aleatorizar a ordem das páginas? (s/n)", default="s", cast=bool,
                   max_len=max_len)
    rep = _ask(style, "  se faltarem páginas, repetir? (s/n)", default="s", cast=bool,
               max_len=max_len)
    try:
        tmp = Session(session.out_dir)
        tmp.sources = pool
        files = tmp.run_mode1(pages, copies, bool(shuffle), bool(rep))
        session.last_files = files
        _term_saved(style, files, session)
    except Exception as exc:
        print(style.err("  erro: %s" % exc))


def _term_common(style, opts, max_len, is_auto):
    opts["pdfs"] = _ask(style, "  quantos PDFs gerar", default="1", cast=int, max_len=max_len) or 1
    if opts["pdfs"] < 1:
        opts["pdfs"] = 1
    print("  Tamanho das páginas:")
    print("   a) A4        b) A5        c) Carta (Letter)   d) A3")
    print("   e) aleatório  f) personalizado (ex.: 10x15cm, 210x297mm, 400x600pt)")
    size = _ask(style, "  escolha (a-f)", default="e" if is_auto else "a", max_len=max_len)
    size = (size or "e").lower()
    presets = {"a": "A4", "b": "A5", "c": "Letter", "d": "A3"}
    if size in presets:
        opts["page_size"] = presets[size]
        opts["random_size"] = False
        mode = ""
        if not is_auto:
            mode = _ask(style, "  orientação (r = retrato, p = paisagem)", default="r",
                        max_len=max_len)
            if mode and mode.lower().startswith("p"):
                w, h = resolve_page_size(opts["page_size"], random.Random(0))
                opts["page_size"] = (h, w)
    elif size == "f":
        while True:
            spec = _ask(style, "  tamanho", default="10x15cm", max_len=max_len)
            if spec is None:
                return None
            try:
                opts["page_size"] = resolve_page_size(spec, random.Random(0))
                break
            except PDFError as exc:
                print(style.warn("  %s" % exc))
        opts["random_size"] = False
    else:
        opts["random_size"] = True
        print(style.dim("  Sortear o tamanho de cada PDF dentro de uma faixa:"))
        while True:
            lo = _ask(style, "    largura mínima (mm)", default="80", cast=int, max_len=max_len)
            hi = _ask(style, "    largura máxima (mm)", default="200", cast=int, max_len=max_len)
            if lo is None or hi is None:
                return None
            if lo >= 20 and hi > lo:
                break
            print(style.warn("    a largura máxima deve ser maior que a mínima (20 a 600 mm)."))
        opts["size_min"] = lo * 72 / 25.4
        opts["size_max"] = hi * 72 / 25.4
    n_pages = _ask(style, "  páginas por PDF (se for aleatório, será um número próximo)",
                   default="1", cast=int, max_len=max_len) or 1
    opts["pages"] = max(1, n_pages)
    rand_pages = _ask(style, "  variar a quantidade de páginas? (s/n)",
                      default="s" if is_auto else "n", cast=bool, max_len=max_len)
    if rand_pages:
        lo = _ask(style, "    mínimo de páginas", default="1", cast=int, max_len=max_len) or 1
        hi = _ask(style, "    máximo de páginas", default=str(max(n_pages, 5)), cast=int,
                  max_len=max_len) or n_pages
        if hi < lo:
            lo, hi = hi, lo
        opts["random_pages"] = True
        opts["pages_min"] = max(1, lo)
        opts["pages_max"] = max(1, hi)
    else:
        opts["random_pages"] = False
    opts["serif"] = _ask(style, "  fonte com serifa (Times)? (s/n)", default="n", cast=bool,
                         max_len=max_len)
    wm = _ask(style, "  marca d'água (ex.: CONFIDENCIAL; Enter = nenhuma)", default="",
              max_len=max_len, allow_empty=True)
    opts["watermark"] = wm or None
    return opts


def _term_mode2(style, session, max_len):
    print()
    if session.texts:
        print(style.ok("  %d texto(s) carregado(s)." % len(session.texts)))
    else:
        print(style.warn("  nenhum texto carregado: os PDFs serão só com fotos/textos aleatórios."))
    if session.images:
        print(style.ok("  %d foto(s) carregada(s)." % len(session.images)))
    if not session.texts or not session.images:
        add = _ask(style, "  escrever um texto agora? (s/n)", default="s" if not session.texts else "n",
                   cast=bool, max_len=max_len)
        if add:
            print(style.dim("  Digite o texto (linha vazia termina). Enter vazio já basta para"
                            " usar textos aleatórios."))
            lines = []
            while True:
                line = input("    > ")
                if not line.strip():
                    break
                lines.append(line)
            if lines:
                session.texts.append("\n".join(lines))
    opts = {"texts": session.texts, "images": session.images}
    opts = _term_common(style, opts, max_len, is_auto=False)
    if opts is None:
        return
    try:
        files = session.run_mode2(opts)
        _term_saved(style, files, session)
    except Exception as exc:
        print(style.err("  erro: %s" % exc))


def _term_mode3(style, session, max_len):
    print()
    print(style.dim("  Os textos são criados automaticamente no idioma português."))
    opts = {}
    opts = _term_common(style, opts, max_len, is_auto=True)
    if opts is None:
        return
    try:
        files = session.run_mode3(opts)
        _term_saved(style, files, session)
    except Exception as exc:
        print(style.err("  erro: %s" % exc))


def _term_last(style, session, max_len):
    if not session.last_files:
        print(style.warn("  nenhum PDF gerado nesta sessão."))
        return
    files = [f for f in session.last_files if os.path.exists(f)]
    print()
    print(style.bold("  Últimos PDFs desta sessão:"))
    for i, f in enumerate(files, 1):
        print("   %2d) %s" % (i, _truncate(os.path.basename(f), _term_width() - 8)))
    print("    a) abrir pasta de saída   p) abrir um PDF   r) relatório   Enter) voltar")
    op = _ask(style, "  opção", default="", max_len=max_len, allow_empty=True)
    if op == "":
        return
    if op.lower() == "a":
        if not open_file(session.out_dir):
            print(style.dim("  abra manualmente: %s" % session.out_dir))
    elif op.lower() == "r":
        save_report(session, style)
    elif op.isdigit() and 1 <= int(op) <= len(files):
        target = files[int(op) - 1]
        if not open_file(target):
            print(style.dim("  abra manualmente: %s" % target))


def _term_help(style, session, max_len):
    print(style.bold("  Modo gráfico:"))
    has_tk = _has_tk()
    if has_tk:
        print("   - No computador: python gerador_pdf.py --grafico")
        print("     (ou apenas python gerador_pdf.py numa tela normal)")
    else:
        print(style.warn("   - Tkinter não está instalado neste ambiente "
                         "(no Termux: pkg install python-tk)."))
    print(style.bold("  Exemplos no terminal:"))
    print("   python gerador_pdf.py --cli misturar a.pdf b.pdf -p 10 -n 3")
    print("   python gerador_pdf.py --cli texto --texto texto.txt --foto 1.jpg -p 4")
    print("   python gerador_pdf.py --cli aleatorio --pdfs 5 -p 3 --tamanho aleatorio")
    print(style.bold("  Ajuda completa:") )
    print("   python gerador_pdf.py --cli ajuda")
    print()
    print(style.bold("  Pasta de saída atual: "))
    print("   %s" % session.out_dir)
    change = _ask(style, "  mudar a pasta de saída? (s/n)", default="n", cast=bool, max_len=max_len)
    if change:
        new = _ask(style, "  nova pasta", default=session.out_dir, max_len=max_len)
        if new:
            session.out_dir = os.path.abspath(os.path.expanduser(new))
            print(style.ok("  pasta de saída: %s" % session.out_dir))


def terminal_mode(session=None, force=False):
    session = session or Session()
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    style = Style(color=color)
    max_len = _term_width() - 6
    print()
    print(style.dim("  Modo terminal. Os menus funcionam com números e Enter."))
    while True:
        _term_menu(style, session)
        try:
            choice = input(style.bold("  opção > ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            choice = "0"
        try:
            if choice in ("0",):
                print(style.dim("  até logo!"))
                return 0
            elif choice == "1":
                _term_mode1(style, session, max_len)
            elif choice == "2":
                _term_mode2(style, session, max_len)
            elif choice == "3":
                _term_mode3(style, session, max_len)
            elif choice == "4":
                _term_add(style, session, max_len)
            elif choice == "5":
                _term_list(session, style)
            elif choice == "6":
                _term_last(style, session, max_len)
            elif choice == "7":
                _term_help(style, session, max_len)
            elif choice in ("", "h", "?"):
                continue
            else:
                print(style.warn("  opção inválida."))
        except (KeyboardInterrupt, EOFError):
            print()
            print(style.warn("  operação cancelada."))
        except Exception as exc:  # nunca derruba o menu
            print(style.err("  erro inesperado: %s" % exc))


# =============================================================================
# 11. INTERFACE GRÁFICA (Tkinter, opcional)
# =============================================================================


def graphical_mode(session=None):
    """Abre a janela do programa. Usa apenas tkinter (biblioteca padrão)."""
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox, scrolledtext
        from tkinter import font as tkfont
    except Exception as exc:
        raise PDFError(
            "Tkinter não está disponível neste Python (%s).\n"
            "No Termux/celular use: python gerador_pdf.py --terminal\n"
            "No Debian/Ubuntu: sudo apt install python3-tk" % exc
        )
    session = session or Session()

    root = tk.Tk()
    root.title("Gerador de PDFs Aleatórios")
    root.geometry("940x720")
    root.minsize(820, 620)
    try:
        root.tk.call("tk", "scaling", max(1.0, root.winfo_fpixels("1i") / 96.0))
    except Exception:
        pass

    style = ttk.Style(root)
    for theme in ("clam", "vista", "aqua"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    base_font = tkfont.nametofont("TkDefaultFont")
    try:
        base_size = base_font.actual("size")
        if base_size and base_size < 10:
            base_font.configure(size=10)
    except Exception:
        pass

    status_var = tk.StringVar(value="Pronto. Adicione arquivos e escolha um modo.")
    busy = {"on": False}

    # ---------------- layout principal ----------------
    header = ttk.Frame(root, padding=(12, 10, 12, 4))
    header.pack(fill="x")
    ttk.Label(header, text="Gerador de PDFs Aleatórios",
              font=(base_font.actual("family"), 16, "bold")).pack(side="left")
    ttk.Label(header, text="100% Python • sem pacotes externos",
              foreground="#666").pack(side="right")

    out_frame = ttk.Frame(root, padding=(12, 0, 12, 4))
    out_frame.pack(fill="x")
    ttk.Label(out_frame, text="Pasta de saída:").pack(side="left")
    out_var = tk.StringVar(value=session.out_dir)
    out_entry = ttk.Entry(out_frame, textvariable=out_var)
    out_entry.pack(side="left", fill="x", expand=True, padx=6)

    def choose_out():
        path = filedialog.askdirectory(initialdir=out_var.get() or os.getcwd())
        if path:
            out_var.set(path)
            session.out_dir = path

    ttk.Button(out_frame, text="Escolher…", command=choose_out).pack(side="left")
    ttk.Button(out_frame, text="Abrir pasta",
               command=lambda: open_file(out_var.get()) or status_var.set(
                   "Não foi possível abrir a pasta automaticamente.")).pack(side="left", padx=(6, 0))

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=12, pady=6)

    # ---------------- aba 1 ----------------
    tab1 = ttk.Frame(nb, padding=10)
    nb.add(tab1, text=" 1 · Embaralhar PDFs ")

    src_frame = ttk.LabelFrame(tab1, text="PDFs de origem", padding=8)
    src_frame.pack(fill="both", expand=True)
    src_list = tk.Listbox(src_frame, height=7, selectmode="extended")
    src_list.pack(side="left", fill="both", expand=True)
    src_scroll = ttk.Scrollbar(src_frame, orient="vertical", command=src_list.yview)
    src_scroll.pack(side="left", fill="y")
    src_list.configure(yscrollcommand=src_scroll.set)

    src_btns = ttk.Frame(src_frame)
    src_btns.pack(side="left", fill="y", padx=(8, 0))
    src_info = tk.StringVar(value="Nenhum PDF adicionado")

    def refresh_sources():
        src_list.delete(0, "end")
        total = 0
        for path in session.sources:
            try:
                n = len(PDFDocument.open(path).pages())
                total += n
                src_list.insert("end", "%s  —  %d página%s" % (
                    os.path.basename(path), n, "" if n == 1 else "s"))
            except Exception as exc:
                src_list.insert("end", "%s  —  ERRO: %s" % (os.path.basename(path), exc))
            src_list.itemconfig("end", foreground="#000" if src_list.size() % 2 else "#333")
        src_info.set("%d arquivo(s), %d página(s) no total" % (len(session.sources), total))

    def add_pdf_files():
        paths = filedialog.askopenfilenames(
            title="Escolha os PDFs", filetypes=[("PDF", "*.pdf"), ("Todos", "*")])
        if paths:
            session.add_sources(list(paths))
            refresh_sources()

    def add_pdf_folder():
        path = filedialog.askdirectory(title="Pasta com PDFs")
        if path:
            session.add_sources([path])
            refresh_sources()

    def remove_selected():
        for idx in sorted(src_list.curselection(), reverse=True):
            if 0 <= idx < len(session.sources):
                session.sources.pop(idx)
        refresh_sources()

    def clear_sources():
        if session.sources and messagebox.askyesno("Limpar", "Remover todos os PDFs da lista?"):
            session.sources.clear()
            refresh_sources()

    ttk.Button(src_btns, text="Adicionar PDFs…", command=add_pdf_files).pack(fill="x")
    ttk.Button(src_btns, text="Adicionar pasta…", command=add_pdf_folder).pack(fill="x", pady=4)
    ttk.Button(src_btns, text="Remover selecionados", command=remove_selected).pack(fill="x")
    ttk.Button(src_btns, text="Limpar lista", command=clear_sources).pack(fill="x", pady=4)
    ttk.Label(src_btns, textvariable=src_info, wraplength=200).pack(fill="x", pady=(6, 0))

    m1 = ttk.LabelFrame(tab1, text="Como gerar", padding=8)
    m1.pack(fill="x", pady=(10, 0))
    v_pages = tk.StringVar(value="5")
    v_copies = tk.StringVar(value="1")
    v_shuffle = tk.BooleanVar(value=True)
    v_repeat = tk.BooleanVar(value=True)
    v_name = tk.StringVar(value="misturado")

    row = ttk.Frame(m1)
    row.pack(fill="x")
    ttk.Label(row, text="Páginas no novo PDF:").grid(row=0, column=0, sticky="w")
    ttk.Spinbox(row, from_=1, to=5000, width=7, textvariable=v_pages).grid(row=0, column=1, padx=(6, 18))
    ttk.Label(row, text="Quantos PDFs:").grid(row=0, column=2, sticky="w")
    ttk.Spinbox(row, from_=1, to=200, width=7, textvariable=v_copies).grid(row=0, column=3, padx=(6, 18))
    ttk.Label(row, text="Nome dos arquivos:").grid(row=0, column=4, sticky="w")
    ttk.Entry(row, width=22, textvariable=v_name).grid(row=0, column=5, padx=6, sticky="we")
    row.columnconfigure(5, weight=1)

    row2 = ttk.Frame(m1)
    row2.pack(fill="x", pady=(8, 0))
    ttk.Checkbutton(row2, text="Aleatorizar a ordem das páginas",
                    variable=v_shuffle).pack(side="left")
    ttk.Checkbutton(row2, text="Se faltar página, repetir páginas",
                    variable=v_repeat).pack(side="left", padx=18)

    # ---------------- aba 2 ----------------
    tab2 = ttk.Frame(nb, padding=10)
    nb.add(tab2, text=" 2 · Meus textos e fotos ")

    left = ttk.Frame(tab2)
    left.pack(side="left", fill="both", expand=True)
    right = ttk.Frame(tab2)
    right.pack(side="left", fill="both", padx=(10, 0))

    ttk.Label(left, text="Textos (um texto por parágrafo; linha vazia separa parágrafos):").pack(anchor="w")
    txt = scrolledtext.ScrolledText(left, wrap="word", height=12)
    txt.pack(fill="both", expand=True, pady=(4, 6))
    txt.insert("1.0", "Escreva aqui o texto que deve aparecer nos PDFs.\n\n"
                      "Cada bloco separado por uma linha vazia é usado como um parágrafo.\n"
                      "Se o campo ficar vazio, serão usados textos aleatórios em português.")

    txt_btns = ttk.Frame(left)
    txt_btns.pack(fill="x")
    extra_texts = []

    def load_text_file():
        path = filedialog.askopenfilename(
            title="Arquivo de texto", filetypes=[("Texto", "*.txt *.md *.csv"), ("Todos", "*")])
        if path:
            try:
                extra_texts.append(session.read_text_file(path))
                status_var.set("Texto carregado: %s" % os.path.basename(path))
            except Exception as exc:
                messagebox.showerror("Erro", str(exc))

    ttk.Button(txt_btns, text="Carregar .txt…", command=load_text_file).pack(side="left")
    ttk.Button(txt_btns, text="Limpar texto",
               command=lambda: txt.delete("1.0", "end")).pack(side="left", padx=6)

    ttk.Label(right, text="Fotos (JPG e PNG):").pack(anchor="w")
    img_list = tk.Listbox(right, height=12, width=34, selectmode="extended")
    img_list.pack(fill="both", expand=True, pady=(4, 6))

    def refresh_images():
        img_list.delete(0, "end")
        for path in session.images:
            img_list.insert("end", os.path.basename(path))

    def add_images():
        paths = filedialog.askopenfilenames(
            title="Escolha as fotos",
            filetypes=[("Imagens", "*.jpg *.jpeg *.png *.JPG *.JPEG *.PNG"), ("Todos", "*")])
        if not paths:
            return
        try:
            added = session.add_images(list(paths))
            refresh_images()
            status_var.set("%d foto(s) adicionada(s)." % len(added))
        except Exception as exc:
            messagebox.showerror("Erro na imagem", str(exc))

    def remove_images():
        for idx in sorted(img_list.curselection(), reverse=True):
            if idx < len(session.images):
                session.images.pop(idx)
        refresh_images()

    img_btns = ttk.Frame(right)
    img_btns.pack(fill="x")
    ttk.Button(img_btns, text="Adicionar fotos…", command=add_images).pack(side="left")
    ttk.Button(img_btns, text="Remover", command=remove_images).pack(side="left", padx=6)
    ttk.Label(right, foreground="#666", wraplength=250,
              text="As fotos entram em tamanho aleatório, com legendas, junto ao texto."
              ).pack(anchor="w", pady=(8, 0))

    opts2 = ttk.LabelFrame(tab2, text="Formato", padding=8)
    opts2.pack(fill="x", pady=(10, 0))
    o_pdfs = tk.StringVar(value="1")
    o_pages = tk.StringVar(value="3")
    o_rand_pages = tk.BooleanVar(value=False)
    o_pages_max = tk.StringVar(value="6")
    o_size = tk.StringVar(value="A4")
    o_rand_size = tk.BooleanVar(value=False)
    o_size_min = tk.StringVar(value="8")
    o_size_max = tk.StringVar(value="21")
    o_serif = tk.BooleanVar(value=False)
    o_watermark = tk.StringVar(value="")
    o_name2 = tk.StringVar(value="documento")

    SIZE_CHOICES = list(PAGE_SIZES.keys()) + ["personalizado"]

    def _opts_row(parent, r):
        ttk.Label(parent, text="Quantos PDFs:").grid(row=r, column=0, sticky="w")
        ttk.Spinbox(parent, from_=1, to=200, width=6, textvariable=o_pdfs).grid(row=r, column=1, padx=(6, 16))
        ttk.Label(parent, text="Páginas:").grid(row=r, column=2, sticky="w")
        ttk.Spinbox(parent, from_=1, to=500, width=6, textvariable=o_pages).grid(row=r, column=3, padx=(6, 16))
        ttk.Label(parent, text="Nome:").grid(row=r, column=4, sticky="w")
        ttk.Entry(parent, width=18, textvariable=o_name2).grid(row=r, column=5, sticky="we")
        parent.columnconfigure(5, weight=1)

    _opts_row(opts2, 0)
    r1 = ttk.Frame(opts2)
    r1.grid(row=1, column=0, columnspan=6, sticky="we", pady=(6, 0))
    ttk.Checkbutton(r1, text="Variar nº de páginas até", variable=o_rand_pages).pack(side="left")
    ttk.Spinbox(r1, from_=1, to=500, width=6, textvariable=o_pages_max).pack(side="left", padx=(4, 16))
    ttk.Label(r1, text="Tamanho:").pack(side="left")
    size_combo = ttk.Combobox(r1, values=SIZE_CHOICES, textvariable=o_size, width=22, state="readonly")
    size_combo.pack(side="left", padx=6)
    ttk.Checkbutton(r1, text="Sortear tamanho de", variable=o_rand_size).pack(side="left", padx=(10, 0))
    ttk.Spinbox(r1, from_=3, to=60, width=5, textvariable=o_size_min).pack(side="left")
    ttk.Label(r1, text="a").pack(side="left", padx=2)
    ttk.Spinbox(r1, from_=3, to=60, width=5, textvariable=o_size_max).pack(side="left")
    ttk.Label(r1, text="cm").pack(side="left", padx=(2, 0))

    r2 = ttk.Frame(opts2)
    r2.grid(row=2, column=0, columnspan=6, sticky="we", pady=(6, 0))
    ttk.Checkbutton(r2, text="Fonte serifada (Times)", variable=o_serif).pack(side="left")
    ttk.Label(r2, text="Marca d'água:").pack(side="left", padx=(16, 4))
    ttk.Entry(r2, width=20, textvariable=o_watermark).pack(side="left")
    ttk.Label(r2, text="(deixe vazio para nenhuma)", foreground="#666").pack(side="left", padx=6)

    def open_manual_size():
        dialog = tk.Toplevel(root)
        dialog.title("Tamanho personalizado")
        dialog.transient(root)
        dialog.grab_set()
        w_var, h_var, u_var = tk.StringVar(value="10"), tk.StringVar(value="15"), tk.StringVar(value="cm")
        ttk.Label(dialog, text="Largura").grid(row=0, column=0, padx=8, pady=8)
        ttk.Entry(dialog, textvariable=w_var, width=8).grid(row=0, column=1)
        ttk.Label(dialog, text="x").grid(row=0, column=2)
        ttk.Entry(dialog, textvariable=h_var, width=8).grid(row=0, column=3)
        ttk.Combobox(dialog, values=["mm", "cm", "pt"], textvariable=u_var, width=5,
                     state="readonly").grid(row=0, column=4, padx=8)
        ttk.Label(dialog, text="Dica: o tamanho também pode ser digitado direto no campo.",
                  foreground="#666").grid(row=1, column=0, columnspan=5, padx=8)

        def apply():
            o_size.set("%sx%s%s" % (w_var.get(), h_var.get(), u_var.get()))
            dialog.destroy()

        ttk.Button(dialog, text="Usar", command=apply).grid(row=2, column=3, columnspan=2, pady=8)

    ttk.Button(r2, text="Tamanho…", command=open_manual_size).pack(side="left", padx=(10, 0))

    # ---------------- aba 3 ----------------
    tab3 = ttk.Frame(nb, padding=10)
    nb.add(tab3, text=" 3 · Aleatório automático ")
    ttk.Label(tab3, text="Cria PDFs com textos aleatórios em português.\n"
                         "Escolha apenas quantos, o tamanho e o número de páginas.",
              foreground="#444").pack(anchor="w")
    opts3 = ttk.LabelFrame(tab3, text="Opções", padding=8)
    opts3.pack(fill="x", pady=10)
    a_pdfs = tk.StringVar(value="3")
    a_pages = tk.StringVar(value="2")
    a_rand_pages = tk.BooleanVar(value=True)
    a_pages_min = tk.StringVar(value="1")
    a_pages_max = tk.StringVar(value="5")
    a_rand_size = tk.BooleanVar(value=True)
    a_size = tk.StringVar(value="A4")
    a_size_min = tk.StringVar(value="8")
    a_size_max = tk.StringVar(value="21")
    a_watermark = tk.BooleanVar(value=False)
    a_name = tk.StringVar(value="aleatorio")

    r = ttk.Frame(opts3)
    r.pack(fill="x")
    ttk.Label(r, text="Quantos PDFs:").grid(row=0, column=0, sticky="w")
    ttk.Spinbox(r, from_=1, to=500, width=6, textvariable=a_pdfs).grid(row=0, column=1, padx=6)
    ttk.Label(r, text="Páginas:").grid(row=0, column=2, sticky="w", padx=(14, 0))
    ttk.Spinbox(r, from_=1, to=500, width=6, textvariable=a_pages).grid(row=0, column=3, padx=6)
    ttk.Checkbutton(r, text="entre", variable=a_rand_pages).grid(row=0, column=4)
    ttk.Spinbox(r, from_=1, to=500, width=5, textvariable=a_pages_min).grid(row=0, column=5)
    ttk.Label(r, text="e").grid(row=0, column=6, padx=2)
    ttk.Spinbox(r, from_=1, to=500, width=5, textvariable=a_pages_max).grid(row=0, column=7)
    ttk.Label(r, text="Nome:").grid(row=0, column=8, padx=(14, 4))
    ttk.Entry(r, width=16, textvariable=a_name).grid(row=0, column=9)

    r = ttk.Frame(opts3)
    r.pack(fill="x", pady=(10, 0))
    ttk.Label(r, text="Tamanho:").pack(side="left")
    a_size_combo = ttk.Combobox(r, values=SIZE_CHOICES, textvariable=a_size, width=22, state="readonly")
    a_size_combo.pack(side="left", padx=6)
    ttk.Checkbutton(r, text="Sortear cada PDF com largura de", variable=a_rand_size).pack(side="left", padx=(10, 0))
    ttk.Spinbox(r, from_=3, to=60, width=5, textvariable=a_size_min).pack(side="left")
    ttk.Label(r, text="a").pack(side="left", padx=2)
    ttk.Spinbox(r, from_=3, to=60, width=5, textvariable=a_size_max).pack(side="left")
    ttk.Label(r, text="cm").pack(side="left", padx=(2, 8))
    ttk.Checkbutton(r, text="Marca d'água", variable=a_watermark).pack(side="left")

    hint = ttk.Label(tab3, foreground="#666",
                     text="O tamanho do arquivo acompanha o tamanho e o número de páginas.")
    hint.pack(anchor="w")

    # ---------------- log ----------------
    log_frame = ttk.LabelFrame(root, text="Resultado", padding=6)
    log_frame.pack(fill="both", expand=False, padx=12, pady=(0, 4))
    log = scrolledtext.ScrolledText(log_frame, height=7, wrap="word", state="disabled")
    log.pack(fill="both", expand=True)
    log.tag_configure("ok", foreground="#0a7a2f")
    log.tag_configure("err", foreground="#b00020")
    log.tag_configure("info", foreground="#444")
    log.tag_configure("link", foreground="#0b5cad", underline=True)

    def log_write(text, tag="info"):
        log.configure(state="normal")
        log.insert("end", text + "\n", tag)
        log.see("end")
        log.configure(state="disabled")

    last_files = []

    def show_files(files, mode_label):
        nonlocal last_files
        last_files = files
        file_row.pack(fill="x", padx=12, pady=(0, 6), before=log_frame.master)
        for child in file_row.winfo_children():
            child.destroy()
        ttk.Label(file_row, text="%s — abrir:" % mode_label, foreground="#444").pack(side="left")
        for path in files[-6:]:
            btn = ttk.Button(file_row, text=os.path.basename(path),
                             command=lambda p=path: open_file(p) or log_write(
                                 "Abra manualmente: %s" % p, "info"))
            btn.pack(side="left", padx=2)

    file_row = ttk.Frame(root)

    def set_busy(value):
        busy["on"] = value
        root.configure(cursor="watch" if value else "")
        root.update_idletasks()

    def safe_run(label, func):
        if busy["on"]:
            return
        set_busy(True)
        status_var.set(label + "…")
        log_write("▶ " + label, "info")
        root.update()

        def work():
            try:
                files = func()
                root.after(0, lambda: done(files))
            except Exception as exc:
                root.after(0, lambda: failed(exc))

        def done(files):
            set_busy(False)
            status_var.set("Concluído: %d PDF(s)." % len(files))
            for path in files:
                try:
                    kb = os.path.getsize(path) / 1024.0
                except OSError:
                    kb = 0
                log_write("   %s  (%.1f KB)  →  %s" % (os.path.basename(path), kb,
                                                       os.path.dirname(path)), "ok")
            show_files(files, "Pronto")
            out_var.set(session.out_dir)

        def failed(exc):
            set_busy(False)
            status_var.set("Erro.")
            log_write("✖ %s" % exc, "err")
            messagebox.showerror("Erro", str(exc))

        import threading
        threading.Thread(target=work, daemon=True).start()

    def _int(var, default, minimum=1):
        try:
            return max(minimum, int(str(var.get()).strip()))
        except Exception:
            return default

    def _watermark_text():
        wm = o_watermark.get().strip()
        return wm if wm else None

    def _common2():
        session.out_dir = os.path.abspath(os.path.expanduser(out_var.get().strip() or session.out_dir))
        texts = [t for t in [txt.get("1.0", "end").strip()] + extra_texts if t.strip()]
        if o_rand_size.get():
            smin = _int(o_size_min, 8) * 72 / 25.4
            smax = max(_int(o_size_max, 21) * 72 / 25.4, smin + 10)
            page_size = "aleatorio"
        else:
            smin = smax = 0
            page_size = o_size.get() or "A4"
        return dict(
            pdfs=_int(o_pdfs, 1), pages=_int(o_pages, 1),
            random_pages=bool(o_rand_pages.get()), pages_min=1, pages_max=_int(o_pages_max, 5),
            random_size=bool(o_rand_size.get()), page_size=page_size,
            size_min=smin, size_max=smax,
            texts=texts, images=list(session.images),
            serif=bool(o_serif.get()), watermark=_watermark_text(),
            filename=o_name2.get().strip() or "documento",
        )

    def run_mode1():
        if not session.sources:
            messagebox.showwarning("Sem PDFs", "Adicione pelo menos um PDF de origem.")
            return
        session.out_dir = os.path.abspath(os.path.expanduser(out_var.get().strip() or session.out_dir))
        pages = _int(v_pages, 5)
        copies = _int(v_copies, 1)
        shuffle = bool(v_shuffle.get())
        repeat = bool(v_repeat.get())
        name = v_name.get().strip() or "misturado"

        def job():
            return session.run_mode1(pages, copies, shuffle, repeat, out_name=name)

        safe_run("Sorteando páginas de %d PDF(s)" % len(session.sources), job)

    def run_mode2():
        try:
            opts = _common2()
        except Exception as exc:
            messagebox.showerror("Erro", str(exc))
            return

        def job():
            return session.run_mode2(opts)

        safe_run("Gerando PDFs com seus textos e fotos", job)

    def run_mode3():
        def job():
            session.out_dir = os.path.abspath(os.path.expanduser(out_var.get().strip() or session.out_dir))
            if a_rand_size.get():
                smin = _int(a_size_min, 8) * 72 / 25.4
                smax = max(_int(a_size_max, 21) * 72 / 25.4, smin + 10)
                page_size = "aleatorio"
            else:
                smin = smax = 0
                page_size = a_size.get() or "A4"
            opts = dict(
                pdfs=_int(a_pdfs, 3), pages=_int(a_pages, 2),
                random_pages=bool(a_rand_pages.get()),
                pages_min=_int(a_pages_min, 1), pages_max=_int(a_pages_max, 5),
                random_size=bool(a_rand_size.get()), page_size=page_size,
                size_min=smin, size_max=smax,
                watermark=True if a_watermark.get() else None,
                filename=a_name.get().strip() or "aleatorio",
            )
            return session.run_mode3(opts)

        safe_run("Gerando PDFs aleatórios", job)

    # botões das abas
    b1 = ttk.Frame(tab1)
    b1.pack(fill="x", pady=(10, 0))
    ttk.Button(b1, text="Gerar PDFs embaralhados", command=run_mode1).pack(side="left")
    ttk.Button(b1, text="Mostrar pasta de saída",
               command=lambda: open_file(out_var.get())).pack(side="left", padx=8)

    b2 = ttk.Frame(tab2)
    b2.pack(side="right", fill="y", pady=(10, 0))
    ttk.Button(tab2, text="Gerar PDFs com meu conteúdo", command=run_mode2)

    def place_button2():
        ttk.Button(right, text="Gerar PDFs com meu conteúdo", command=run_mode2).pack(
            fill="x", pady=(10, 0))

    b2.destroy()
    place_button2()

    ttk.Button(tab3, text="Gerar PDFs aleatórios", command=run_mode3).pack(anchor="w", pady=(10, 0))

    # ---------------- rodapé ----------------
    foot = ttk.Frame(root, padding=(12, 0, 12, 8))
    foot.pack(fill="x")
    ttk.Label(foot, textvariable=status_var, foreground="#333").pack(side="left")
    ttk.Button(foot, text="Abrir último PDF",
               command=lambda: (last_files and open_file(last_files[-1])) or status_var.set(
                   "Gere um PDF primeiro.")).pack(side="right")
    ttk.Button(foot, text="Sair", command=root.destroy).pack(side="right", padx=6)

    refresh_sources()
    refresh_images()

    root.mainloop()
    return 0


# =============================================================================
# 12. LINHA DE COMANDO
# =============================================================================

CLI_HELP = """\
Gerador de PDFs Aleatórios — linha de comando

Uso:
  python gerador_pdf.py                       abre a interface gráfica (se possível)
                                              ou o modo terminal
  python gerador_pdf.py --terminal            modo terminal (menus)
  python gerador_pdf.py --grafico             força a janela gráfica
  python gerador_pdf.py --cli ajuda           esta ajuda

Modos:
  --cli misturar PDF... [-p N] [-n K] [--nao-aleatorizar] [--sem-repetir]
        Sorteia N páginas dos PDFs informados e gera K arquivos.

  --cli texto [--texto ARQ | --string "TXT" ...] [--foto IMG ...] [-p N] [--pdfs K]
        Gera PDFs usando seus textos (arquivos .txt) e fotos (JPG/PNG).
        Sem --texto/--string, usa textos aleatórios.
        Também aceita uma pasta: --texto pasta/ --foto pasta/

  --cli aleatorio [--pdfs K] [-p N] [--tamanho T] [--aleatorio-tamanho]
        Gera K PDFs com textos totalmente aleatórios.

Opções comuns:
  -p, --paginas N            páginas por PDF (padrão depende do modo)
  -n, --quantidade K         quantidade de PDFs gerados (padrão 1)
  --pdfs K                   igual a -n
  -o, --saida PASTA          pasta onde os PDFs serão salvos
  --tamanho T                A4, A5, A3, Letter, Legal, 10x15cm, 210x297mm,
                             400x600pt, "A4 paisagem" ou aleatorio
  --aleatorio-tamanho        sorteia o tamanho de cada PDF
  --largura-min MM, --largura-max MM
                             faixa de tamanho quando o tamanho é sorteado
  --aleatorio-paginas MIN MAX
                             sorteia o número de páginas entre MIN e MAX
  --fonte-serif              usa fonte com serifa (Times)
  --marca-dagua TEXTO        escreve uma marca d'água em diagonal
  --nome NOME                prefixo dos arquivos gerados

Exemplos:
  python gerador_pdf.py --cli misturar a.pdf b.pdf -p 12 -n 3
  python gerador_pdf.py --cli misturar ./pdfs/ -p 8 --sem-repetir
  python gerador_pdf.py --cli texto --texto cartas.txt --foto f1.jpg --foto f2.png -p 4 --pdfs 5
  python gerador_pdf.py --cli aleatorio --pdfs 10 -p 3 --aleatorio-tamanho
"""


def cli_mode(args):
    if args.cli in ("ajuda", "help", "-h", "--help", "?"):
        print(CLI_HELP)
        return 0
    out_dir = os.path.abspath(os.path.expanduser(args.saida or os.path.join(_base_dir(), "saida")))
    session = Session(out_dir)
    if args.largura_min and args.largura_max and args.largura_min >= args.largura_max:
        print("erro: --largura-max deve ser maior que --largura-min", file=sys.stderr)
        return 2

    def say(text):
        print(text)

    def common_opts():
        opts = {}
        if args.aleatorio_tamanho:
            opts["random_size"] = True
            opts["page_size"] = "aleatorio"
            if args.largura_min or args.largura_max:
                opts["size_min"] = float(args.largura_min or 80) * 72 / 25.4
                opts["size_max"] = float(args.largura_max or 200) * 72 / 25.4
        else:
            opts["random_size"] = False
            opts["page_size"] = _parse_size_arg(args.tamanho)
        if args.aleatorio_paginas:
            lo, hi = args.aleatorio_paginas
            opts["random_pages"] = True
            opts["pages_min"] = max(1, lo)
            opts["pages_max"] = max(1, hi)
        else:
            opts["random_pages"] = False
        opts["pdfs"] = args.pdfs or args.quantidade or 1
        opts["pages"] = args.paginas or 1
        opts["serif"] = bool(args.fonte_serif)
        opts["watermark"] = args.marca_dagua
        opts["filename"] = args.nome
        return opts

    try:
        if args.cli == "misturar":
            if not args.arquivos:
                print("erro: informe ao menos um PDF ou uma pasta.", file=sys.stderr)
                return 2
            session.add_sources(args.arquivos)
            pages = args.paginas or 10
            copies = args.pdfs or args.quantidade or 1
            files = session.run_mode1(pages, copies, not args.nao_aleatorizar,
                                      not args.sem_repetir, out_name=args.nome)
        elif args.cli == "texto":
            opts = common_opts()
            opts["pdfs"] = args.pdfs or args.quantidade or 1
            opts["pages"] = args.paginas or 1
            for item in args.texto or []:
                if os.path.isdir(item):
                    for name in sorted(os.listdir(item)):
                        if name.lower().endswith((".txt", ".md", ".csv")):
                            session.add_text_file(os.path.join(item, name))
                else:
                    session.add_text_file(item)
            for s in args.string or []:
                session.texts.append(s)
            for item in args.foto or []:
                if os.path.isdir(item):
                    for name in sorted(os.listdir(item)):
                        if name.lower().endswith((".jpg", ".jpeg", ".png")):
                            session.add_images([os.path.join(item, name)])
                else:
                    session.add_images([item])
            if args.arquivos:
                session.add_sources(args.arquivos)
            files = session.run_mode2(opts)
        elif args.cli == "aleatorio":
            opts = common_opts()
            opts["pdfs"] = args.pdfs or args.quantidade or 1
            opts["pages"] = args.paginas or 1
            if not args.tamanho and not args.aleatorio_tamanho:
                opts["random_size"] = True
            files = session.run_mode3(opts)
        else:
            print("modo desconhecido: %s (use 'ajuda')" % args.cli, file=sys.stderr)
            return 2
    except Exception as exc:
        print("erro: %s" % exc, file=sys.stderr)
        return 1
    print("Gerado(s) %d arquivo(s) em %s:" % (len(files), out_dir))
    for f in files:
        print("  " + f)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="gerador_pdf.py",
        description="Gerador de PDFs aleatórios (somente Python padrão).",
        add_help=True,
    )
    parser.add_argument("--terminal", action="store_true", help="força o modo terminal")
    parser.add_argument("--grafico", "--gui", dest="grafico", action="store_true",
                        help="força a interface gráfica")
    parser.add_argument("--cli", metavar="MODO",
                        help="misturar | texto | aleatorio | ajuda")
    parser.add_argument("-p", "--paginas", type=int, help="páginas por PDF")
    parser.add_argument("-n", "--quantidade", type=int, help="quantidade de PDFs")
    parser.add_argument("--pdfs", type=int, help="quantidade de PDFs")
    parser.add_argument("-o", "--saida", help="pasta de saída")
    parser.add_argument("--tamanho", help="A4, A5, Letter, 10x15cm, 400x600pt ou aleatorio")
    parser.add_argument("--aleatorio-tamanho", action="store_true")
    parser.add_argument("--largura-min", type=float, help="largura mínima em mm")
    parser.add_argument("--largura-max", type=float, help="largura máxima em mm")
    parser.add_argument("--aleatorio-paginas", nargs=2, type=int, metavar=("MIN", "MAX"))
    parser.add_argument("--nao-aleatorizar", action="store_true",
                        help="mantém a ordem original das páginas")
    parser.add_argument("--sem-repetir", action="store_true",
                        help="não repete páginas quando faltam")
    parser.add_argument("--texto", action="append", help="arquivo/pasta de texto")
    parser.add_argument("--string", action="append", help="texto direto na linha de comando")
    parser.add_argument("--foto", action="append", help="arquivo/pasta de fotos")
    parser.add_argument("--fonte-serif", action="store_true")
    parser.add_argument("--marca-dagua", help="texto da marca d'água")
    parser.add_argument("--nome", help="prefixo dos arquivos gerados")
    parser.add_argument("arquivos", nargs="*", help="PDFs (modo misturar) ou extras")
    args = parser.parse_args(argv)

    if args.cli:
        return cli_mode(args)

    session = Session(args.saida)
    if args.arquivos:
        try:
            session.add_sources(args.arquivos)
        except Exception as exc:
            print("aviso: %s" % exc, file=sys.stderr)

    if args.terminal:
        return terminal_mode(session)

    # decisão automática: gráfico só em computador com tela e tkinter
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
                       or sys.platform in ("win32", "darwin"))
    if args.grafico:
        if not has_display:
            print("Nenhuma tela detectada: usando o modo terminal.", file=sys.stderr)
            return terminal_mode(session)
        return graphical_mode(session)
    if has_display and _has_tk() and not in_termux() and not os.environ.get("GERADOR_PDF_TERMINAL"):
        try:
            return graphical_mode(session)
        except PDFError as exc:
            print(exc, file=sys.stderr)
            print("Usando o modo terminal.", file=sys.stderr)
    if not sys.stdin.isatty() and not sys.stdout.isatty():
        # chamado sem tela e sem terminal interativo: mostra a ajuda
        print(CLI_HELP)
        return 0
    return terminal_mode(session)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
