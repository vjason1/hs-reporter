"""Parse the text form of HammerScript values, as printed by `hs eval` (without -j).

Examples of what Hammerspace prints:

    ITEM_TABLE{ |NAME = "file.txt", |SIZE = 0 BYTES, |OWNER = USER('root@localdomain'), ... }
    LOCAL_TIME('2026-10-06 14:58:46')     33.4566 SECONDS     (56 YEARS+292 DAYS)     00:05:00
    TRUE   FALSE   #EMPTY   100%   0640   0 OPERATIONS / SECOND
    INSTANCES_TABLE{ |VOLUME = STORAGE_VOLUME('dsx-1::/hsvol0'), ...; |VOLUME = ... }   (rows split by ';')
    {"./a.txt", 12 BYTES, TRUE}           (a tuple, e.g. from -e '{DPATH,SIZE,IS_FILE}')

Values become plain Python: bool, None, int/float (bytes, seconds, counts), str, Typed, Time,
dict (one-row table), list (multi-row table or tuple).
"""
import datetime as _dt
import re

SIZE_UNITS = {"BYTE": 1, "BYTES": 1, "KBYTE": 10**3, "KBYTES": 10**3, "MBYTE": 10**6, "MBYTES": 10**6,
              "GBYTE": 10**9, "GBYTES": 10**9, "TBYTE": 10**12, "TBYTES": 10**12,
              "PBYTE": 10**15, "PBYTES": 10**15, "EBYTE": 10**18, "EBYTES": 10**18}
TIME_UNITS = {"NANOSECOND": 1e-9, "NANOSECONDS": 1e-9, "MICROSECOND": 1e-6, "MICROSECONDS": 1e-6,
              "MILLISECOND": 1e-3, "MILLISECONDS": 1e-3, "SECOND": 1, "SECONDS": 1, "MINUTE": 60,
              "MINUTES": 60, "HOUR": 3600, "HOURS": 3600, "DAY": 86400, "DAYS": 86400,
              "WEEK": 604800, "WEEKS": 604800, "MONTH": 30 * 86400, "MONTHS": 30 * 86400,
              "YEAR": 365 * 86400, "YEARS": 365 * 86400}
COUNT_UNITS = {"FILE", "FILES", "OPERATIONS", "OPERATION", "IOPS"}


class Typed(str):
    """A typed literal such as USER('root@localdomain'): compares as its text value."""
    def __new__(cls, kind, value):
        s = super().__new__(cls, value)
        s.kind = kind
        return s

    def __repr__(self):
        return f"{self.kind}({str.__repr__(self)})"


class Time(float):
    """Seconds since the epoch, from LOCAL_TIME('YYYY-MM-DD HH:MM:SS') (cluster local time)."""
    def __repr__(self):
        return f"Time({_dt.datetime.fromtimestamp(self).isoformat(sep=' ')})"


def parse_time(text: str) -> Time:
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            # Naive and treated as UTC so comparisons between cluster times are consistent.
            return Time(_dt.datetime.strptime(text, fmt).replace(tzinfo=_dt.timezone.utc).timestamp())
        except ValueError:
            continue
    raise ValueError(f"Unrecognized time {text!r}")


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<str>"(?:[^"\\]|\\.)*")
  | (?P<quoted>'(?:[^'\\]|\\.)*')
  | (?P<clock>\d{1,3}:\d{2}:\d{2}(?:\.\d+)?)
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)
  | (?P<empty>\#EMPTY)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<punct>[{}()\[\],;|=%/+*:-])
""", re.X)


def tokenize(text: str):
    pos, out = 0, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m:
            raise ValueError(f"Unexpected character {text[pos]!r} at {pos}")
        kind = m.lastgroup
        if kind != "ws":
            out.append((kind, m.group(kind), m.start()))
        pos = m.end()
    return out


class _P:
    def __init__(self, text):
        self.toks = tokenize(text)
        self.i = 0

    def peek(self, k=0):
        j = self.i + k
        return self.toks[j] if j < len(self.toks) else (None, None, None)

    def take(self, value=None):
        t = self.peek()
        if value is not None and t[1] != value:
            raise ValueError(f"Expected {value!r}, found {t[1]!r}")
        self.i += 1
        return t

    def at_end(self):
        return self.i >= len(self.toks)

    # value := sum
    def value(self):
        v = self.atom()
        # (56 YEARS+292 DAYS) style sums inside parentheses are handled in atom()
        return v

    def atom(self):
        kind, tok, _ = self.peek()
        if kind is None:
            raise ValueError("Unexpected end of value")
        if kind == "str":
            self.take()
            return bytes(tok[1:-1], "utf-8").decode("unicode_escape") if "\\" in tok else tok[1:-1]
        if kind == "empty":
            self.take()
            return None
        if kind == "clock":
            self.take()
            parts = [float(x) for x in tok.split(":")]
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        if kind == "num":
            return self.quantity()
        if tok == "-" and self.peek(1)[0] == "num":
            self.take()
            v = self.quantity()
            return -v if isinstance(v, (int, float)) else v
        if tok == "(":
            self.take()
            total = self.atom()
            while self.peek()[1] in ("+", "-"):
                op = self.take()[1]
                rhs = self.atom()
                total = total + rhs if op == "+" else total - rhs
            self.take(")")
            return total
        if tok == "{":
            return self.braces(None)
        if kind == "ident":
            self.take()
            up = tok.upper()
            if up in ("TRUE", "FALSE"):
                return up == "TRUE"
            nxt = self.peek()[1]
            if nxt == "{":
                return self.braces(up)
            if nxt == "(":
                return self.call(up)
            return Typed("IDENT", tok)
        raise ValueError(f"Unexpected {tok!r}")

    def quantity(self):
        _, tok, _ = self.take()
        num = float(tok)
        if re.fullmatch(r"0\d+", tok):  # mode bits like 0640
            return tok
        if num.is_integer() and "." not in tok and "e" not in tok.lower():
            num = int(tok)
        kind, nxt, _ = self.peek()
        if nxt == "%":
            self.take()
            return num / 100.0
        if kind == "ident":
            unit = nxt.upper()
            if unit in SIZE_UNITS:
                self.take()
                return int(round(num * SIZE_UNITS[unit]))
            if unit in TIME_UNITS:
                self.take()
                return num * TIME_UNITS[unit]
            if unit in COUNT_UNITS:
                self.take()
                if self.peek()[1] == "/":  # "0 OPERATIONS / SECOND"
                    self.take()
                    if self.peek()[0] == "ident":
                        self.take()
                return num
        return num

    def call(self, name):
        self.take("(")
        if self.peek()[0] == "quoted":
            arg = self.take()[1][1:-1]
            self.take(")")
            if name in ("LOCAL_TIME", "TIME", "UTC_TIME"):
                return parse_time(arg)
            return Typed(name, arg)
        # TEMPERATURE(|ACTIVITY=...) and other structured calls: read as a table body
        body = self.table_body(")")
        self.take(")")
        return {"__call__": name, **(body[0] if body else {})}

    def braces(self, name):
        self.take("{")
        if self.peek()[1] == "}":
            self.take()
            return {} if name else []
        if self.peek()[1] == "|":
            rows = self.table_body("}")
            self.take("}")
            return rows[0] if len(rows) == 1 else rows
        # tuple / list form: a, b; c, d
        rows, row = [], [self.atom()]
        while self.peek()[1] in (",", ";"):
            sep = self.take()[1]
            if sep == ";":
                rows.append(row)
                row = []
            if self.peek()[1] == "}":
                break
            row.append(self.atom())
        rows.append(row)
        self.take("}")
        return rows[0] if len(rows) == 1 else rows

    def table_body(self, closer):
        rows, row = [], {}
        while self.peek()[1] not in (closer, None):
            self.take("|")
            key = self.take()[1]
            self.take("=")
            row[key.upper()] = self.atom()
            sep = self.peek()[1]
            if sep == ",":
                self.take()
            elif sep == ";":
                self.take()
                rows.append(row)
                row = {}
        rows.append(row)
        return rows


def parse(text: str):
    """Parse a single HammerScript value."""
    p = _P(text)
    v = p.atom()
    if not p.at_end():
        raise ValueError(f"Unexpected trailing text at {p.peek()[2]}")
    return v


def parse_stream(text: str):
    """Parse the output of one or more hs eval calls: '##### path' headers (hstk prints these
    when given several paths) followed by values. Yields (header_path_or_None, value)."""
    header = None
    buf = []

    def flush():
        chunk = "\n".join(buf).strip()
        buf.clear()
        if not chunk:
            return []
        out = []
        p = _P(chunk)
        while not p.at_end():
            out.append(p.atom())
        return out

    for line in text.splitlines():
        if line.startswith("##### "):
            for v in flush():
                yield header, v
            header = line[6:].strip()
        else:
            buf.append(line)
    for v in flush():
        yield header, v
