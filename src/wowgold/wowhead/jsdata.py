"""Read the data Wowhead embeds in its page scripts.

Listview data is almost JSON, but not quite: some keys are unquoted (`quality:-1`), strings
can use single quotes, and big tables are assigned to a variable first
(`var listviewspells = [...]; new Listview({..., data: listviewspells})`).
"""

from __future__ import annotations

import json
import re

_IDENT_START = re.compile(r"[A-Za-z_$]")
_IDENT = re.compile(r"[A-Za-z0-9_$]*")


def _read_string(s: str, i: int) -> tuple[str, int]:
    """String literal starting at s[i]; returns (JSON string literal, index after it)."""
    quote, j, chars = s[i], i + 1, []
    while j < len(s):
        ch = s[j]
        if ch == "\\" and j + 1 < len(s):
            nxt = s[j + 1]
            # \' is only an escape in JS; JSON wants a plain quote
            chars.append("'" if nxt == "'" else "\\" + nxt)
            j += 2
            continue
        if ch == quote:
            break
        chars.append('\\"' if ch == '"' and quote == "'" else ch)
        j += 1
    return '"' + "".join(chars) + '"', j + 1


def js_to_json(s: str) -> str:
    """Turn a JS object/array literal into JSON: quote bare keys, convert single-quoted
    strings, map undefined to null, drop trailing commas."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if ch in "\"'":
            lit, i = _read_string(s, i)
            out.append(lit)
            continue
        if _IDENT_START.match(ch):
            word = ch + _IDENT.match(s, i + 1).group(0)
            j = i + len(word)
            k = j
            while k < n and s[k].isspace():
                k += 1
            prev = next((c for c in reversed("".join(out[-3:])) if not c.isspace()), "")
            if k < n and s[k] == ":" and prev in "{,":
                out.append(json.dumps(word))
            elif word == "undefined":
                out.append("null")
            else:
                out.append(word)
            i = j
            continue
        if ch == ",":
            k = i + 1
            while k < n and s[k].isspace():
                k += 1
            if k < n and s[k] in "]}":
                i += 1
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def balanced(text: str, start: int) -> str | None:
    """The bracketed value starting at text[start] ('[' or '{'), respecting strings."""
    open_, close = text[start], {"[": "]", "{": "}"}[text[start]]
    depth, i, in_str, quote = 0, start, False, ""
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 1
            elif ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str, quote = True, ch
        elif ch == open_:
            depth += 1
        elif ch == close:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
        i += 1
    return None


def parse_js(literal: str):
    """Parse a JS literal (strict JSON first, then the tolerant conversion). None on failure."""
    for text in (literal, None):
        try:
            return json.loads(text if text is not None else js_to_json(literal))
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def array_vars(script: str) -> dict[str, list]:
    """`var name = [...]` assignments in a script, parsed."""
    out = {}
    for m in re.finditer(r"\bvar\s+([A-Za-z_$][\w$]*)\s*=\s*\[", script):
        lit = balanced(script, m.end() - 1)
        if lit and isinstance(value := parse_js(lit), list):
            out[m[1]] = value
    return out
