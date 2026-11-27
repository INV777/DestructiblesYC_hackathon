"""Reading raw bank exports and invoice lists: offline rules, no network, no AI."""
from __future__ import annotations

import socket
from pathlib import Path

import pytest

from settlebot.importer import analyse, parse_amount, parse_date, read_tables

FILES = Path(__file__).resolve().parent / "fixtures" / "imports"
STATEMENTS = ["statement_withdrawal_deposit.xls", "statement_drcr_semicolon.csv", "statement_signed_two_sheets.xlsx",
              "statement_html_disguised.xls", "statement_unicode_text.txt", "statement_no_header.csv"]
OUT_LINES = [("2026-10-03", "RAZ*SWIGGY BLR", 1240.0), ("2026-10-07", "AWS EMEA", 18500.0),
             ("2026-10-07", "AWS EMEA", 18500.0), ("2026-10-09", "PAYTM*SHARMA TRD", 4000.0),
             ("2026-10-22", "POS CAFÉ COFFEE DAY PUNE", 860.5)]


def read(name, choices=None, known=None):
    data = (FILES / name).read_bytes()
    return analyse(read_tables(data, name), name, choices, known)


@pytest.mark.parametrize("name", STATEMENTS)
def test_every_layout_gives_the_same_money_out_lines(name):
    r = read(name)
    assert r.kind == "bank_line" and not r.problems
    assert [(ln["date"], ln["description"], ln["amount_inr"]) for ln in r.lines] == OUT_LINES
    assert r.money_in == 2                       # the salary credit and the refund are left out
    assert all(ln["source"]["file"] == name and ln["source"]["row"] >= 1 for ln in r.lines)


def test_account_details_above_and_totals_below_the_table_are_skipped():
    r = read("statement_withdrawal_deposit.xls")
    assert r.header_row == 7 and r.mapping["amount"] is None          # "Value Dt" is not an amount
    reasons = {s["reason"] for s in r.skipped}
    assert reasons == {"Opening, closing or total row"}
    assert r.lines[0]["source"]["row"] == 9 and r.lines[0]["reference"] == "0004410"


def test_the_transactions_sheet_is_chosen_over_a_summary_sheet():
    r = read("statement_signed_two_sheets.xlsx")
    assert r.sheet == "Transactions" and r.header_row == 4 and r.amount_mode == "signed"
    assert [s["name"] for s in r.sheets] == ["Summary", "Transactions"]


def test_month_first_dates_are_detected_and_unclear_order_is_flagged():
    assert read("statement_unicode_text.txt").day_first is False
    r = read("statement_ambiguous_dates.csv")
    assert r.day_first is True and r.day_first_certain is False
    assert any("day-first or month-first" in n for n in r.notes)
    us = read("statement_ambiguous_dates.csv", {"day_first": False})
    assert [ln["date"] for ln in us.lines] == ["2026-03-04", "2026-05-04", "2026-09-04"]


def test_columns_are_found_from_the_numbers_when_there_is_no_header():
    r = read("statement_no_header.csv")
    assert r.header_row is None and r.amount_mode == "signed"
    assert r.columns[r.mapping["balance"]]["letter"] == "D" and r.columns[r.mapping["amount"]]["letter"] == "C"


def test_a_persons_column_choice_is_used():
    r = read("statement_withdrawal_deposit.xls")
    m = dict(r.mapping)
    m["description"] = m["reference"]            # pick the reference column as the description
    edited = read("statement_withdrawal_deposit.xls", {"mapping": m})
    assert edited.lines[0]["description"] == "0004410"
    missing = read("statement_withdrawal_deposit.xls", {"mapping": {**m, "debit": None}})
    assert missing.problems and not missing.lines


def test_invoice_register_fills_a_missing_vendor_id_only_on_an_exact_name():
    known = {"VRL LOGISTICS": "V001", "TCI EXPRESS": "V002"}
    r = read("invoice_register.xlsx", known=known)
    assert r.kind == "invoice" and len(r.lines) == 4
    first, last = r.lines[0], r.lines[3]
    assert first["vendor_id"] == "V001" and first["bank_account"] == {
        "account_number": "50100123450001", "ifsc": "HDFC0000123", "name": None}
    assert last["vendor_id"] == "V001" and last["source"]["vendor_id_from_name"] is True
    assert read("invoice_register.xlsx").lines[3]["vendor_id"] is None    # no master: left empty, never guessed


@pytest.mark.parametrize("text,value,marker", [
    ("₹1,23,456.00", 123456.0, None), ("Rs. 4,000", 4000.0, None), ("(500.25)", -500.25, None),
    ("500-", -500.0, None), ("-1,240.00", -1240.0, None), ("1,240.00 Dr", 1240.0, "DR"), ("75 CR", 75.0, "CR"),
    ("INR 18,500", 18500.0, None), (860.5, 860.5, None), ("", None, None), ("-", None, None), ("abc", None, None),
])
def test_amounts(text, value, marker):
    assert parse_amount(text) == (value, marker)


@pytest.mark.parametrize("text,day_first,iso", [
    ("03/10/26", True, "2026-10-03"), ("03/10/2026", False, "2026-03-10"), ("03-Oct-2026", True, "2026-10-03"),
    ("3 October 2026", True, "2026-10-03"), ("Oct 3, 2026", True, "2026-10-03"), ("2026-10-03 10:42:00", True, "2026-10-03"),
    ("03.10.2026", True, "2026-10-03"), ("20261003", True, "2026-10-03"), (46298, True, "2026-10-03"),
    ("31/02/2026", True, None), ("Total", True, None), ("", True, None),
])
def test_dates(text, day_first, iso):
    assert parse_date(text, day_first) == iso


def test_reading_files_never_touches_the_network(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("the importer must work offline")
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    for p in FILES.iterdir():
        read(p.name)
