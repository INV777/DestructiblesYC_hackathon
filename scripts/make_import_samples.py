"""Rebuild the invented sample exports in tests/fixtures/imports.

  python -m pip install xlwt openpyxl        # xlwt writes old .xls files; it is not a runtime dependency
  python scripts/make_import_samples.py tests/fixtures/imports

All names, accounts and amounts are invented. The layouts imitate common Indian bank exports.
"""
import datetime as dt, sys
from pathlib import Path
import openpyxl, xlwt
from openpyxl.styles import Font

OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

# Same money-out lines everywhere: three companies with recorded Crustdata responses, a duplicate, two unknowns.
TX = [  # (day, description, out, in)
    (3, "RAZ*SWIGGY BLR", 1240.00, None),
    (5, "NEFT CR-EXAMPLE TRADERS-SALARY OCT", None, 50000.00),
    (7, "AWS EMEA", 18500.00, None),
    (7, "AWS EMEA", 18500.00, None),
    (9, "PAYTM*SHARMA TRD", 4000.00, None),
    (14, "IMPS-REFUND-ORDER 4471", None, 620.00),
    (22, "POS CAFÉ COFFEE DAY PUNE", 860.50, None),
]

def balances(start=125000.00):
    b, out = start, []
    for d, desc, o, i in TX:
        b = round(b - (o or 0) + (i or 0), 2)
        out.append(b)
    return out

BAL = balances()

def inr(x):  # Indian digit grouping: 1,25,000.00
    s = f"{x:.2f}"; whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = ",".join([head[max(0, i - 2):i] for i in range(len(head), 0, -2)][::-1])
        whole = f"{head},{tail}"
    return f"{whole}.{frac}"

# 1. Legacy .xls: account details on top, Withdrawal / Deposit / Closing Balance columns, totals at the end
wb = xlwt.Workbook(); ws = wb.add_sheet("Statement")
pre = ["EXAMPLE BANK LTD", "Account holder: A. N. EXAMPLE LOGISTICS PVT LTD", "Account No: XXXXXXXX1234",
       "Statement period: 01/10/2026 to 31/10/2026", ""]
for r, t in enumerate(pre):
    ws.write(r, 0, t)
hdr = ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]
for c, t in enumerate(hdr):
    ws.write(6, c, t)
ws.write(7, 1, "OPENING BALANCE"); ws.write(7, 6, 125000.00)
for k, ((d, desc, o, i), b) in enumerate(zip(TX, BAL)):
    r = 8 + k
    ws.write(r, 0, f"{d:02d}/10/26"); ws.write(r, 1, desc); ws.write(r, 2, f"000{4410 + k}")
    ws.write(r, 3, f"{d:02d}/10/26")
    if o is not None: ws.write(r, 4, o)
    if i is not None: ws.write(r, 5, i)
    ws.write(r, 6, b)
r = 8 + len(TX) + 1
ws.write(r, 0, "STATEMENT SUMMARY"); ws.write(r + 1, 1, "Total"); ws.write(r + 1, 4, 43100.50); ws.write(r + 1, 5, 50620.00)
ws.write(r + 3, 0, "*** End of statement ***")
wb.save(str(OUT / "statement_withdrawal_deposit.xls"))

# 2. Semicolon CSV in Windows-1252, one Amount column with a Dr/Cr column, dates like 03-Oct-2026
lines = ["Account statement;;;;;", "Txn Date;Description;Ref No;Amount (INR);Dr/Cr;Balance (INR)"]
for (d, desc, o, i), b in zip(TX, BAL):
    amt = o if o is not None else i
    lines.append(f"{d:02d}-Oct-2026;{desc};REF{d}{int(amt) % 97};Rs. {inr(amt)};{'Dr' if o is not None else 'Cr'};{inr(b)}")
(OUT / "statement_drcr_semicolon.csv").write_bytes(("\r\n".join(lines) + "\r\n").encode("cp1252"))

# 3. .xlsx with a summary sheet first; real Excel dates; one signed Amount column; title rows above the table
wb = openpyxl.Workbook(); s1 = wb.active; s1.title = "Summary"
s1.append(["Account summary"]); s1.append(["Opening balance", 125000.00]); s1.append(["Closing balance", BAL[-1]])
s2 = wb.create_sheet("Transactions")
s2.append(["Account statement, October 2026"]); s2["A1"].font = Font(bold=True); s2.merge_cells("A1:D1")
s2.append([])
s2.append(["Generated 01/11/2026"])
s2.append(["Posting Date", "Transaction Details", "Amount", "Running Balance"])
for (d, desc, o, i), b in zip(TX, BAL):
    s2.append([dt.datetime(2026, 10, d), desc, -o if o is not None else i, b])
for row in s2.iter_rows(min_row=5, min_col=1, max_col=1):
    for c in row: c.number_format = "DD-MM-YYYY"
wb.save(OUT / "statement_signed_two_sheets.xlsx")

# 4. An ".xls" that is really an HTML page with two tables (account details, then transactions)
rows = "".join(
    f"<tr><td>{d:02d}-10-2026</td><td>{desc}</td><td>{4410 + k}</td><td>{inr(o) if o is not None else ''}</td>"
    f"<td>{inr(i) if i is not None else ''}</td><td>{inr(b)}</td></tr>"
    for k, ((d, desc, o, i), b) in enumerate(zip(TX, BAL)))
html = ("<html><body><table><tr><td>Customer</td><td>A. N. Example Logistics</td></tr>"
        "<tr><td>Account</td><td>XXXXXXXX1234</td></tr></table><br>"
        "<table><tr><th>Tran Date</th><th>PARTICULARS</th><th>CHQNO</th><th>DR</th><th>CR</th><th>BAL</th></tr>"
        f"{rows}</table></body></html>")
(OUT / "statement_html_disguised.xls").write_text(html, encoding="utf-8")

# 5. Excel "Unicode Text": UTF-16 with BOM, tab separated, US month-first dates (10/22/2026)
lines = ["Date\tDescription\tDebit\tCredit\tBalance"]
for (d, desc, o, i), b in zip(TX, BAL):
    lines.append(f"10/{d:02d}/2026\t{desc}\t{o if o is not None else ''}\t{i if i is not None else ''}\t{b}")
(OUT / "statement_unicode_text.txt").write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-16"))

# 6. No header row at all: date, description, signed amount, balance
lines = []
for (d, desc, o, i), b in zip(TX, BAL):
    lines.append(f"2026-10-{d:02d},{desc},{-o if o is not None else i},{b}")
(OUT / "statement_no_header.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

# 7. Dates that could be either order (no day above 12)
lines = ["Date,Narration,Withdrawal,Deposit,Balance",
         "03/04/2026,RAZ*SWIGGY BLR,1240.00,,123760.00",
         "05/04/2026,AWS EMEA,18500.00,,105260.00",
         "09/04/2026,PAYTM*SHARMA TRD,4000.00,,101260.00"]
(OUT / "statement_ambiguous_dates.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

# 8. Invoice register (.xlsx): supplier codes, text account numbers, one row with no code, one re-sent bill
wb = openpyxl.Workbook(); s = wb.active; s.title = "Bills"
s.append(["Bill No", "Supplier", "Supplier Code", "Bill Date", "Amount (₹)", "Bank A/c No", "IFSC", "Email"])
bills = [
    ("INV-101", "VRL Logistics Limited", "V001", dt.datetime(2026, 10, 5), 48000, "50100123450001", "HDFC0000123", "accounts@vrlgroup.in"),
    ("TCI/7", "TCI Express Limited", "V002", dt.datetime(2026, 10, 6), 12500, "33330000999999", "YESB0000777", "billing@tciexpress.in"),
    ("INV-0101", "VRL Logistics Limited", "V001", dt.datetime(2026, 10, 19), 48000, "50100123450001", "HDFC0000123", "accounts@vrlgroup.in"),
    ("VRL-902", "VRL Logistics Limited", None, dt.datetime(2026, 10, 21), 15500, "50100123450001", "HDFC0000123", "accounts@vrlgroup.in"),
]
for b in bills:
    s.append(list(b))
for row in s.iter_rows(min_row=2, min_col=6, max_col=6):
    for c in row: c.number_format = "@"
wb.save(OUT / "invoice_register.xlsx")
print("written:", sorted(p.name for p in OUT.iterdir()))
