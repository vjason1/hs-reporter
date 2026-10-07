"""Objective conditions: a HammerScript subset that can be validated and evaluated against
scanned file metadata.

Supported, following Hammerspace's documented forms (e.g. IF LAST_USE_AGE<90*DAYS THEN ...,
ACCESS_AGE>=2DAYS, SIZE<2*MBYTES AND HAS_ONLINE_INSTANCE, GET_TAG("skip_virus_scan")):

    fields          SIZE, MODIFY_AGE, OWNER, IS_ONLINE, ... (see FIELDS)
    numbers+units   30*DAYS  30DAYS  2*MBYTES  40BYTES  1.5 GBYTES
    literals        "text"  TRUE  FALSE  NOW  LOCAL_TIME('2026-01-01')  USER('alice@dom')  ITEM_TYPE('FILE')
    operators       = == != < <= > >=   + - * /   AND OR NOT (also && || !)   ( )
    functions       GET_TAG("n")  HAS_TAG("n")  GET_ATTRIBUTE("n")  HAS_ATTRIBUTE("n")  HAS_LABEL("n")  HAS_KEYWORD("n")
    name and path   FNMATCH('*.log', NAME)?TRUE   !FNMATCH('*/scratch/*', PATH)?TRUE
    conditional     cond ? a : b   (C-style; without ": b" a false condition gives no value)

Function calls on tags, labels, keywords and attributes are gathered by the scan like fields.
"""
import fnmatch
import re

from .hsvalue import SIZE_UNITS, TIME_UNITS, Time, Typed, parse_time

# The fields offered for conditions (the commonly used ones), with types for the builder.
# kind: bool | bytes | age | time | text | typed:<KIND> | labels
FIELDS = {
    "SIZE": ("bytes", "Size"), "SPACE_USED": ("bytes", "Space used"),
    "NAME": ("text", "File name"), "PATH": ("text", "Path"), "DPATH": ("text", "Path from the scanned folder"),
    "TYPE": ("typed:ITEM_TYPE", "Type (FILE, DIRECTORY, SYMLINK)"),
    "OWNER": ("typed:USER", "Owner"), "OWNER_GROUP": ("typed:GROUP", "Owning group"),
    "PARENT_SHARE": ("typed:SHARE", "Share"),
    "CREATE_AGE": ("age", "Created ago"), "CHANGE_AGE": ("age", "Changed ago"),
    "ACCESS_AGE": ("age", "Accessed ago"), "MODIFY_AGE": ("age", "Modified ago"),
    "LAST_USE_AGE": ("age", "Last used ago"),
    "ACTUAL_CREATE_AGE": ("age", "Actually created ago"), "ACTUAL_ACCESS_AGE": ("age", "Actually accessed ago"),
    "ACTUAL_MODIFY_AGE": ("age", "Actually modified ago"), "ACTUAL_LAST_USE_AGE": ("age", "Actually last used ago"),
    "CREATE_TIME": ("time", "Create time"), "CHANGE_TIME": ("time", "Change time"),
    "ACCESS_TIME": ("time", "Access time"), "MODIFY_TIME": ("time", "Modify time"),
    "LAST_USE_TIME": ("time", "Last use time"),
    "ACTUAL_CREATE_TIME": ("time", "Actual create time"), "ACTUAL_MODIFY_TIME": ("time", "Actual modify time"),
    "ACTUAL_ACCESS_TIME": ("time", "Actual access time"), "ACTUAL_LAST_USE_TIME": ("time", "Actual last use time"),
    "IS_BEING_CREATED": ("bool", "Being created"), "IS_MODIFIED_AFTER_SNAP": ("bool", "Modified after snapshot"),
    "DATA_ORIGIN_LOCAL": ("bool", "Data originated locally"), "DATA_ORIGIN_REMOTE": ("bool", "Data originated remotely"),
    "IS_OPEN": ("bool", "Open"), "HAS_ONLINE_INSTANCE": ("bool", "Has an online instance"),
    "IS_ONLINE": ("bool", "Online"), "IS_BEING_MOVED": ("bool", "Being moved"), "IS_OFFLINE": ("bool", "Offline"),
    "IS_DURABLE": ("bool", "Durable"), "IS_UNAVAILABLE": ("bool", "Unavailable"), "IS_AVAILABLE": ("bool", "Available"),
    "IS_LIVE": ("bool", "Live (not a snapshot)"), "IS_SNAP": ("bool", "Snapshot"),
    "IS_FILE": ("bool", "Is a file"), "IS_SYMLINK": ("bool", "Is a symlink"), "IS_DIRECTORY": ("bool", "Is a directory"),
    "DIRECTORY": ("bool", "Directory attribute"), "ARCHIVE": ("bool", "Archive attribute"),
    "IS_NEWBORN": ("bool", "Newborn"), "IS_RECENTLY_USED": ("bool", "Recently used"),
    "IS_UNUSED_SINCE_CREATION": ("bool", "Unused since creation"),
    "ALL_LABELS": ("labels", "All labels"),
}
# Known from the folder walk, so never need hs
WALK_FIELDS = {"NAME", "PATH", "DPATH", "IS_FILE", "IS_DIRECTORY", "IS_SYMLINK"}
# Always gathered: the capacity model needs them
REQUIRED_FIELDS = ["SIZE", "SPACE_USED"]
META_FUNCS = {"GET_TAG", "HAS_TAG", "GET_ATTRIBUTE", "HAS_ATTRIBUTE", "HAS_LABEL", "HAS_KEYWORD"}
TYPED_FUNCS = {"USER", "GROUP", "SHARE", "ITEM_TYPE", "STORAGE_VOLUME", "SITE", "MIME_TYPE", "VIRUS_SCAN_STATE"}
TIME_FUNCS = {"LOCAL_TIME", "TIME", "UTC_TIME"}
UNITS = {**SIZE_UNITS, **TIME_UNITS}


class ExprError(ValueError):
    def __init__(self, msg, pos=None):
        super().__init__(msg)
        self.pos = pos


_TOK = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)(?P<unit>[A-Za-z]+)?
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<quoted>'(?:[^'\\]|\\.)*')
  | (?P<op>==|!=|<=|>=|&&|\|\||[=<>!+\-*/(),?:])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
""", re.X)


def tokenize(text):
    out, pos = [], 0
    while pos < len(text):
        m = _TOK.match(text, pos)
        if not m:
            raise ExprError(f"Unexpected character {text[pos]!r}", pos)
        if m.lastgroup == "ws" or (m.group("ws")):
            pos = m.end()
            continue
        if m.group("num") is not None:
            unit = m.group("unit")
            if unit and unit.upper() not in UNITS:
                raise ExprError(f"Unknown unit {unit!r}", m.start("unit"))
            out.append(("num", m.group("num"), m.start(), unit.upper() if unit else None))
        elif m.group("str") is not None:
            out.append(("str", m.group("str")[1:-1], m.start(), None))
        elif m.group("quoted") is not None:
            out.append(("quoted", m.group("quoted")[1:-1], m.start(), None))
        elif m.group("op") is not None:
            out.append(("op", m.group("op"), m.start(), None))
        else:
            out.append(("ident", m.group("ident"), m.start(), None))
        pos = m.end()
    return out


def meta_key(func: str, arg: str) -> str:
    return f'{func.upper()}("{arg}")'


class _Parser:
    """Recursive descent to a small AST: ('or',a,b) ('and',a,b) ('not',a) ('cmp',op,a,b)
    ('arith',op,a,b) ('neg',a) ('const',v) ('field',name) ('meta',key)."""

    def __init__(self, text):
        self.text = text
        self.t = tokenize(text)
        self.i = 0
        self.fields = set()
        self.metas = set()

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else ("end", None, len(self.text), None)

    def take(self, kind=None, value=None):
        tok = self.peek()
        if (kind and tok[0] != kind) or (value is not None and (tok[1] or "").upper() != value):
            want = f"'{value}'" if value else kind
            got = repr(tok[1]) if tok[0] != "end" else "end of expression"
            raise ExprError(f"Expected {want}, found {got}", tok[2])
        self.i += 1
        return tok

    def is_kw(self, *words):
        tok = self.peek()
        return tok[0] == "ident" and tok[1].upper() in words or tok[0] == "op" and tok[1] in words

    def parse(self):
        if not self.t:
            raise ExprError("Empty condition", 0)
        node = self.ternary()
        if self.peek()[0] != "end":
            tok = self.peek()
            raise ExprError(f"Unexpected {tok[1]!r}", tok[2])
        return node

    def ternary(self):
        """cond ? a : b, lowest precedence and right-associative, as in C."""
        n = self.or_()
        if self.peek()[0] == "op" and self.peek()[1] == "?":
            self.i += 1
            a = self.ternary()
            b = None
            if self.peek()[0] == "op" and self.peek()[1] == ":":
                self.i += 1
                b = self.ternary()
            n = ("tern", n, a, b)
        return n

    def or_(self):
        n = self.and_()
        while self.is_kw("OR", "||"):
            self.i += 1
            n = ("or", n, self.and_())
        return n

    def and_(self):
        n = self.not_()
        while self.is_kw("AND", "&&"):
            self.i += 1
            n = ("and", n, self.not_())
        return n

    def not_(self):
        if self.is_kw("NOT", "!"):
            self.i += 1
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        n = self.sum_()
        tok = self.peek()
        if tok[0] == "op" and tok[1] in ("=", "==", "!=", "<", "<=", ">", ">="):
            self.i += 1
            op = "==" if tok[1] == "=" else tok[1]
            n = ("cmp", op, n, self.sum_())
        return n

    def sum_(self):
        n = self.term()
        while self.peek()[0] == "op" and self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            n = ("arith", op, n, self.term())
        return n

    def term(self):
        n = self.unary()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            n = ("arith", op, n, self.unary())
        return n

    def unary(self):
        if self.peek()[0] == "op" and self.peek()[1] == "-":
            self.i += 1
            return ("neg", self.unary())
        return self.primary()

    def primary(self):
        kind, val, pos, unit = self.peek()
        if kind == "num":
            self.i += 1
            num = float(val)
            if unit:
                return ("const", num * UNITS[unit])
            # "1.5 GBYTES": a number followed by a separate unit word
            nk, nv, _, _ = self.peek()
            if nk == "ident" and nv.upper() in UNITS:
                self.i += 1
                return ("const", num * UNITS[nv.upper()])
            return ("const", num)
        if kind == "str":
            self.i += 1
            return ("const", val)
        if kind == "op" and val == "(":
            self.i += 1
            n = self.ternary()
            self.take("op", ")")
            return n
        if kind == "ident":
            up = val.upper()
            self.i += 1
            if up in ("TRUE", "FALSE"):
                return ("const", up == "TRUE")
            if up == "NOW":
                return ("now",)
            if up in UNITS:
                return ("const", float(UNITS[up]))
            if self.peek()[0] == "op" and self.peek()[1] == "(":
                return self.call(up, pos)
            if up in FIELDS:
                self.fields.add(up)
                return ("field", up)
            hint = _suggest(up)
            what = "unit" if hint in UNITS else "field"
            raise ExprError(f"Unknown {what} {val!r}" + (f"; did you mean {hint}?" if hint else ""), pos)
        got = val if kind != "end" else "end of expression"
        raise ExprError(f"Expected a value, found {got}", pos)

    def call(self, name, pos):
        if name == "FNMATCH":
            return self.fnmatch_call(pos)
        self.take("op", "(")
        tok = self.peek()
        if tok[0] not in ("str", "quoted"):
            raise ExprError(f"{name}() takes a quoted name", tok[2])
        self.i += 1
        arg = tok[1]
        self.take("op", ")")
        if name in TIME_FUNCS:
            try:
                return ("const", parse_time(arg))
            except ValueError:
                raise ExprError(f"Use a date like '2026-01-31' or '2026-01-31 18:00:00'", tok[2])
        if name in TYPED_FUNCS:
            return ("const", Typed(name, arg))
        if name in META_FUNCS:
            key = meta_key(name, arg)
            self.metas.add(key)
            return ("meta", key)
        raise ExprError(f"Unknown function {name}()", pos)


TEXT_FIELDS = {f for f, (k, _) in FIELDS.items() if k == "text"}


def _fnmatch_call(self, pos):
    """FNMATCH('pattern', NAME): shell-style wildcards; '*' also matches '/' (no FNM_PATHNAME)."""
    self.take("op", "(")
    tok = self.peek()
    if tok[0] not in ("str", "quoted"):
        raise ExprError("FNMATCH() takes a quoted pattern first, like FNMATCH('*.log', NAME)", tok[2])
    self.i += 1
    pattern = tok[1]
    self.take("op", ",")
    ftok = self.peek()
    if ftok[0] != "ident" or ftok[1].upper() not in TEXT_FIELDS:
        raise ExprError("FNMATCH() matches NAME or PATH, like FNMATCH('*/scratch/*', PATH)", ftok[2])
    self.i += 1
    field = ftok[1].upper()
    self.fields.add(field)
    self.take("op", ")")
    return ("fnmatch", pattern, field)


_Parser.fnmatch_call = _fnmatch_call


def _suggest(word):
    import difflib
    plural_units = [u for u in UNITS if u.endswith("S")]  # suggest DAYS, MBYTES (the usual spelling)
    m = difflib.get_close_matches(word, list(FIELDS) + list(META_FUNCS) + plural_units, n=1, cutoff=0.75)
    return m[0] if m else None


class Condition:
    """A parsed condition. .fields and .metas are what the scan must gather."""

    def __init__(self, text):
        self.text = (text or "").strip()
        p = _Parser(self.text)
        self.ast = p.parse()
        self.fields = p.fields
        self.metas = p.metas
        self._check(self.ast)

    def _check(self, n):
        """Light type checks for mistakes that would otherwise silently match nothing."""
        if n[0] == "cmp":
            _, op, a, b = n
            for side, other in ((a, b), (b, a)):
                if side[0] == "field" and other[0] == "const":
                    kind = FIELDS[side[1]][0]
                    v = other[1]
                    if kind == "bool" and not isinstance(v, bool):
                        raise ExprError(f"{side[1]} is TRUE or FALSE")
                    if kind == "age" and isinstance(v, Time):
                        raise ExprError(f"{side[1]} is an age; compare it with a duration like 30*DAYS")
                    if kind in ("bytes", "age") and isinstance(v, str):
                        raise ExprError(f"{side[1]} is a number; compare it with a value like "
                                        + ("10*MBYTES" if kind == "bytes" else "30*DAYS"))
                    if kind == "time" and isinstance(v, (int, float)) and not isinstance(v, (bool, Time)) and v < 1e8:
                        raise ExprError(f"{side[1]} is a time; compare it with LOCAL_TIME('2026-01-31') or NOW-30*DAYS")
        for c in n[1:]:
            if isinstance(c, tuple):
                self._check(c)

    def evaluate(self, item: dict, now: float):
        return bool(_eval(self.ast, item, now))


def _eval(n, item, now):
    op = n[0]
    if op == "const":
        return n[1]
    if op == "now":
        return now
    if op == "field":
        return item.get(n[1])
    if op == "meta":
        return item.get(n[1])
    if op == "and":
        return bool(_eval(n[1], item, now)) and bool(_eval(n[2], item, now))
    if op == "or":
        return bool(_eval(n[1], item, now)) or bool(_eval(n[2], item, now))
    if op == "not":
        return not bool(_eval(n[1], item, now))
    if op == "fnmatch":
        v = item.get(n[2])
        return isinstance(v, str) and fnmatch.fnmatchcase(v, n[1])
    if op == "tern":
        if bool(_eval(n[1], item, now)):
            return _eval(n[2], item, now)
        return _eval(n[3], item, now) if n[3] is not None else None
    if op == "neg":
        v = _eval(n[1], item, now)
        return -v if isinstance(v, (int, float)) else None
    if op == "arith":
        a, b = _eval(n[2], item, now), _eval(n[3], item, now)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            return None
        try:
            return {"+": a + b, "-": a - b, "*": a * b}[n[1]] if n[1] != "/" else a / b
        except ZeroDivisionError:
            return None
    if op == "cmp":
        a, b = _eval(n[2], item, now), _eval(n[3], item, now)
        if a is None or b is None:
            return False  # unknown values never match
        if isinstance(a, str) != isinstance(b, str):
            return n[1] == "!="
        try:
            return {"==": a == b, "!=": a != b, "<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[n[1]]
        except TypeError:
            return False
    return None


def check(text: str) -> dict:
    """Validate a condition for the GUI: errors with a position, and the fields it uses."""
    try:
        c = Condition(text)
        return {"ok": True, "fields": sorted(c.fields), "metas": sorted(c.metas)}
    except ExprError as e:
        return {"ok": False, "error": str(e), "pos": e.pos}


def scan_expression(fields: list[str], metas: list[str]) -> str:
    """The tuple each file's metadata is gathered with: DPATH first so results identify themselves."""
    parts = ["DPATH"] + [f for f in fields if f != "DPATH"] + list(metas)
    return "{" + ",".join(parts) + "}"
