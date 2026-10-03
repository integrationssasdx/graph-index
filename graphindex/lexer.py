"""A self-contained GraphQL lexer.

Implements enough of the lexical grammar of the GraphQL specification
(https://spec.graphql.org/) to tokenise both SDL and executable documents:
punctuators, names, int/float literals, strings (incl. block strings),
comments, and the spread operator. Every token remembers its position so
parse errors can point at a location.
"""

from .errors import ParseError

# Single-character punctuators.
_PUNCT = set("!$&()*:=@[]{}|")


class Token:
    __slots__ = ("kind", "value", "line", "col", "pos")

    def __init__(self, kind, value, line, col, pos):
        self.kind = kind  # 'name' | 'number' | 'string' | 'punct' | 'eof'
        self.value = value
        self.line = line
        self.col = col
        self.pos = pos

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"Token({self.kind!r}, {self.value!r}, {self.line}:{self.col})"


def _is_name_start(c):
    return c == "_" or c.isalpha()


def _is_name_cont(c):
    return c == "_" or c.isalnum()


def tokenize(src: str):
    """Return the list of tokens in *src*, terminated by an ``eof`` token."""
    tokens = []
    i, n = 0, len(src)
    line, col = 1, 1

    def advance(k=1):
        nonlocal i, col, line
        for _ in range(k):
            if i < n and src[i] == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1

    def read_unicode_escape():
        # i points at the 'u'.
        advance()  # consume u
        hex_digits = ""
        for _ in range(4):
            if i >= n:
                raise ParseError("invalid unicode escape sequence")
            hex_digits += src[i]
            advance()
        try:
            return chr(int(hex_digits, 16))
        except ValueError:
            raise ParseError(f"invalid unicode escape \\u{hex_digits}")

    def read_string():
        # Opening quote at src[i] already observed; consume it.
        advance()
        out = []
        while True:
            if i >= n:
                raise ParseError("unterminated string literal")
            c = src[i]
            if c == '"':
                advance()  # consume closing "
                return "".join(out)
            if c in "\n\r":
                raise ParseError("unterminated string literal (newline in string)")
            if c == "\\":
                advance()
                if i >= n:
                    raise ParseError("unterminated escape sequence")
                esc = src[i]
                simple = {'"': '"', "\\": "\\", "/": "/", "b": "\b",
                          "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
                if esc in simple:
                    out.append(simple[esc])
                    advance()
                elif esc == "u":
                    out.append(read_unicode_escape())
                else:
                    raise ParseError(f"invalid escape character {esc!r}")
            else:
                out.append(c)
                advance()

    def read_block_string():
        # Opening triple quote observed.
        advance(3)
        out = []
        while True:
            if i >= n:
                raise ParseError("unterminated block string literal")
            if src.startswith('"""', i):
                advance(3)
                return _block_string_value("".join(out))
            if src.startswith('\\"""', i):
                out.append('"""')
                advance(4)
                continue
            out.append(src[i])
            advance()

    def read_number():
        start = i
        is_float = False
        if src[i] == "-":
            advance()
        if i >= n or not src[i].isdigit():
            raise ParseError("invalid number literal")
        if src[i] == "0":
            advance()
        else:
            while i < n and src[i].isdigit():
                advance()
        # Fractional part.
        if i < n and src[i] == ".":
            is_float = True
            advance()
            if i >= n or not src[i].isdigit():
                raise ParseError("invalid number literal: expected digit after '.'")
            while i < n and src[i].isdigit():
                advance()
        # Exponent.
        if i < n and src[i] in "eE":
            is_float = True
            advance()
            if i < n and src[i] in "+-":
                advance()
            if i >= n or not src[i].isdigit():
                raise ParseError("invalid number literal: expected exponent digits")
            while i < n and src[i].isdigit():
                advance()
        return ("float" if is_float else "int", src[start:i])

    while i < n:
        c = src[i]

        # Ignored: commas, whitespace, line terminators.
        if c in " \t\v\f\r\n,":
            advance()
            continue

        # Comments run to the end of the line.
        if c == "#":
            while i < n and src[i] not in "\n\r":
                advance()
            continue

        start_line, start_col, start_pos = line, col, i

        # Spread operator.
        if src.startswith("...", i):
            tokens.append(Token("punct", "...", start_line, start_col, start_pos))
            advance(3)
            continue

        if c in _PUNCT:
            tokens.append(Token("punct", c, start_line, start_col, start_pos))
            advance()
            continue

        # String literals (block and regular).
        if c == '"':
            if src.startswith('"""', i):
                value = read_block_string()
            else:
                value = read_string()
            tokens.append(Token("string", value, start_line, start_col, start_pos))
            continue

        # Numbers.
        if c == "-" or c.isdigit():
            value = read_number()
            tokens.append(Token("number", value, start_line, start_col, start_pos))
            continue

        # Names.
        if _is_name_start(c):
            j = i
            while i < n and _is_name_cont(src[i]):
                advance()
            text = src[j:i]
            tokens.append(Token("name", text, start_line, start_col, j))
            continue

        raise ParseError(f"unexpected character {c!r} at line {line}, column {col}")

    tokens.append(Token("eof", None, line, col, i))
    return tokens


def _block_string_value(raw: str) -> str:
    """Apply block-string indentation/blank-line normalization per spec."""
    lines = raw.split("\n")
    # Compute common indent from non-blank lines (excluding line 1).
    common_indent = None
    for ln in lines[1:]:
        stripped = ln.lstrip(" \t")
        if stripped:
            indent = len(ln) - len(stripped)
            common_indent = indent if common_indent is None else min(common_indent, indent)
    if common_indent:
        lines = [lines[0]] + [ln[common_indent:] for ln in lines[1:]]
    # Drop leading/trailing blank lines.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)
