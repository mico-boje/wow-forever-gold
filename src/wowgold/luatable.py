"""Minimal parser for WoW SavedVariables files (Lua table literals).

SavedVariables are written by the game client as plain assignments:

    WowGoldLoggerDB = {
        ["sessions"] = {
            {
                ["method"] = "herb_felwood",
                ["items"] = { [13463] = 14, },
            }, -- [1]
        },
    }

Only literals appear (strings, numbers, booleans, nil, nested tables), so a small
recursive-descent parser is enough; no Lua code is ever executed.
"""

from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(
    r"""
    (?P<ws>\s+|--\[\[.*?\]\]|--[^\n]*)
  | (?P<str>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')
  | (?P<num>-?(?:0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][-+]?\d+)?|\.\d+(?:[eE][-+]?\d+)?))
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op>[{}\[\]=,;])
    """,
    re.VERBOSE | re.DOTALL,
)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "\n": "\n"}


class LuaParseError(ValueError):
    pass


def _unescape(body: str) -> str:
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if nxt.isdigit():  # \ddd decimal byte
            m = re.match(r"\d{1,3}", body[i + 1 :])
            assert m
            out.append(chr(int(m.group())))
            i += 1 + len(m.group())
        else:
            out.append(_ESCAPES.get(nxt, nxt))
            i += 2
    return "".join(out)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise LuaParseError(f"unexpected character {text[pos]!r} at offset {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind != "ws":
            tokens.append((kind, m.group()))
    return tokens


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.toks = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise LuaParseError("unexpected end of input")
        if value is not None and tok[1] != value:
            raise LuaParseError(f"expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def value(self) -> Any:
        kind, text = self.take()
        if kind == "str":
            return _unescape(text[1:-1])
        if kind == "num":
            if text.lower().startswith(("0x", "-0x")):
                return int(text, 16)
            return float(text) if any(c in text for c in ".eE") else int(text)
        if kind == "name":
            if text in ("true", "false"):
                return text == "true"
            if text == "nil":
                return None
            raise LuaParseError(f"unexpected identifier {text!r}")
        if text == "{":
            return self.table()
        raise LuaParseError(f"unexpected token {text!r}")

    def table(self) -> dict[Any, Any] | list[Any]:
        result: dict[Any, Any] = {}
        next_index = 1
        while True:
            tok = self.peek()
            if tok is None:
                raise LuaParseError("unterminated table")
            if tok[1] == "}":
                self.take()
                break
            if tok[1] == "[":
                self.take()
                key = self.value()
                self.take("]")
                self.take("=")
                result[key] = self.value()
            elif (
                tok[0] == "name" and self.i + 1 < len(self.toks) and self.toks[self.i + 1][1] == "="
            ):
                self.take()
                self.take("=")
                result[tok[1]] = self.value()
            else:
                result[next_index] = self.value()
                next_index += 1
            if (sep := self.peek()) and sep[1] in (",", ";"):
                self.take()
        # Pure arrays (keys 1..n) become Python lists.
        if (
            result
            and all(isinstance(k, int) for k in result)
            and sorted(result) == list(range(1, len(result) + 1))
        ):
            return [result[k] for k in range(1, len(result) + 1)]
        return result


def parse_saved_variables(text: str) -> dict[str, Any]:
    """Parse `Name = <value>` assignments into {Name: value}."""
    p = _Parser(_tokenize(text))
    out: dict[str, Any] = {}
    while p.peek() is not None:
        kind, name = p.take()
        if kind != "name":
            raise LuaParseError(f"expected a variable name, got {name!r}")
        p.take("=")
        out[name] = p.value()
    return out
