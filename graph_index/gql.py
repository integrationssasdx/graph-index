"""Self-contained GraphQL lexer and parser.

Parses both schema definition documents (SDL) and executable documents
(operations, fragments) into small dataclass ASTs. Syntax problems raise
PlanError with code "ParseError".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .errors import PlanError

PUNCTUATORS = set("!$():=@[]{|}&")
NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")

# Type references are nested tuples: ("named", name) | ("list", ref) | ("non_null", ref).
TypeRef = Tuple


# ---------------------------------------------------------------------------
# AST nodes
# ---------------------------------------------------------------------------


@dataclass
class Var:
    name: str


class EnumLiteral(str):
    """An unquoted enum literal in an argument or default-value position.

    It subclasses ``str`` so the existing filter/variable paths treat it
    exactly like the bare string it has always resolved to, while callers
    that require a String literal (introspection ``name``) can detect and
    reject it.
    """


@dataclass
class Directive:
    name: str
    args: Dict[str, Any]


@dataclass
class InputValueDef:
    name: str
    type: TypeRef
    default: Any = None
    has_default: bool = False


@dataclass
class FieldDef:
    name: str
    args: List[InputValueDef]
    type: TypeRef
    directives: List[Directive]


@dataclass
class ObjectTypeDef:
    name: str
    fields: List[FieldDef]
    directives: List[Directive]
    interfaces: List[str]
    is_extend: bool = False


@dataclass
class InputDef:
    name: str
    fields: List[InputValueDef]


@dataclass
class EnumDef:
    name: str
    values: List[str]


@dataclass
class DirectiveDef:
    name: str
    args: List[InputValueDef]
    repeatable: bool
    locations: List[str]


@dataclass
class Operation:
    op_type: str  # "query" | "mutation" | "subscription"
    name: Optional[str]
    var_defs: List[Tuple[str, TypeRef, Any, bool]]  # (name, type, default, has_default)
    selection_set: list


@dataclass
class FieldNode:
    name: str
    alias: Optional[str]
    args: Dict[str, Any]
    selection_set: Optional[list]  # None when the field has no subselection


@dataclass
class FragmentSpread:
    name: str


@dataclass
class InlineFragment:
    type_condition: Optional[str]
    selection_set: list


@dataclass
class FragmentDef:
    name: str
    type_condition: str
    selection_set: list


@dataclass
class Token:
    kind: str  # "name" | "int" | "float" | "string" | "punct" | "eof"
    value: str
    line: int
    col: int


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------


def tokenize(text: str, source: str) -> List[Token]:
    tokens: List[Token] = []
    i = 0
    line = 1
    col = 1
    n = len(text)

    def error(message: str, ln: Optional[int] = None, cl: Optional[int] = None):
        raise PlanError("ParseError", f"{source}:{ln or line}:{cl or col}: {message}")

    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
                col += 1
            continue
        start_line, start_col = line, col
        if ch == ".":
            if text[i : i + 3] == "...":
                tokens.append(Token("punct", "...", start_line, start_col))
                i += 3
                col += 3
                continue
            error("unexpected character '.'")
        if ch in PUNCTUATORS:
            tokens.append(Token("punct", ch, start_line, start_col))
            i += 1
            col += 1
            continue
        if ch == '"':
            if text[i : i + 3] == '"""':
                j = i + 3
                buf: List[str] = []
                closed = False
                while j < n:
                    if text[j] == "\\" and text[j + 1 : j + 4] == '"""':
                        buf.append('"""')
                        j += 4
                    elif text[j : j + 3] == '"""':
                        closed = True
                        break
                    else:
                        buf.append(text[j])
                        j += 1
                if not closed:
                    error("unterminated block string", start_line, start_col)
                segment = text[i : j + 3]
                tokens.append(Token("string", "".join(buf), start_line, start_col))
                i = j + 3
            else:
                j = i + 1
                buf = []
                closed = False
                while j < n:
                    c = text[j]
                    if c == '"':
                        closed = True
                        break
                    if c == "\n":
                        error("unterminated string", start_line, start_col)
                    if c == "\\":
                        esc = text[j + 1] if j + 1 < n else ""
                        simple = {
                            '"': '"',
                            "\\": "\\",
                            "/": "/",
                            "b": "\b",
                            "f": "\f",
                            "n": "\n",
                            "r": "\r",
                            "t": "\t",
                        }
                        if esc == "u":
                            hexs = text[j + 2 : j + 6]
                            if len(hexs) < 4 or any(
                                c2 not in "0123456789abcdefABCDEF" for c2 in hexs
                            ):
                                error("invalid unicode escape", start_line, start_col)
                            buf.append(chr(int(hexs, 16)))
                            j += 6
                        elif esc in simple:
                            buf.append(simple[esc])
                            j += 2
                        else:
                            error(f"invalid escape '\\{esc}'", start_line, start_col)
                    else:
                        buf.append(c)
                        j += 1
                if not closed:
                    error("unterminated string", start_line, start_col)
                segment = text[i : j + 1]
                tokens.append(Token("string", "".join(buf), start_line, start_col))
                i = j + 1
            newlines = segment.count("\n")
            if newlines:
                line += newlines
                col = len(segment) - segment.rindex("\n")
            else:
                col += len(segment)
            continue
        if ch == "-" or ch.isdigit():
            m = NUMBER_RE.match(text, i)
            if not m:
                error("invalid number", start_line, start_col)
            sval = m.group(0)
            kind = "float" if ("." in sval or "e" in sval or "E" in sval) else "int"
            tokens.append(Token(kind, sval, start_line, start_col))
            i = m.end()
            col += len(sval)
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(Token("name", text[i:j], start_line, start_col))
            col += j - i
            i = j
            continue
        error(f"unexpected character {ch!r}")
    tokens.append(Token("eof", "", line, col))
    return tokens


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class Parser:
    def __init__(self, text: str, source: str):
        self.source = source
        self.tokens = tokenize(text, source)
        self.pos = 0

    def peek(self) -> Token:
        return self.tokens[self.pos]

    def error(self, message: str) -> "PlanError":
        tok = self.peek()
        raise PlanError("ParseError", f"{self.source}:{tok.line}:{tok.col}: {message}")

    def describe(self) -> str:
        tok = self.peek()
        if tok.kind == "eof":
            return "end of input"
        return repr(tok.value)

    def advance(self) -> Token:
        tok = self.tokens[self.pos]
        if tok.kind != "eof":
            self.pos += 1
        return tok

    def at_punct(self, p: str) -> bool:
        tok = self.peek()
        return tok.kind == "punct" and tok.value == p

    def eat_punct(self, p: str) -> bool:
        if self.at_punct(p):
            self.advance()
            return True
        return False

    def expect_punct(self, p: str) -> None:
        if not self.eat_punct(p):
            self.error(f"expected '{p}', found {self.describe()}")

    def at_name(self, name: Optional[str] = None) -> bool:
        tok = self.peek()
        return tok.kind == "name" and (name is None or tok.value == name)

    def expect_name(self) -> str:
        tok = self.peek()
        if tok.kind != "name":
            self.error(f"expected a name, found {self.describe()}")
        self.advance()
        return tok.value

    # -- types ---------------------------------------------------------------

    def parse_type_ref(self) -> TypeRef:
        if self.eat_punct("["):
            inner = self.parse_type_ref()
            self.expect_punct("]")
            ref: TypeRef = ("list", inner)
        else:
            ref = ("named", self.expect_name())
        if self.eat_punct("!"):
            ref = ("non_null", ref)
        return ref

    # -- values ----------------------------------------------------------------

    def parse_value(self, const: bool = False) -> Any:
        tok = self.peek()
        if tok.kind == "punct" and tok.value == "$":
            if const:
                self.error("variables are not allowed in this position")
            self.advance()
            return Var(self.expect_name())
        if tok.kind == "int":
            self.advance()
            return int(tok.value)
        if tok.kind == "float":
            self.advance()
            return float(tok.value)
        if tok.kind == "string":
            self.advance()
            return tok.value
        if tok.kind == "name":
            self.advance()
            if tok.value == "true":
                return True
            if tok.value == "false":
                return False
            if tok.value == "null":
                return None
            return EnumLiteral(tok.value)  # enum value
        if self.eat_punct("["):
            items = []
            while not self.eat_punct("]"):
                items.append(self.parse_value(const))
            return items
        if self.eat_punct("{"):
            obj: Dict[str, Any] = {}
            while not self.eat_punct("}"):
                key = self.expect_name()
                if key in obj:
                    self.error(f"duplicate object field '{key}'")
                self.expect_punct(":")
                obj[key] = self.parse_value(const)
            return obj
        self.error(f"expected a value, found {self.describe()}")

    def parse_directives(self, const: bool = False) -> List[Directive]:
        directives = []
        while self.eat_punct("@"):
            name = self.expect_name()
            args: Dict[str, Any] = {}
            if self.eat_punct("("):
                while not self.eat_punct(")"):
                    arg_name = self.expect_name()
                    if arg_name in args:
                        self.error(f"duplicate argument '{arg_name}' on '@{name}'")
                    self.expect_punct(":")
                    args[arg_name] = self.parse_value(const)
            directives.append(Directive(name, args))
        return directives

    # -- executable documents ---------------------------------------------------

    def parse_selection_set(self) -> list:
        self.expect_punct("{")
        selections = []
        while not self.at_punct("}"):
            if self.peek().kind == "eof":
                self.error("unterminated selection set")
            selections.append(self.parse_selection())
        self.expect_punct("}")
        return selections

    def parse_selection(self):
        if self.eat_punct("..."):
            if self.at_name("on"):
                self.advance()
                condition = self.expect_name()
                self.parse_directives()
                return InlineFragment(condition, self.parse_selection_set())
            if self.at_punct("{") or self.at_punct("@"):
                self.parse_directives()
                return InlineFragment(None, self.parse_selection_set())
            name = self.expect_name()
            self.parse_directives()
            return FragmentSpread(name)
        first = self.expect_name()
        alias = None
        name = first
        if self.eat_punct(":"):
            alias = first
            name = self.expect_name()
        args: Dict[str, Any] = {}
        if self.eat_punct("("):
            while not self.eat_punct(")"):
                arg_name = self.expect_name()
                if arg_name in args:
                    self.error(f"duplicate argument '{arg_name}'")
                self.expect_punct(":")
                args[arg_name] = self.parse_value()
        self.parse_directives()
        selection_set = None
        if self.at_punct("{"):
            selection_set = self.parse_selection_set()
        return FieldNode(name, alias, args, selection_set)


def parse_executable(text: str, source: str):
    """Parse an executable document. Returns (operations, fragments)."""
    parser = Parser(text, source)
    operations: List[Operation] = []
    fragments: Dict[str, FragmentDef] = {}
    while parser.peek().kind != "eof":
        if parser.at_punct("{"):
            operations.append(Operation("query", None, [], parser.parse_selection_set()))
            continue
        if parser.at_name("query") or parser.at_name("mutation") or parser.at_name("subscription"):
            op_type = parser.advance().value
            name = None
            if parser.peek().kind == "name":
                name = parser.advance().value
            var_defs = []
            if parser.eat_punct("("):
                while not parser.eat_punct(")"):
                    parser.expect_punct("$")
                    var_name = parser.expect_name()
                    parser.expect_punct(":")
                    var_type = parser.parse_type_ref()
                    default = None
                    has_default = False
                    if parser.eat_punct("="):
                        default = parser.parse_value(const=True)
                        has_default = True
                    parser.parse_directives(const=True)
                    var_defs.append((var_name, var_type, default, has_default))
            parser.parse_directives()
            operations.append(Operation(op_type, name, var_defs, parser.parse_selection_set()))
            continue
        if parser.at_name("fragment"):
            parser.advance()
            frag_name = parser.expect_name()
            if frag_name == "on":
                parser.error("fragment name cannot be 'on'")
            if not parser.at_name("on"):
                parser.error("expected 'on' after fragment name")
            parser.advance()
            condition = parser.expect_name()
            parser.parse_directives()
            selection_set = parser.parse_selection_set()
            if frag_name in fragments:
                parser.error(f"duplicate fragment '{frag_name}'")
            fragments[frag_name] = FragmentDef(frag_name, condition, selection_set)
            continue
        parser.error(f"unexpected {parser.describe()}")
    return operations, fragments


# ---------------------------------------------------------------------------
# Schema (SDL) documents
# ---------------------------------------------------------------------------


class SdlDocument:
    def __init__(self):
        self.types: List[ObjectTypeDef] = []
        self.interfaces: List[ObjectTypeDef] = []
        self.inputs: List[InputDef] = []
        self.scalars: List[str] = []
        self.enums: List[EnumDef] = []
        self.directives: List[DirectiveDef] = []
        self.unions: Dict[str, List[str]] = {}
        self.roots: Dict[str, str] = {}
        self.schema_seen = False


def _parse_field_def(parser: Parser) -> FieldDef:
    name = parser.expect_name()
    args: List[InputValueDef] = []
    if parser.eat_punct("("):
        while not parser.eat_punct(")"):
            if parser.peek().kind == "string":
                parser.advance()  # argument description
            args.append(_parse_input_value_def(parser))
    parser.expect_punct(":")
    type_ref = parser.parse_type_ref()
    directives = parser.parse_directives(const=True)
    return FieldDef(name, args, type_ref, directives)


def _parse_input_value_def(parser: Parser) -> InputValueDef:
    name = parser.expect_name()
    parser.expect_punct(":")
    type_ref = parser.parse_type_ref()
    default = None
    has_default = False
    if parser.eat_punct("="):
        default = parser.parse_value(const=True)
        has_default = True
    parser.parse_directives(const=True)
    return InputValueDef(name, type_ref, default, has_default)


def _parse_object_type(parser: Parser, is_extend: bool) -> ObjectTypeDef:
    name = parser.expect_name()
    interfaces: List[str] = []
    if parser.at_name("implements"):
        parser.advance()
        parser.eat_punct("&")
        interfaces.append(parser.expect_name())
        while parser.eat_punct("&"):
            interfaces.append(parser.expect_name())
    directives = parser.parse_directives(const=True)
    fields: List[FieldDef] = []
    if parser.eat_punct("{"):
        while not parser.eat_punct("}"):
            if parser.peek().kind == "string":
                parser.advance()  # field description
            fields.append(_parse_field_def(parser))
    return ObjectTypeDef(name, fields, directives, interfaces, is_extend)


def _parse_schema_body(parser: Parser, doc: SdlDocument) -> None:
    parser.parse_directives(const=True)
    parser.expect_punct("{")
    while not parser.eat_punct("}"):
        op = parser.expect_name()
        if op not in ("query", "mutation", "subscription"):
            parser.error(f"unknown root operation '{op}'")
        parser.expect_punct(":")
        type_name = parser.expect_name()
        if op in doc.roots:
            parser.error(f"duplicate root operation '{op}'")
        doc.roots[op] = type_name


def parse_schema_document(text: str, source: str) -> SdlDocument:
    parser = Parser(text, source)
    doc = SdlDocument()
    while parser.peek().kind != "eof":
        if parser.peek().kind == "string":
            parser.advance()  # definition description
        if parser.at_name("schema"):
            if doc.schema_seen:
                parser.error("duplicate schema definition")
            doc.schema_seen = True
            parser.advance()
            _parse_schema_body(parser, doc)
        elif parser.at_name("scalar"):
            parser.advance()
            doc.scalars.append(parser.expect_name())
            parser.parse_directives(const=True)
        elif parser.at_name("type"):
            parser.advance()
            doc.types.append(_parse_object_type(parser, is_extend=False))
        elif parser.at_name("interface"):
            parser.advance()
            doc.interfaces.append(_parse_object_type(parser, is_extend=False))
        elif parser.at_name("input"):
            parser.advance()
            name = parser.expect_name()
            parser.parse_directives(const=True)
            fields: List[InputValueDef] = []
            if parser.eat_punct("{"):
                while not parser.eat_punct("}"):
                    if parser.peek().kind == "string":
                        parser.advance()
                    fields.append(_parse_input_value_def(parser))
            doc.inputs.append(InputDef(name, fields))
        elif parser.at_name("enum"):
            parser.advance()
            enum_name = parser.expect_name()
            parser.parse_directives(const=True)
            enum_values: List[str] = []
            if parser.eat_punct("{"):
                while not parser.eat_punct("}"):
                    if parser.peek().kind == "string":
                        parser.advance()
                    enum_values.append(parser.expect_name())
                    parser.parse_directives(const=True)
            doc.enums.append(EnumDef(enum_name, enum_values))
        elif parser.at_name("union"):
            parser.advance()
            union_name = parser.expect_name()
            parser.parse_directives(const=True)
            members: List[str] = []
            if parser.eat_punct("="):
                parser.eat_punct("|")
                members.append(parser.expect_name())
                while parser.eat_punct("|"):
                    members.append(parser.expect_name())
            doc.unions[union_name] = members
        elif parser.at_name("directive"):
            parser.advance()
            parser.expect_punct("@")
            directive_name = parser.expect_name()
            directive_args: List[InputValueDef] = []
            if parser.eat_punct("("):
                while not parser.eat_punct(")"):
                    if parser.peek().kind == "string":
                        parser.advance()
                    directive_args.append(_parse_input_value_def(parser))
            repeatable = False
            if parser.at_name("repeatable"):
                parser.advance()
                repeatable = True
            if not parser.at_name("on"):
                parser.error("expected 'on' in directive definition")
            parser.advance()
            parser.eat_punct("|")
            locations: List[str] = []
            locations.append(parser.expect_name())
            while parser.eat_punct("|"):
                locations.append(parser.expect_name())
            doc.directives.append(
                DirectiveDef(directive_name, directive_args, repeatable, locations)
            )
        elif parser.at_name("extend"):
            parser.advance()
            if parser.at_name("type"):
                parser.advance()
                doc.types.append(_parse_object_type(parser, is_extend=True))
            elif parser.at_name("schema"):
                parser.advance()
                _parse_schema_body(parser, doc)
            else:
                parser.error("unsupported 'extend' target")
        else:
            parser.error(f"unexpected {parser.describe()}")
    return doc
