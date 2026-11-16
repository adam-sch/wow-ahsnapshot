"""Parser for WoW SavedVariables files (a restricted Lua table-constructor syntax).

WoW writes files like:

    AHSnapshotDB = {
    ["version"] = 1,
    ["snapshots"] = {
    {
    ["t"] = 1763300000,
    }, -- [1]
    },
    }

Tables become dicts; tables whose keys are exactly 1..n become lists.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|--\[\[.*?\]\]|--[^\n]*)                # whitespace and comments
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')      # quoted strings
  | (?P<num>-?(?:0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<punct>[{}\[\]=,;])
    """,
    re.VERBOSE | re.DOTALL,
)

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "a": "\a", "b": "\b", "f": "\f", "v": "\v",
            "\\": "\\", '"': '"', "'": "'", "\n": "\n"}
_ESCAPE_RE = re.compile(r"\\(\d{1,3}|.)", re.DOTALL)


class LuaParseError(ValueError):
    pass


def _unescape(body: str) -> str:
    if "\\" not in body:
        return body

    def repl(m: re.Match) -> str:
        e = m.group(1)
        if e.isdigit():
            return chr(int(e))
        return _ESCAPES.get(e, e)

    # \ddd escapes are bytes; WoW strings are UTF-8, so rebuild bytes then decode.
    raw = _ESCAPE_RE.sub(repl, body)
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw


def _tokens(text: str):
    pos, end = 0, len(text)
    match = _TOKEN.match
    while pos < end:
        m = match(text, pos)
        if not m:
            snippet = text[pos:pos + 40].replace("\n", "\\n")
            raise LuaParseError(f"unexpected input at offset {pos}: {snippet!r}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        value = m.group()
        if kind == "str":
            yield "str", _unescape(value[1:-1])
        elif kind == "num":
            if value.lstrip("-").lower().startswith("0x"):
                yield "num", int(value, 16)
            elif re.fullmatch(r"-?\d+", value):
                yield "num", int(value)
            else:
                yield "num", float(value)
        elif kind == "name":
            yield "name", value
        else:
            yield value, value


class _Parser:
    def __init__(self, text: str):
        self.toks = list(_tokens(text))
        self.i = 0

    def peek(self, offset: int = 0):
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else ("eof", None)

    def take(self, kind: str | None = None):
        tok = self.peek()
        if kind is not None and tok[0] != kind:
            raise LuaParseError(f"expected {kind!r}, got {tok!r} (token {self.i})")
        self.i += 1
        return tok

    def value(self) -> Any:
        kind, val = self.take()
        if kind in ("str", "num"):
            return val
        if kind == "name":
            if val == "true":
                return True
            if val == "false":
                return False
            if val == "nil":
                return None
            raise LuaParseError(f"unexpected name {val!r}")
        if kind == "{":
            return self.table()
        raise LuaParseError(f"unexpected token {kind!r}")

    def table(self) -> Any:
        result: dict[Any, Any] = {}
        next_index = 1
        while self.peek()[0] != "}":
            kind = self.peek()[0]
            if kind == "[":
                self.take("[")
                key = self.value()
                self.take("]")
                self.take("=")
                result[key] = self.value()
            elif kind == "name" and self.peek(1)[0] == "=":
                key = self.take()[1]
                self.take("=")
                result[key] = self.value()
            else:
                result[next_index] = self.value()
                next_index += 1
            if self.peek()[0] in (",", ";"):
                self.take()
        self.take("}")
        if result and all(isinstance(k, int) for k in result) and sorted(result) == list(range(1, len(result) + 1)):
            return [result[k] for k in range(1, len(result) + 1)]
        return result

    def chunk(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        while self.peek()[0] != "eof":
            name = self.take("name")[1]
            self.take("=")
            out[name] = self.value()
            if self.peek()[0] == ";":
                self.take()
        return out


def loads(text: str) -> dict[str, Any]:
    """Parse a SavedVariables file body into {global_name: value}."""
    return _Parser(text).chunk()


def load(path) -> dict[str, Any]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return loads(f.read())
