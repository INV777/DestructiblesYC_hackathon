"""Turn a raw bank export or invoice list into SettleBot lines. Offline: no network, no AI.

Rules read the file, find the table, work out which column is which, and clean the values. Nothing
is guessed silently: every choice is reported back (and shown in the console's preview), rows that
can't be read are listed with the reason, and a person confirms before anything runs.

    sheets = read_tables(data, "statement.xlsx")
    result = analyse(sheets, "statement.xlsx")            # automatic choices
    result = analyse(sheets, "statement.xlsx", {"day_first": False, "mapping": {...}})  # person's edits
    result.lines  -> SettleBot's input format, each line tagged with its source row
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import re
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from typing import Any

MAX_ROWS = 20_000

# ---- the fields SettleBot needs, and what files tend to call them -------------------------------
FIELDS: dict[str, list[str]] = {
    "bank_line": ["date", "description", "debit", "credit", "amount", "drcr", "balance", "reference"],
    "invoice": ["invoice_no", "vendor_id", "vendor_name", "date", "amount", "sender_email",
                "account_number", "ifsc", "account_name"],
}
LABELS = {
    "date": "Date", "description": "Description", "debit": "Money out (withdrawals)",
    "credit": "Money in (deposits)", "amount": "Amount", "drcr": "Debit/credit marker", "balance": "Balance",
    "reference": "Reference", "invoice_no": "Invoice number", "vendor_id": "Vendor ID",
    "vendor_name": "Vendor name", "sender_email": "Sender email", "account_number": "Pay-to account number",
    "ifsc": "Pay-to IFSC", "account_name": "Pay-to account name",
}
SYNONYMS: dict[str, list[str]] = {
    "date": ["txn date", "transaction date", "tran date", "trans date", "date", "posting date", "booking date",
             "invoice date", "inv date", "bill date", "document date", "value date", "value dt"],
    "description": ["narration", "description", "particulars", "transaction remarks", "remarks", "details",
                    "transaction details", "transaction description", "transaction particulars", "merchant",
                    "beneficiary", "payee"],
    "debit": ["withdrawal amt", "withdrawal amount", "withdrawal", "withdrawals", "debit", "debit amount", "dr",
              "debits", "dr amount", "dr amt", "amount debited", "paid out", "money out", "debit amt"],
    "credit": ["deposit amt", "deposit amount", "deposit", "deposits", "credit", "credit amount", "credits", "cr",
               "cr amount", "cr amt", "amount credited", "paid in", "money in", "credit amt"],
    "amount": ["amount", "transaction amount", "txn amount", "amt", "value", "invoice amount", "bill amount",
               "net amount", "amount payable", "total amount", "invoice value", "grand total", "total"],
    "drcr": ["dr cr", "cr dr", "drcr", "debit credit", "dr or cr", "type", "transaction type", "txn type"],
    "balance": ["balance", "closing balance", "running balance", "available balance", "bal", "balance amt",
                "balance amount"],
    "reference": ["chq ref no", "chq no", "chqno", "cheque no", "cheque number", "ref no", "reference", "reference no",
                  "ref", "utr", "utr no", "transaction id", "txn id"],
    "invoice_no": ["invoice no", "invoice number", "inv no", "invoice", "bill no", "bill number", "invoice id",
                   "document no", "voucher no", "inv number"],
    "vendor_id": ["vendor id", "vendor code", "vendor no", "supplier id", "supplier code", "party code",
                  "creditor code", "vendor"],
    "vendor_name": ["vendor name", "supplier name", "supplier", "party name", "party", "creditor", "payee name",
                    "company", "vendor"],
    "sender_email": ["sender email", "email", "e mail", "email id", "from", "sender"],
    "account_number": ["bank account number", "bank account", "account number", "account no", "a c no", "acc no",
                       "bank a c", "beneficiary account", "account"],
    "ifsc": ["ifsc", "ifsc code", "ifsc no"],
    "account_name": ["account name", "beneficiary name", "account holder", "name on account", "account holder name"],
}
SUMMARY_ROW = re.compile(r"^\s*(opening|closing)\s+balance|^\s*(grand\s+)?total\b|^\s*balance\s+(b/?f|c/?f|brought|carried)"
                         r"|^\s*(b/?f|c/?f)\b|^\s*statement\s+summary|^\s*\**\s*end\s+of\s+statement", re.I)


def norm_header(h: Any) -> str:
    s = str(h or "").lower().replace("₹", " ").replace("a/c", "a c")
    s = re.sub(r"\((inr|rs\.?|in rs\.?|in inr|₹)\)|\brs\.?\b|\binr\b", " ", s)
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


# ---- reading files -------------------------------------------------------------------------------
@dataclass
class Sheet:
    name: str
    rows: list[list[Any]]


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._cell is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self.tables:
            self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def decode_text(data: bytes) -> str:
    for bom, enc in ((b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if data.startswith(bom):
            return data.decode(enc, errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    try:
        from charset_normalizer import from_bytes  # installed with requests
        best = from_bytes(data[:200_000]).best()
        if best is not None:
            return data.decode(best.encoding, errors="replace")
    except Exception:  # pragma: no cover - detection is best effort
        pass
    return data.decode("cp1252", errors="replace")


def _sniff_delimiter(text: str) -> str:
    lines = [ln for ln in text.splitlines()[:80] if ln.strip()]
    best, best_score = ",", -1.0
    for d in (",", ";", "\t", "|"):
        counts = [len(next(csv.reader([ln], delimiter=d))) for ln in lines]
        wide = [c for c in counts if c > 1]
        if not wide:
            continue
        mode = max(set(wide), key=wide.count)
        score = wide.count(mode) * mode
        if score > best_score:
            best, best_score = d, score
    return best


def _trim(row: list[Any]) -> list[Any]:
    row = ["" if v is None else v for v in row]
    while row and (row[-1] == "" or (isinstance(row[-1], str) and not row[-1].strip())):
        row.pop()
    return row


def read_tables(data: bytes, filename: str = "") -> list[Sheet]:
    """Every table in the file, by content rather than extension (bank '.xls' files are often HTML or text)."""
    sheets: list[Sheet] = []
    if data[:2] == b"PK":  # .xlsx / .xlsm
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:
            rows = []
            for r in ws.iter_rows(values_only=True):
                rows.append(_trim(list(r)))
                if len(rows) >= MAX_ROWS:
                    break
            sheets.append(Sheet(ws.title, rows))
        wb.close()
    elif data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":  # legacy .xls
        import xlrd
        book = xlrd.open_workbook(file_contents=data)
        for sh in book.sheets():
            rows = []
            for i in range(min(sh.nrows, MAX_ROWS)):
                out = []
                for c in sh.row(i):
                    if c.ctype == xlrd.XL_CELL_DATE:
                        out.append(xlrd.xldate_as_datetime(c.value, book.datemode))
                    elif c.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                        out.append("")
                    else:
                        out.append(c.value)
                rows.append(_trim(out))
            sheets.append(Sheet(sh.name, rows))
    else:
        text = decode_text(data)
        if re.search(r"<table", text[:50_000], re.I):
            p = _TableParser()
            p.feed(text)
            sheets = [Sheet(f"Table {i + 1}", [_trim(r) for r in t[:MAX_ROWS]]) for i, t in enumerate(p.tables)]
        else:
            delim = _sniff_delimiter(text)
            rows = [_trim(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
            sheets = [Sheet(filename or "File", rows[:MAX_ROWS])]
    return [s for s in sheets if any(any(str(v).strip() for v in r) for r in s.rows)]


# ---- values ------------------------------------------------------------------------------------
_MONTHS = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                            "nov", "dec"])}
_NUMERIC_DATE = re.compile(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}|\d{4})$")


def _year(y: str) -> int:
    n = int(y)
    return n + 2000 if len(y) == 2 else n


def _iso(y: int, m: int, d: int) -> str | None:
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def parse_date(v: Any, day_first: bool = True) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 20000 <= v <= 80000:  # an Excel day number that arrived without its date format
            return (dt.date(1899, 12, 30) + dt.timedelta(days=int(v))).isoformat()
        return None
    s = re.sub(r"[ T]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\s*(am|pm)?$", "", str(v).strip(), flags=re.I).strip()
    if m := re.fullmatch(r"(\d{4})[/.\-](\d{1,2})[/.\-](\d{1,2})", s):
        return _iso(int(m[1]), int(m[2]), int(m[3]))
    if m := _NUMERIC_DATE.fullmatch(s):
        a, b, y = int(m[1]), int(m[2]), _year(m[3])
        return _iso(y, b, a) if day_first else _iso(y, a, b)
    if m := re.fullmatch(r"(\d{1,2})[\s\-/.]*([A-Za-z]{3,9})[\s\-/.,]*(\d{2}|\d{4})", s):
        mon = _MONTHS.get(m[2][:3].lower())
        return _iso(_year(m[3]), mon, int(m[1])) if mon else None
    if m := re.fullmatch(r"([A-Za-z]{3,9})[\s\-.]*(\d{1,2}),?[\s\-]*(\d{4})", s):
        mon = _MONTHS.get(m[1][:3].lower())
        return _iso(int(m[3]), mon, int(m[2])) if mon else None
    if m := re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s):
        return _iso(int(m[1]), int(m[2]), int(m[3]))
    return None


def date_order(values: list[Any]) -> tuple[bool, bool]:
    """(day_first, certain). Dates like 25/10 prove day first; 10/25 proves month first."""
    day, month = False, False
    for v in values:
        m = _NUMERIC_DATE.fullmatch(str(v).strip().split(" ")[0]) if isinstance(v, str) else None
        if m:
            day |= int(m[1]) > 12
            month |= int(m[2]) > 12
    if day and not month:
        return True, True
    if month and not day:
        return False, True
    has_numeric = any(isinstance(v, str) and _NUMERIC_DATE.fullmatch(v.strip().split(" ")[0]) for v in values)
    return True, not has_numeric  # Indian files default to day first; unclear only if such dates exist


def parse_amount(v: Any) -> tuple[float | None, str | None]:
    """(value, "DR"/"CR" marker if the cell carried one). Handles ₹/Rs/INR, Indian digit grouping,
    (brackets) and trailing minus for negatives, and '500.00 Dr' / 'Cr' suffixes."""
    if v is None or isinstance(v, bool):
        return None, None
    if isinstance(v, (int, float)):
        return float(v), None
    s = str(v).strip().upper().replace(" ", " ")
    if not s or s in {"-", "--", "—", "NA", "N/A", "NIL"}:
        return None, None
    marker = None
    if m := re.search(r"\s*\b(DR|CR)\.?$", s):
        marker, s = m[1], s[:m.start()].strip()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = re.sub(r"₹|\bRS\.?|\bINR\b", "", s).replace(",", "").replace(" ", "")
    if s.endswith("-"):
        neg, s = not neg, s[:-1]
    if s.startswith("-"):
        neg, s = not neg, s[1:]
    s = s.lstrip("+")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        return None, None
    val = float(s)
    return (-val if neg else val), marker


def _text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()[:10]
    return re.sub(r"\s+", " ", str(v)).strip()


# ---- finding the table and the columns -----------------------------------------------------------
def _field_scores(header: Any) -> dict[str, int]:
    h = norm_header(header)
    if not h:
        return {}
    out = {}
    for f, syns in SYNONYMS.items():
        best = 0
        for rank, syn in enumerate(syns):
            if h == syn:
                best = max(best, 100 - rank)
            elif re.search(rf"\b{re.escape(syn)}\b", h) and len(syn) > 2:
                best = max(best, 50 - rank)
        if best:
            out[f] = best
    return out


def find_header(rows: list[list[Any]]) -> int | None:
    """Index of the header row (0-based), or None if the table has no header."""
    best, best_score = None, 1
    for i, row in enumerate(rows[:60]):
        matched = {f for cell in row for f in _field_scores(cell)}
        cells = [c for c in row if _text(c)]
        if len(matched) < 2 or len(cells) < 2:
            continue
        below = rows[i + 1:i + 8]
        data_like = sum(1 for r in below if any(parse_date(c) for c in r) and any(parse_amount(c)[0] is not None
                                                                                 for c in r if not parse_date(c)))
        score = len(matched) * 2 + data_like
        if score > best_score:
            best, best_score = i, score
    return best


def _col_letter(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _share(vals: list[Any], test) -> float:
    vals = [v for v in vals if _text(v)]
    return sum(1 for v in vals if test(v)) / len(vals) if vals else 0.0


def _balance_fits(bal: list[float | None], out: list[float | None], inn: list[float | None]) -> bool:
    """True when the balance moves by exactly money in minus money out, in either row order."""
    fwd = back = n = 0
    for i in range(1, len(bal)):
        if bal[i] is None or bal[i - 1] is None:
            continue
        n += 1
        o, c = out[i] or 0.0, inn[i] or 0.0
        o0, c0 = out[i - 1] or 0.0, inn[i - 1] or 0.0
        fwd += abs(bal[i - 1] - o + c - bal[i]) < 0.011
        back += abs(bal[i] - o0 + c0 - bal[i - 1]) < 0.011
    return n >= 2 and max(fwd, back) >= 0.8 * n


def _assign(headers: list[Any], kind: str) -> dict[str, int]:
    """Header names to fields: each field and each column used once, strongest match first. A column is
    only offered to fields that fit it about as well as its best field ("Value Dt" is a date, not an amount)."""
    cands = []
    for i, h in enumerate(headers):
        scores = {f: sc for f, sc in _field_scores(h).items() if f in FIELDS[kind]}
        if scores:
            top = max(scores.values())
            cands += [(sc, f, i) for f, sc in scores.items() if sc >= top - 10]
    used_cols, mapping = set(), {}
    for score, f, i in sorted(cands, key=lambda t: (-t[0], t[2])):
        if f not in mapping and i not in used_cols:
            mapping[f] = i
            used_cols.add(i)
    return mapping


@dataclass
class ImportResult:
    filename: str
    sheets: list[dict[str, Any]]
    sheet: str
    kind: str
    header_row: int | None              # 1-based row number of the header, as a spreadsheet shows it
    columns: list[dict[str, Any]]       # [{index, letter, header, sample}]
    mapping: dict[str, int | None]
    amount_mode: str | None             # bank lines: debit_column | drcr | signed | unsigned
    day_first: bool
    day_first_certain: bool
    lines: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    money_in: int = 0
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)  # a run cannot start until these are fixed

    def to_dict(self, preview: int = 12) -> dict[str, Any]:
        d = asdict(self)
        d["line_count"] = len(self.lines)
        d["lines"] = self.lines[:preview]
        reasons: dict[str, int] = {}
        for s in self.skipped:
            reasons[s["reason"]] = reasons.get(s["reason"], 0) + 1
        d["skipped_by_reason"] = reasons
        d["skipped"] = self.skipped[:15]
        d["fields"] = [{"field": f, "label": LABELS[f]} for f in FIELDS[self.kind]]
        return d


def analyse(sheets: list[Sheet], filename: str, choices: dict[str, Any] | None = None,
            known_vendors: dict[str, str] | None = None) -> ImportResult:
    """Pick (or apply the person's choice of) sheet, header row, kind, column mapping and date order,
    then convert. `known_vendors` maps a vendor's core name to its ID, for invoice lists without IDs."""
    if not sheets:
        raise ValueError("No table found in this file.")
    choices = choices or {}
    notes: list[str] = []

    # sheet: the person's choice, else the one whose header looks most like a transaction table
    by_name = {s.name: s for s in sheets}
    if choices.get("sheet") in by_name:
        sheet = by_name[choices["sheet"]]
    else:
        def sheet_score(s: Sheet) -> tuple[int, int]:
            h = find_header(s.rows)
            matched = len({f for c in s.rows[h] for f in _field_scores(c)}) if h is not None else 0
            return matched, len(s.rows)
        sheet = max(sheets, key=sheet_score)
        if len(sheets) > 1:
            notes.append(f"The file has {len(sheets)} sheets; '{sheet.name}' looks like the transaction table.")
    rows = sheet.rows

    # header row
    if choices.get("header_row") is not None:
        h = int(choices["header_row"]) - 1 if int(choices["header_row"]) > 0 else None
        if h is not None and not 0 <= h < len(rows):
            raise ValueError(f"Row {choices['header_row']} is outside the sheet (it has {len(rows)} rows).")
    else:
        h = find_header(rows)
    width = max((len(r) for r in rows), default=0)
    headers = [(rows[h][i] if h is not None and i < len(rows[h]) else "") for i in range(width)]
    body_start = (h + 1) if h is not None else 0
    body = rows[body_start:]
    columns = []
    for i in range(width):
        sample = next((_text(r[i]) for r in body if i < len(r) and _text(r[i])), "")
        columns.append({"index": i, "letter": _col_letter(i), "header": _text(headers[i]) or f"Column {_col_letter(i)}",
                        "sample": sample[:40]})
    col = lambda i: [r[i] if i < len(r) else "" for r in body]  # noqa: E731

    # kind
    kind = choices.get("kind")
    if kind not in FIELDS:
        inv = _assign(headers, "invoice")
        kind = "invoice" if ("invoice_no" in inv and ("vendor_id" in inv or "vendor_name" in inv)) else "bank_line"

    # column mapping: the person's choice, else headers, then the data itself
    if isinstance(choices.get("mapping"), dict):
        mapping = {f: (int(v) if v is not None and str(v) != "" else None) for f, v in choices["mapping"].items()
                   if f in FIELDS[kind]}
    else:
        mapping = _assign(headers, kind)
        date_cols = [i for i in range(width) if i not in mapping.values() and _share(col(i), parse_date) >= 0.6]
        if "date" not in mapping and date_cols:
            mapping["date"] = date_cols[0]
            notes.append(f"No date heading found; column {_col_letter(date_cols[0])} holds dates.")
        if kind == "bank_line":
            _infer_bank_columns(mapping, col, width, notes)
        if "description" not in mapping and kind == "bank_line":
            text_cols = [i for i in range(width) if i not in mapping.values()
                         and _share(col(i), lambda v: parse_amount(v)[0] is None and not parse_date(v)) >= 0.6]
            if text_cols:
                best = max(text_cols, key=lambda i: sum(len(_text(v)) for v in col(i)))
                mapping["description"] = best
                notes.append(f"No description heading found; column {_col_letter(best)} has the longest text.")
    for f in FIELDS[kind]:
        mapping.setdefault(f, None)

    # how money out is shown (bank lines)
    amount_mode = None
    if kind == "bank_line":
        if mapping.get("debit") is not None:
            amount_mode = "debit_column"
        elif mapping.get("amount") is not None and mapping.get("drcr") is not None:
            amount_mode = "drcr"
        elif mapping.get("amount") is not None:
            vals = [parse_amount(v) for v in col(mapping["amount"])]
            if any(m for _, m in vals):
                amount_mode = "drcr"
            elif any(v is not None and v < 0 for v, _ in vals):
                amount_mode = "signed"
            else:
                amount_mode = "unsigned"
        if choices.get("amount_mode") in ("debit_column", "drcr", "signed", "unsigned"):
            amount_mode = choices["amount_mode"]

    # date order
    if choices.get("day_first") is not None:
        day_first, certain = bool(choices["day_first"]), True
    else:
        day_first, certain = date_order(col(mapping["date"])) if mapping.get("date") is not None else (True, True)

    res = ImportResult(filename=filename, sheets=[{"name": s.name, "rows": len(s.rows)} for s in sheets],
                       sheet=sheet.name, kind=kind, header_row=(h + 1) if h is not None else None, columns=columns,
                       mapping=mapping, amount_mode=amount_mode, day_first=day_first, day_first_certain=certain,
                       notes=notes)
    _convert(res, body, body_start, known_vendors or {})
    return res


def _infer_bank_columns(mapping: dict[str, int], col, width: int, notes: list[str]) -> None:
    """Fill debit / credit / balance from the numbers when headings didn't settle them."""
    nums = {i: [parse_amount(v)[0] for v in col(i)] for i in range(width)
            if i not in (mapping.get("date"), mapping.get("description"))
            and _share(col(i), lambda v: parse_amount(v)[0] is not None) >= 0.6}
    if mapping.get("balance") is None and mapping.get("debit") is not None:
        for b, vals in nums.items():
            if b not in mapping.values() and _balance_fits(vals, nums.get(mapping["debit"], []),
                                                         nums.get(mapping.get("credit"), [None] * len(vals))):
                mapping["balance"] = b
                break
    if mapping.get("debit") is None and mapping.get("amount") is None:
        free = [i for i in nums if i not in mapping.values()]
        bals = [mapping["balance"]] if mapping.get("balance") in nums else free
        for b in bals:  # one signed column: the balance moves by exactly that amount
            for s in free:
                if s != b and any(v is not None and v < 0 for v in nums[s]) and \
                        _balance_fits(nums[b], [None] * len(nums[b]), nums[s]):
                    mapping.update({"balance": b, "amount": s})
                    notes.append("Amounts and balance were found from the numbers: the balance moves by exactly "
                                 "each amount, and negative amounts are money going out.")
                    return
        for b in bals:
            for o in free:
                for c in free + [None]:
                    if len({b, o, c}) < (3 if c is not None else 2):
                        continue
                    inn = nums[c] if c is not None else [None] * len(nums[b])
                    if _balance_fits(nums[b], nums[o], inn):
                        mapping.update({"balance": b, "debit": o})
                        if c is not None:
                            mapping["credit"] = c
                        notes.append("Money out, money in and balance were found from the numbers: the balance "
                                     "moves by exactly those amounts.")
                        return


def _convert(res: ImportResult, body: list[list[Any]], body_start: int, known_vendors: dict[str, str]) -> None:
    m, kind = res.mapping, res.kind
    get = lambda r, f: (r[m[f]] if m.get(f) is not None and m[f] < len(r) else "")  # noqa: E731
    need = ["date", "description"] if kind == "bank_line" else ["invoice_no", "amount"]
    if kind == "bank_line" and m.get("debit") is None and m.get("amount") is None:
        res.problems.append("Choose the column with the money going out (withdrawals or amount).")
    if kind == "invoice" and m.get("vendor_id") is None and m.get("vendor_name") is None:
        res.problems.append("Choose the column with the vendor ID or vendor name.")
    for f in need:
        if m.get(f) is None:
            res.problems.append(f"Choose the column for {LABELS[f].lower()}.")
    if res.problems:
        return
    if kind == "bank_line" and res.amount_mode == "unsigned":
        res.notes.append("Every amount is read as money going out, because the file has no debit/credit marker "
                         "and no negative amounts. Change this if the column also holds deposits.")
    if not res.day_first_certain:
        res.notes.append("The dates could be day-first or month-first (no day above 12). Day first is assumed; "
                         "check the dates below.")

    for k, r in enumerate(body):
        row_no = body_start + k + 1
        cells = [_text(c) for c in r]
        if not any(cells):
            continue
        text = " ".join(c for c in cells if c)
        date_raw, desc = get(r, "date"), _text(get(r, "description"))
        if SUMMARY_ROW.search(desc or text):
            res.skipped.append({"row": row_no, "reason": "Opening, closing or total row", "text": text[:90]})
            continue
        date = parse_date(date_raw, res.day_first)
        if not date:
            reason = "No date in this row" if not _text(date_raw) else f"Date not readable ({_text(date_raw)[:20]})"
            res.skipped.append({"row": row_no, "reason": reason, "text": text[:90]})
            continue
        src = {"file": res.filename, "sheet": res.sheet, "row": row_no}
        if kind == "bank_line":
            out = _money_out(res, r, get)
            if out == "in":
                res.money_in += 1
                continue
            if out is None or out <= 0:
                res.skipped.append({"row": row_no, "reason": "No amount going out", "text": text[:90]})
                continue
            if not desc:
                res.skipped.append({"row": row_no, "reason": "No description", "text": text[:90]})
                continue
            line = {"date": date, "description": desc, "amount_inr": round(out, 2), "source": src}
            if _text(get(r, "reference")):
                line["reference"] = _text(get(r, "reference"))
        else:
            amount, _ = parse_amount(get(r, "amount"))
            inv = _text(get(r, "invoice_no"))
            if not inv or amount is None or amount <= 0:
                res.skipped.append({"row": row_no, "reason": "No invoice number or amount", "text": text[:90]})
                continue
            vendor_id, name = _text(get(r, "vendor_id")) or None, _text(get(r, "vendor_name"))
            if not vendor_id and name and known_vendors:
                from .lookalike import core_name
                vendor_id = known_vendors.get(core_name(name))
                if vendor_id:
                    src["vendor_id_from_name"] = True
            line = {"kind": "invoice", "invoice_no": inv, "vendor_id": vendor_id, "date": date,
                    "description": name or vendor_id or "", "amount_inr": round(amount, 2), "source": src}
            if _text(get(r, "sender_email")):
                line["sender_email"] = _text(get(r, "sender_email"))
            acct = get(r, "account_number")
            if _text(acct):
                if isinstance(acct, float) and acct >= 1e15:
                    res.notes.append(f"Row {row_no}: the account number has more than 15 digits and was stored as a "
                                     "number, so Excel may have changed its last digits. Save that column as text.")
                line["bank_account"] = {"account_number": re.sub(r"\s", "", _text(acct)),
                                        "ifsc": _text(get(r, "ifsc")).upper() or None,
                                        "name": _text(get(r, "account_name")) or None}
        res.lines.append(line)
        if len(res.lines) >= MAX_ROWS:
            break
    if res.money_in:
        res.notes.append(f"{_n(res.money_in, 'incoming payment was', 'incoming payments were')} left out: "
                         "SettleBot checks money going out.")
    filled = sum(1 for ln in res.lines if ln.get("source", {}).get("vendor_id_from_name"))
    if filled:
        res.notes.append(f"{_n(filled, 'invoice had', 'invoices had')} no vendor ID; it was filled in from the "
                         "vendor master because the vendor name matched exactly.")
    if not res.lines and not res.problems:
        res.problems.append("No lines could be read with these choices. Check the header row and the columns.")


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _money_out(res: ImportResult, r: list[Any], get) -> float | str | None:
    """A positive amount going out, "in" for money coming in, or None."""
    mode = res.amount_mode
    if mode == "debit_column":
        out, _ = parse_amount(get(r, "debit"))
        if out:
            return abs(out)
        inn, _ = parse_amount(get(r, "credit"))
        return "in" if inn else None
    val, marker = parse_amount(get(r, "amount"))
    if val is None:
        return None
    if mode == "drcr":
        flag = _text(get(r, "drcr")).upper() or (marker or "")
        if flag.startswith(("C", "DEP")):          # Cr, Credit, C, Deposit
            return "in"
        if flag.startswith(("D", "W")):             # Dr, Debit, D, Withdrawal
            return abs(val)
        return None
    if mode == "signed":
        return abs(val) if val < 0 else "in"
    return abs(val)  # unsigned: every amount is money out
