"""The evidence files: what they say, how they're hashed, and the PDF the bank opens."""

from __future__ import annotations

import copy
import hashlib
import random
import re
from dataclasses import replace
from itertools import permutations
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from disputable import ce3, documents
from disputable.documents import (BRANDS, CANT_SHOW, ITEM, MARGIN, MAX_NAME, NO_SIGNATURE, NO_SIGNER, NOT_A_NUMBER,
                                  PAGE_WIDTH, WIDTHS, Document, purchase_history, warranty_registration, with_files)
from disputable.engine import Action, Evidence, decide
from disputable.fees import EvidenceClass
from disputable.seed.fernhill import (ALEX, ALEX_DEVICE, ALEX_IP, ANSWERS, ENDING_2, FERNHILL, FIRST_PACKAGE,
                                      FIRST_PACKAGE_FACTS, FIRST_PACKAGE_FILE, HEADPHONES_DELIVERED_AT, HEADPHONES_ORDER,
                                      HEADPHONES_SERIAL, SECOND_PACKAGE, SECOND_PACKAGE_FILE, THREE_WEEKS_LATER,
                                      WARRANTY_SIGN_UP)
from disputable.store import Customer, Refund

ROOT = Path(__file__).resolve().parents[1]
ROW = re.compile(rb"BT /F1 (\d+) Tf ([\d.]+) (\d+) Td \(((?:\\.|[^\\)])*)\) Tj ET")
HEADER = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"


def round_one():
    case = FERNHILL.case("headphones")
    return case, ce3.check(FERNHILL, case)


def with_records(**changes):
    """FERNHILL with some records swapped, its cases kept."""
    return replace(FERNHILL, **changes)


def with_orders(store=FERNHILL, **by_number):
    """The store with these orders, by number, in place of its own."""
    return replace(store, orders=tuple(by_number.get(f"o{order.number}", order) for order in store.orders))


def first_file(store):
    case = store.case("headphones")
    return purchase_history(store, case, ce3.check(store, case))


def second_file(store, entry=WARRANTY_SIGN_UP):
    return warranty_registration(store, store.case("headphones"), entry.id)


def opened(pdf: bytes) -> list[list[tuple[int, float, int, str]]]:
    """Read back a PDF this module wrote. Its structure is checked on the way: the xref entries point at their objects,
    and the page tree lists every page. Each page comes back as its rows: (size, x, y, text)."""
    assert pdf.startswith(HEADER) and pdf.endswith(b"%%EOF\n")
    start = int(re.search(rb"startxref\n(\d+)\n%%EOF\n$", pdf).group(1))
    table = re.match(rb"xref\n0 (\d+)\n0000000000 65535 f \n((?:\d{10} 00000 n \n)*)trailer\n<< /Size (\d+) /Root 1 0 R >>",
                     pdf[start:])
    offsets = [int(entry) for entry in re.findall(rb"(\d{10}) 00000 n \n", table.group(2))]
    assert int(table.group(1)) == int(table.group(3)) == len(offsets) + 1
    bodies = {}
    for number, offset in enumerate(offsets, start=1):
        assert pdf[offset:].startswith(b"%d 0 obj\n" % number)
        end = pdf.index(b"\nendobj\n", offset)
        bodies[number] = pdf[offset + len(b"%d 0 obj\n" % number):end]
    assert bodies[1] == b"<< /Type /Catalog /Pages 2 0 R >>"
    assert bodies[3] == b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>"
    kids = [int(kid) for kid in re.findall(rb"(\d+) 0 R", re.search(rb"/Kids \[(.*?)\]", bodies[2]).group(1))]
    assert re.search(rb"/Count (\d+)", bodies[2]).group(1) == b"%d" % len(kids)
    assert b"/Info" not in pdf and b"Date" not in pdf  # nothing that changes from one build to the next
    pages = []
    for kid in kids:
        contents = int(re.search(rb"/Contents (\d+) 0 R", bodies[kid]).group(1))
        length, stream = re.fullmatch(rb"<< /Length (\d+) >>\nstream\n(.*)\nendstream", bodies[contents], re.S).groups()
        assert int(length) == len(stream)
        rows = ROW.findall(stream)
        assert len(rows) == stream.count(b"\n") + 1  # every line of the stream is a row
        pages.append([(int(size), float(x), int(y), unescaped(text)) for size, x, y, text in rows])
    return pages


def unescaped(text: bytes) -> str:
    return re.sub(rb"\\([0-7]{3}|.)", lambda m: bytes([int(m[1], 8)]) if len(m[1]) == 3 else m[1], text).decode("cp1252")


def shown_text(pages) -> str:
    """Every row but the page numbers, one space apart."""
    return " ".join(text for page in pages for size, x, y, text in page[:-1])


def body_rows(document: Document) -> list[str]:
    """The text rows of every page, without the title's rows or the page numbers."""
    pages = opened(document.pdf())
    title = len(documents._wrap(document.title, documents.TITLE[0], PAGE_WIDTH - 2 * MARGIN))
    return [text for page in pages for size, x, y, text in page[:-1]][title:]


# ----------------------------------------------------------------------------- the story's two files
def test_the_first_file_shows_the_purchase_history_and_the_signed_delivery():
    assert FIRST_PACKAGE.fact_ids == FIRST_PACKAGE_FACTS == ANSWERS["headphones"].fact_ids
    assert FIRST_PACKAGE.title == "Order #1109: earlier purchases on the same card"
    lines = FIRST_PACKAGE.lines
    assert lines[0] == ("Disputed order #1109, placed 2026-09-22: Over-ear studio headphones, serial FH-HP-03-55821, "
                        "USD 1,249.00, paid with Visa ending 0008.")
    assert lines[1:6] == ("Cardholder: Alex Sample.", "Store account: acct-alex.",
                          "Delivery address: 2 Sample Street, San Francisco, CA 94103, US.", f"Device ID: {ALEX_DEVICE}.",
                          f"IP address: {ALEX_IP}.")
    for number, placed, days, what in (("1012", "2025-11-24", 302, "Wired in-ear monitors, USD 149.00"),
                                       ("1038", "2026-03-18", 188, "Hard travel case, USD 39.00"),
                                       ("1077", "2026-08-12", 41, "Bluetooth adapter, USD 59.00")):
        assert (f"{ITEM}Order #{number}, placed {placed}, {days} days before the disputed order: {what}. Same account, "
                "delivery address, device and IP as the disputed order. The store's records show no dispute or fraud "
                "report on it.") in lines
    assert lines[-4] == ("Order #1109 was delivered on 2026-09-26 to 2 Sample Street, San Francisco, CA 94103, US "
                         "(carrier tracking 1Z999AA10123456784).")
    # The seed review's line: the first file cites the signed delivery, so it must say so.
    assert lines[-3] == "Delivered and signed for at the billing address. Signed by: A. SAMPLE."
    assert lines[-1] == "All dates are calendar dates in UTC."


def test_the_second_file_shows_the_warranty_sign_up_against_the_delivery():
    assert SECOND_PACKAGE.fact_ids == {WARRANTY_SIGN_UP.id} == ANSWERS["headphones_round_2"].fact_ids
    assert SECOND_PACKAGE.title == f"Order #1109: warranty registration of serial {HEADPHONES_SERIAL}"
    assert SECOND_PACKAGE.lines[5:9] == (
        f"Serial {HEADPHONES_SERIAL} was registered for warranty on 2026-09-29, 3 days after delivery:",
        f"{ITEM}It used {ALEX.email}, the email on the customer's orders since order #1012 (2025-11-24).",
        f"{ITEM}A code sent by text message to {ALEX.phone}, the cardholder's phone on order #1109, confirmed it.",
        f"{ITEM}It came from device {WARRANTY_SIGN_UP.device_id} and IP address {WARRANTY_SIGN_UP.ip_address}. None of "
        "the customer's orders came from that device or that IP address.")
    assert "Delivered and signed for at the billing address. Signed by: A. SAMPLE." in SECOND_PACKAGE.lines
    for value in (HEADPHONES_SERIAL, ALEX.email, ALEX.phone, WARRANTY_SIGN_UP.device_id, WARRANTY_SIGN_UP.ip_address):
        assert FERNHILL.quote_is_in(WARRANTY_SIGN_UP.id, value) and value in SECOND_PACKAGE.text


def test_the_story_files_keep_their_hashes():
    # A change of wording gives a file a new hash, so a case between rounds would take a rebuilt file for a new one.
    # Change these only for a reason that outweighs that.
    assert FIRST_PACKAGE_FILE == "64631fdcbf12d0484f219a78616f3e45b422fdb1e32692f65f2b4a33d620b1be"
    assert SECOND_PACKAGE_FILE == "2d8e9d0a0b30c6471fe9a6620f16bde5b2e948b8492793bb47af5c2555a3e436"


def test_the_seed_rejects_the_very_file_round_one_sends():
    assert FERNHILL.case("headphones_round_2").rejected_file_hashes == {FIRST_PACKAGE.sha256}
    assert ANSWERS["headphones"].file_hashes == {FIRST_PACKAGE.sha256}
    assert ANSWERS["headphones_round_2"].file_hashes == {SECOND_PACKAGE.sha256}
    assert ENDING_2.warranty == ()  # so Ending 2 has no second file to build
    with pytest.raises(KeyError):
        warranty_registration(ENDING_2, ENDING_2.case("headphones_round_2"), WARRANTY_SIGN_UP.id)


# ----------------------------------------------------------------------------- the hash, and the package
def test_a_file_built_again_has_the_same_text_hash_and_bytes():
    store = copy.deepcopy(FERNHILL)
    case = store.case("headphones")
    again = purchase_history(store, case, ce3.check(store, case))
    assert again == FIRST_PACKAGE and again.sha256 == FIRST_PACKAGE_FILE
    assert again.pdf() == FIRST_PACKAGE.pdf()  # no "Generated" stamp, no run id, no date of the build
    assert warranty_registration(store, case, WARRANTY_SIGN_UP.id).pdf() == SECOND_PACKAGE.pdf()
    # Round two builds the sign-up's file from its own case, and gets the same file.
    assert warranty_registration(store, store.case("headphones_round_2"), WARRANTY_SIGN_UP.id) == SECOND_PACKAGE


def test_the_hash_is_of_the_text_not_of_the_bytes(monkeypatch):
    assert FIRST_PACKAGE.sha256 == hashlib.sha256(FIRST_PACKAGE.text.encode("utf-8")).hexdigest()
    assert FIRST_PACKAGE.sha256 != hashlib.sha256(FIRST_PACKAGE.pdf()).hexdigest()
    assert replace(FIRST_PACKAGE, notes="Other notes", description="Other words").sha256 == FIRST_PACKAGE.sha256
    before = FIRST_PACKAGE.pdf()
    monkeypatch.setattr(documents, "TEXT", (11, 15))  # laid out anew: other bytes, the same evidence
    assert FIRST_PACKAGE.pdf() != before and FIRST_PACKAGE.sha256 == FIRST_PACKAGE_FILE
    changed = replace(FIRST_PACKAGE, lines=(*FIRST_PACKAGE.lines[:-1], "All dates are calendar dates in UTC!"))
    assert changed.sha256 != FIRST_PACKAGE.sha256


def test_the_same_records_in_another_order_give_the_same_files():
    # A database read with no ORDER BY can hand records back in any order, and a tie in time keeps that order. The text,
    # and so the hash, must not follow it.
    at = HEADPHONES_DELIVERED_AT
    twin = replace(FERNHILL.order("1012"), number="1013")  # one checkout, split in two at the same moment
    orders = (*with_orders(o1012=replace(FERNHILL.order("1012"), refunds=(Refund(Decimal("7.00"), at),
                                                                        Refund(Decimal("5.00"), at)))).orders, twin)
    scans = (*FERNHILL.scans, replace(FERNHILL.record("scan-1109-delivered"), id="scan-1109-delivered-again",
                                      signed_by="ALEX S"))
    files = [(first_file(store), second_file(store)) for store in (with_records(orders=orders, scans=scans),
                                                                    with_records(orders=orders[::-1], scans=scans[::-1]))]
    assert files[0] == files[1]
    first, second = files[0]
    assert "Signed by: ALEX S." in first.text and "scan-1109-delivered-again" in first.fact_ids  # the later id wins a tie
    assert "USD 5.00 of it was refunded on 2026-09-26. USD 7.00 of it was refunded on 2026-09-26." in first.text
    assert f"{ITEM}It used {ALEX.email}, the email on the customer's orders since order #1012 (2025-11-24)." in second.lines


def test_a_new_fact_beside_a_rebuilt_rejected_file_goes_to_a_person():
    round_2 = FERNHILL.dispute("headphones_round_2", THREE_WEEKS_LATER)
    case, result = round_one()
    rebuilt = purchase_history(FERNHILL, case, result)  # round one's file, built again three weeks later
    assert rebuilt.sha256 == FIRST_PACKAGE_FILE
    new = Evidence(EvidenceClass.INDEPENDENT_POSSESSION, SECOND_PACKAGE.fact_ids, frozenset({SECOND_PACKAGE.sha256}))
    assert decide(round_2, new, THREE_WEEKS_LATER).action is Action.CHALLENGE
    decision = decide(round_2, replace(new, file_hashes=new.file_hashes | {rebuilt.sha256}), THREE_WEEKS_LATER)
    assert decision.action is Action.HAND_OFF and "rebuilt" in decision.reasons[0]


def refunded_since():
    """The records three weeks on: a small refund on order #1012 that round one never saw."""
    refund = Refund(Decimal("10.00"), THREE_WEEKS_LATER - timedelta(days=1))
    return with_orders(o1012=replace(FERNHILL.order("1012"), refunds=(refund,)))


@pytest.mark.parametrize("store, case, reported", [
    (FERNHILL, FERNHILL.case("headphones_round_2"), ["1038"]),
    (ENDING_2, ENDING_2.case("headphones_round_2"), ["1038"]),
    (refunded_since(), FERNHILL.case("headphones_round_2"), []),
    (FERNHILL, replace(FERNHILL.case("headphones"), prior_challenges=1), []),
    (FERNHILL, replace(FERNHILL.case("headphones_round_2"), prior_challenges=0), []),
], ids=["with the bank's report", "in the second ending", "after a refund", "after an RFI nobody answered",
        "with only the rejected sets"])
def test_a_later_round_never_builds_the_purchase_history(store, case, reported):
    # Visa allows one CE 3.0 attempt. Built again in round two, from that round's check or from records that moved on,
    # round one's file gets other text, so another hash, and would pass for a new file beside the warranty sign-up.
    with pytest.raises(ValueError, match="one CE 3.0 attempt"):
        purchase_history(store, case, ce3.check(store, case, reported))


def test_records_that_moved_on_would_give_round_ones_file_another_hash():
    # Why a later round can't rebuild it: the same five records, one refund later, read differently.
    assert first_file(refunded_since()).fact_ids == FIRST_PACKAGE_FACTS
    assert first_file(refunded_since()).sha256 != FIRST_PACKAGE_FILE


def test_a_sign_up_the_bank_turned_down_isnt_built_again():
    # Round three, after the bank turned down the warranty file: a later order could change its last line, so a copy
    # built now could pass for a new file.
    case = replace(FERNHILL.case("headphones_round_2"), rejected_fact_ids=FIRST_PACKAGE_FACTS | {WARRANTY_SIGN_UP.id})
    with pytest.raises(ValueError, match="already turned down"):
        warranty_registration(FERNHILL, case, WARRANTY_SIGN_UP.id)


def test_a_package_keeps_every_record_its_files_prove():
    case, result = round_one()
    package = with_files(ce3.evidence(result, case), FIRST_PACKAGE)
    # The signed delivery is the file's record, not CE 3.0's. Left out of round one's plan, round two would take it
    # for new.
    assert package == ANSWERS["headphones"].evidence()
    assert package.fact_ids - result.fact_ids == {"scan-1109-delivered"}
    later = FERNHILL.case("headphones_round_2")
    assert with_files(Evidence(EvidenceClass.INDEPENDENT_POSSESSION), SECOND_PACKAGE) == ANSWERS[later.id].evidence()
    # In a later round CE 3.0 sends nothing. A file put beside that would go nowhere, so it's refused rather than dropped:
    # round two's warranty file goes with its own class, as above.
    nothing = ce3.evidence(ce3.check(FERNHILL, later, ["1038"]), later)
    assert with_files(nothing) == nothing == Evidence(EvidenceClass.NONE)
    for file in (FIRST_PACKAGE, SECOND_PACKAGE):
        with pytest.raises(ValueError, match="no class"):
            with_files(nothing, file)


def test_the_file_proves_only_the_records_it_shows():
    case, result = round_one()
    one_less = replace(result, priors=tuple(replace(prior, reported=prior.order.number == "1077")
                                            for prior in result.priors))
    document = purchase_history(FERNHILL, case, one_less)
    assert "#1077" not in document.text and document.fact_ids == FIRST_PACKAGE_FACTS - {"order-1077"}


# ----------------------------------------------------------------------------- the PDF
@pytest.mark.parametrize("document", [FIRST_PACKAGE, SECOND_PACKAGE], ids=["first file", "second file"])
def test_the_pdf_shows_every_word_of_the_text_inside_the_margins(document):
    pages = opened(document.pdf())
    assert len(pages) == 1 and pages[0][-1] == (documents.FOOTER, MARGIN, MARGIN // 2, "Page 1 of 1")
    assert shown_text(pages) == " ".join(line for line in (document.title, *document.lines) if line)
    for page in pages:
        for size, x, y, text in page:
            assert x + documents._width(text, size) <= PAGE_WIDTH - MARGIN and y >= MARGIN // 2
        rows = [y for size, x, y, text in page]
        assert rows == sorted(rows, reverse=True)  # top to bottom


def test_list_items_line_up_after_the_dash():
    rows = opened(FIRST_PACKAGE.pdf())[0]
    starts = [index for index, (size, x, y, text) in enumerate(rows) if text.startswith(f"{ITEM}Order #1012")]
    assert len(starts) == 1
    first, after = rows[starts[0]], rows[starts[0] + 1]
    assert first[1] == MARGIN and after[1] == pytest.approx(MARGIN + documents._width(ITEM, documents.TEXT[0]), abs=0.01)


def test_a_long_document_runs_onto_numbered_pages_and_a_long_word_is_cut():
    word = "X" * 300
    document = Document("Many lines", (word, *(f"Paragraph {i} " + "words " * 30 + "end." for i in range(80))),
                        frozenset({"order-1"}), "notes", "description")
    pages = opened(document.pdf())
    assert len(pages) > 2
    assert [page[-1][3] for page in pages] == [f"Page {i} of {len(pages)}" for i in range(1, len(pages) + 1)]
    cut = [text for size, x, y, text in pages[0] if set(text) == {"X"}]
    assert len(cut) > 1 and "".join(cut) == word
    for page in pages:
        for size, x, y, text in page:
            assert x + documents._width(text, size) <= PAGE_WIDTH - MARGIN and y >= MARGIN // 2


def test_a_long_run_with_no_space_is_cut_in_one_pass(monkeypatch):
    # A record can hold thousands of characters with no space, such as an image in base64. Measured again for every cut,
    # that took minutes; read once, each character is looked up about once.
    run = "".join("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"[i * 37 % 64] for i in range(3000))
    document = Document("A long run", (f"Note: {run}",), frozenset({"scan-1"}), "notes", "description")
    looked_up = 0

    class Counted(dict):
        def get(self, key, default=None):
            nonlocal looked_up
            looked_up += 1
            assert looked_up <= 2 * len(run), "each character's width is looked up about once"
            return super().get(key, default)

    with monkeypatch.context() as patch:
        patch.setattr(documents, "WIDTHS", Counted(WIDTHS))
        document.pdf()
    assert looked_up >= len(run)  # every width came from the table
    rows = body_rows(document)
    assert rows[0] == "Note:" and "".join(rows[1:]) == run
    size = documents.TEXT[0]
    for row, after in zip(rows[1:], rows[2:]):  # each cut row holds all that fits
        assert documents._width(row, size) <= PAGE_WIDTH - 2 * MARGIN < documents._width(row + after[0], size)


def test_short_words_are_measured_once_not_with_the_whole_row_again(monkeypatch):
    # Joining a word to a row measured the whole row again: about 50 look-ups a character for one-letter words, so a
    # record of a million characters kept pdf() busy for tens of seconds. The row's width is now added up as it grows.
    text = " ".join(["i"] * 3000)
    looked_up = 0

    class Counted(dict):
        def get(self, key, default=None):
            nonlocal looked_up
            looked_up += 1
            return super().get(key, default)

    size, width = documents.TEXT[0], PAGE_WIDTH - 2 * MARGIN
    with monkeypatch.context() as patch:
        patch.setattr(documents, "WIDTHS", Counted(WIDTHS))
        rows = documents._wrap(text, size, width)
    assert looked_up <= 2 * len(text), f"{looked_up / len(text):.1f} look-ups a character"
    assert " ".join(rows) == text and len(rows) > 1
    for row, after in zip(rows, rows[1:]):  # each row holds every word that fits, as _width measures it
        assert documents._width(row, size) <= width < documents._width(f"{row} {after.split(' ')[0]}", size)
    assert documents._width(rows[-1], size) <= width


def test_a_row_the_text_fills_exactly_is_kept_whole():
    # 27 x and 150 i are 46,800 thousandths of the font size: at size 10, exactly the 468 points between the margins.
    size, width = documents.TEXT[0], PAGE_WIDTH - 2 * MARGIN
    word, pair = "x" * 27 + "i" * 150, ("i" * 150, "x" * 26 + "i")  # the pair, with the space between, fills it too
    assert documents._width(word, size) == width == documents._width(" ".join(pair), size)
    assert documents._wrap(word, size, width) == [word] and documents._wrap(" ".join(pair), size, width) == [" ".join(pair)]
    assert documents._wrap(word + "i", size, width) == [word, "i"]


def test_a_text_longer_than_visas_limit_is_refused_before_it_is_laid_out(monkeypatch):
    # Every character shown takes a byte of the file at least, so such a text can never fit. Laid out first, a few
    # megabytes of short words kept pdf() busy for a minute, only to be refused.
    monkeypatch.setattr(documents, "MAX_PDF_BYTES", 1000)
    document = Document("Title", (" ".join(["i"] * 500),), frozenset({"order-1"}), "notes", "description")  # 1,004
    monkeypatch.setattr(documents, "_wrap", lambda *args: pytest.fail("laid out before the size check"))
    with pytest.raises(ValueError, match="more than 1,004 bytes, over the 1,000 Visa takes"):
        document.pdf()


def test_a_character_outside_ascii_counts_as_a_full_width():
    # Helvetica draws é narrower, but the table holds only ASCII: any other character counts as wide as the font's size,
    # so a row of them can never run past the margin.
    document = Document("Wide", ("é" * 100, " ".join(["é" * 30] * 2)), frozenset({"order-1"}), "notes", "description")
    rows = body_rows(document)
    assert "".join(rows) == "é" * 160 and max(map(len, rows)) == (PAGE_WIDTH - 2 * MARGIN) // documents.TEXT[0]


def test_a_page_never_starts_with_a_blank_line():
    full = tuple(f"Line {i}." for i in range(1, 44))  # fills the first page under a one-row title
    pages = opened(Document("Title", (*full, "", "Next."), frozenset({"order-1"}), "notes", "description").pdf())
    assert len(pages) == 2 and [text for size, x, y, text in pages[0][1:-1]] == list(full)
    assert pages[1][0][2:] == (documents.PAGE_HEIGHT - MARGIN - documents.TEXT[1], "Next.")


def test_brackets_backslashes_and_accents_come_through_the_pdf():
    line = r"Signed (twice) by Zoë Café \ Ünal, costing £5 – or €6."
    pdf = Document("Escapes", (line,), frozenset({"order-1"}), "notes", "description").pdf()
    assert opened(pdf)[0][1][3] == line
    assert pdf[len(HEADER):].isascii()  # accents go in as octal escapes, so no byte can be taken for another


def test_the_width_table_covers_printable_ascii():
    assert sorted(WIDTHS) == list(range(32, 127))
    assert WIDTHS[ord(" ")] == 278 and WIDTHS[ord("W")] == 944 and WIDTHS[ord("i")] == 222


def test_a_pdf_over_visas_limit_is_refused(monkeypatch):
    # The story's first file is refused before it's laid out; the second, whose text fits the limit but whose bytes
    # don't, keeps the check on the finished file tested.
    def characters(document):
        return len(document.title) + sum(map(len, document.lines))

    dense = Document("Dense", (" ".join(["'"] * 5000),), frozenset({"order-1"}), "notes", "description")
    for document in (FIRST_PACKAGE, SECOND_PACKAGE, dense):  # the early check's premise
        assert len(document.pdf()) > characters(document)
    monkeypatch.setattr(documents, "MAX_PDF_BYTES", 1000)
    assert characters(FIRST_PACKAGE) > 1000 > characters(SECOND_PACKAGE)
    with pytest.raises(ValueError, match=r"more than [\d,]+ bytes, over the 1,000 Visa takes"):
        FIRST_PACKAGE.pdf()
    with pytest.raises(ValueError, match=r"The file would be [\d,]+ bytes, over the 1,000 Visa takes"):
        SECOND_PACKAGE.pdf()


# ----------------------------------------------------------------------------- what a document can hold
@pytest.mark.parametrize("line", ["two\nlines", "a\ttab", "two  spaces", " leading", "trailing ", "zero​width",
                                  "soft­hyphen", "Łukasz", "é", "a \U0001f600"],
                         ids=["newline", "tab", "double space", "leading space", "trailing space", "zero-width space",
                              "soft hyphen", "outside WinAnsi", "not composed", "emoji"])
def test_text_the_pdf_cant_show_as_written_is_refused(line):
    with pytest.raises(ValueError, match=re.escape(CANT_SHOW)):
        Document("Title", (line,), frozenset({"order-1"}), "notes", "description")


def test_a_document_needs_a_title_a_line_and_a_fact_and_short_notes():
    for title, lines, facts, notes in (("", ("line",), {"order-1"}, ""), ("Title", ("",), {"order-1"}, ""),
                                       ("Title", ("line",), set(), ""), ("Title", ("line",), {" "}, ""),
                                       ("Title", ("line",), {"order-1"}, "n" * (MAX_NAME + 1))):
        with pytest.raises(ValueError):
            Document(title, lines, frozenset(facts), notes, "description")
    with pytest.raises(TypeError):
        Document("Title", ["a list can change"], frozenset({"order-1"}), "", "")


def test_a_blank_last_line_is_refused():
    # The PDF shows nothing for it, or a last page with only its number, but the hash counts it: the file the bank
    # rejected could come back byte for byte under a new hash.
    for lines in ((*FIRST_PACKAGE.lines, ""), (*(f"Line {i}." for i in range(1, 44)), "")):
        with pytest.raises(ValueError, match="blank line"):
            Document("Title", lines, frozenset({"order-1"}), "notes", "description")


def test_a_file_name_holds_only_the_hash_and_the_notes_fit():
    for document in (FIRST_PACKAGE, SECOND_PACKAGE):
        assert document.filename == f"evidence-{document.sha256[:16]}.pdf" and len(document.filename) <= MAX_NAME
        assert 0 < len(document.notes) <= MAX_NAME
    assert (FIRST_PACKAGE.notes, SECOND_PACKAGE.notes) == ("Earlier purchases, order 1109",
                                                           "Warranty registration, order 1109")


def test_a_record_the_pdf_cant_show_stops_the_file():
    store = with_orders(o1109=replace(HEADPHONES_ORDER, cardholder_name="Łukasz Przykład"))
    with pytest.raises(ValueError, match=re.escape(CANT_SHOW)):
        first_file(store)


def test_spacing_breaks_and_decomposed_letters_in_a_record_are_written_plainly():
    assert "Cardholder: Alex Sample." in first_file(with_orders(o1109=replace(HEADPHONES_ORDER,
                                                                             cardholder_name="  Alex \n Sample "))).lines
    # An accent typed as a letter plus a combining mark is shown as the one letter WinAnsiEncoding has.
    assert "Cardholder: Zoë Sample." in first_file(with_orders(o1109=replace(HEADPHONES_ORDER,
                                                                            cardholder_name="Zoë Sample"))).lines


# ----------------------------------------------------------------------------- what each file says when records differ
def test_a_check_for_another_order_or_with_no_earlier_order_is_refused():
    case, result = round_one()
    with pytest.raises(ValueError, match="another order"):
        purchase_history(FERNHILL, FERNHILL.case("ear_tips"), result)
    nothing = replace(result, priors=tuple(replace(prior, reported=True) for prior in result.priors))
    with pytest.raises(ValueError, match="cites no earlier order"):
        purchase_history(FERNHILL, case, nothing)


def test_a_sign_up_for_another_serial_or_none_is_refused():
    other = replace(WARRANTY_SIGN_UP, serial="FH-HP-03-99999")
    with pytest.raises(ValueError, match="isn't on order #1109"):
        second_file(with_records(warranty=(other,)), other)
    with pytest.raises(ValueError, match="isn't a warranty registration"):
        warranty_registration(FERNHILL, FERNHILL.case("headphones"), "scan-1109-delivered")
    # "N/A" on both is what a store writes for no serial: it ties no sign-up to the order.
    order = replace(HEADPHONES_ORDER, items=tuple(replace(item, serial="N/A") for item in HEADPHONES_ORDER.items))
    entry = replace(WARRANTY_SIGN_UP, serial="N/A")
    with pytest.raises(ValueError, match="isn't on order #1109"):
        second_file(replace(with_orders(o1109=order), warranty=(entry,)), entry)


def with_delivery(**changes):
    return with_records(scans=tuple(replace(scan, **changes) if scan.id == "scan-1109-delivered" else scan
                                    for scan in FERNHILL.scans))


BILLING, ELSEWHERE = ALEX.billing_address, "9 Other Road, Oakland, CA 94607, US"


@pytest.mark.parametrize("signed_by, location, note, says", [
    ("A. SAMPLE", BILLING, "", "Delivered and signed for at the billing address. Signed by: A. SAMPLE."),
    ("A. SAMPLE", ELSEWHERE, "", "Signed for on delivery by A. SAMPLE."),
    (None, "2 SAMPLE STREET,  San Francisco, CA 94103, US", "Left at front door",
     'Delivered to the billing address, with no signature. Carrier note: "Left at front door"'),
    ("unknown", ELSEWHERE, "", "Delivered with no signature."),
    ("DRIVER RELEASE", BILLING, "", "Delivered to the billing address, with no signature."),
    ("Signature not required", ELSEWHERE, "Left with neighbour at No. 4.",
     'Delivered with no signature. Carrier note: "Left with neighbour at No. 4."'),
    (None, ELSEWHERE, 'Rang twice. "Leave it," she said. “Signed for”.',
     "Delivered with no signature. Carrier note: \"Rang twice. 'Leave it,' she said. 'Signed for'.\""),
    (None, ELSEWHERE, "Left at door.'' Signed for by A. SAMPLE. „x“ «y» ‘‘",
     "Delivered with no signature. Carrier note: \"Left at door.' Signed for by A. SAMPLE. 'x' 'y' '\""),
    ("SAMPLE JR.", BILLING, "", "Delivered and signed for at the billing address. Signed by: SAMPLE JR."),
], ids=["signed at the billing address", "signed elsewhere", "unsigned at the billing address", "a placeholder signature",
        "a driver's release", "no signature needed, and a note in the carrier's words", "quotation marks in the note",
        "marks that look like a closing quote", "a signer's name with its own full stop"])
def test_the_delivery_lines_say_only_what_the_scan_shows(signed_by, location, note, says):
    # The note is quoted, so the bank can tell the carrier's words from the file's own, and a quotation mark inside it
    # becomes a single one, so no note can end the quote early and go on as the file's own sentence.
    document = first_file(with_delivery(signed_by=signed_by, location=location, note=note))
    assert document.lines[-3] == says and "scan-1109-delivered" in document.fact_ids


@pytest.mark.parametrize("signed_by", [
    *sorted(NO_SIGNATURE | NO_SIGNER), "Left at front door.", "Front Door / Porch", "FRONTDOOR", "MAIL BOX",
    "Driver-release", "Signature not required.", "Not signed.", "Left with Individual", "Left at back door",
    "Released without signature", "Authority to leave", "COVID-19", "C19", "Front Desk/Reception/Mail Room",
    "Neighbor as Requested", "P.O. Box", "Safeplace"])
def test_a_carriers_words_for_nobody_are_never_a_signature(signed_by):
    # A listed phrase written another way, or a carrier's phrase with a word no name has: no one signed for it.
    assert first_file(with_delivery(signed_by=signed_by)).lines[-3] == "Delivered to the billing address, with no signature."


@pytest.mark.parametrize("signed_by", ["(none)", "Unknown.", "N/A.", "None.", "null.", "not set.", "[none]", "--", "?", "*"])
def test_a_placeholder_however_its_punctuated_or_a_value_with_no_words_is_no_signature(signed_by):
    # "unknown" is no signature, so "Unknown." is none either, and a value with no letters or digits names no one.
    store, says = with_delivery(signed_by=signed_by), "Delivered to the billing address, with no signature."
    assert first_file(store).lines[-3] == says and says in second_file(store).lines


@pytest.mark.parametrize("signed_by", ["A. SAMPLE", "J. DOORLEY", "M. LEFTWICH", "DOCK", "RECEPTION", "J. GUEST", "NA LEE"])
def test_a_name_or_a_role_a_person_signs_as_is_still_a_signer(signed_by):
    assert first_file(with_delivery(signed_by=signed_by)).lines[-3] == (
        f"Delivered and signed for at the billing address. Signed by: {signed_by}.")


@pytest.mark.parametrize("location, tracking_number, says", [
    ("", "1Z999AA10123456784", "Order #1109 was delivered on 2026-09-26 (carrier tracking 1Z999AA10123456784)."),
    ("unknown", None, "Order #1109 was delivered on 2026-09-26."),
], ids=["no location", "a placeholder location"])
def test_a_delivery_with_no_place_on_record_names_none(location, tracking_number, says):
    lines = first_file(with_delivery(location=location, tracking_number=tracking_number)).lines
    assert lines[-4:-2] == (says, "Signed for on delivery by A. SAMPLE.")


def test_the_billing_address_is_named_only_when_the_cardholder_is_the_customer():
    # The store keeps no billing address for a card. To a bank "the billing address" is the cardholder's, so when
    # someone else's card paid, the line would claim a match the records don't show.
    document = first_file(with_orders(o1109=replace(HEADPHONES_ORDER, cardholder_name="Jordan Other")))
    assert document.lines[-3] == "Signed for on delivery by A. SAMPLE." and "billing address" not in document.text
    # A name typed at checkout is what a fraud claim disputes. When the same card is on another customer's order in
    # someone else's name, the card may be theirs, so the delivery isn't said to be at its billing address either.
    jordan = Customer("customer-jordan", "Jordan Other", "jordan.other@example.com", "+1 4155550188", ELSEWHERE)
    jordans = replace(FERNHILL.order("1012"), number="1050", customer_id=jordan.id, card_fingerprint="card-jordan",
                      cardholder_name=jordan.name)
    store = with_orders(with_records(customers=(*FERNHILL.customers, jordan), orders=(*FERNHILL.orders, jordans)),
                        o1109=replace(HEADPHONES_ORDER, card_fingerprint="card-jordan"))
    document = second_file(store)
    assert "Signed for on delivery by A. SAMPLE." in document.lines and "billing address" not in document.text
    alexs = replace(jordans, cardholder_name=ALEX.name)  # the card under the customer's own name on the other account
    store = replace(store, orders=tuple(alexs if order.number == "1050" else order for order in store.orders))
    assert "Delivered and signed for at the billing address. Signed by: A. SAMPLE." in second_file(store).lines
    # Nor when the customer's own earlier order on this card names someone else: the card may be theirs.
    store = with_orders(o1012=replace(FERNHILL.order("1012"), cardholder_name="Jordan Other"))
    assert "billing address" not in first_file(store).text and "billing address" not in second_file(store).text


@pytest.mark.parametrize("name", ["Na Na", "Na Nil"])
def test_a_name_made_only_of_placeholder_words_still_names_someone_on_the_card(name):
    # The file reads "Na Na" as no name, but it may be a person's: on another order on this card, the card may be theirs,
    # so the delivery isn't said to be at its billing address. One placeholder names no one, as a blank doesn't.
    for other, says in ((name, "Signed for on delivery by A. SAMPLE."),
                        ("N/A", "Delivered and signed for at the billing address. Signed by: A. SAMPLE.")):
        store = with_orders(o1012=replace(FERNHILL.order("1012"), cardholder_name=other))
        assert first_file(store).lines[-3] == says and says in second_file(store).lines


def test_the_last_outbound_delivery_counts_and_a_return_does_not():
    delivered = FERNHILL.record("scan-1109-delivered")
    again = replace(delivered, id="scan-1109-redelivered", at=delivered.at + timedelta(days=1), location=ELSEWHERE,
                    signed_by="B. OTHER")
    back = replace(delivered, id="scan-1109-return-delivered", direction="return", at=delivered.at + timedelta(days=5),
                   signed_by="DOCK")
    for scans in ((*FERNHILL.scans, again, back), (back, again, *FERNHILL.scans)):
        document = first_file(with_records(scans=scans))
        assert document.lines[-4:-2] == (
            f"Order #1109 was delivered on 2026-09-27 to {ELSEWHERE} (carrier tracking 1Z999AA10123456784).",
            "Signed for on delivery by B. OTHER.")
        assert document.fact_ids == FIRST_PACKAGE_FACTS - {delivered.id} | {again.id}


def test_with_no_delivery_scan_the_file_shows_none_and_cites_none():
    store = with_records(scans=tuple(scan for scan in FERNHILL.scans if scan.id != "scan-1109-delivered"))
    document = first_file(store)
    assert "delivered" not in document.text and document.fact_ids == FIRST_PACKAGE_FACTS - {"scan-1109-delivered"}
    assert f"Serial {HEADPHONES_SERIAL} was registered for warranty on 2026-09-29:" in second_file(store).lines


@pytest.mark.parametrize("dispute, refunds, ends", [
    ("NON_FRAUD", ((Decimal("10.50"), 200),),
     "The store's records show a dispute on it that wasn't for fraud. USD 10.50 of it was refunded on 2026-03-10."),
    ("INQUIRY", (), "The store's dispute field for it reads \"INQUIRY\"."),
    ("none", (), "The store's records show no dispute or fraud report on it."),
    ("unknown", (), "The store's records don't show whether it had a dispute."),
    (" UNDEFINED ", (), "The store's records don't show whether it had a dispute."),
    ("Unknown.", (), "The store's records don't show whether it had a dispute."),
    ("(none)", (), "The store's records show no dispute or fraud report on it."),
    ("No dispute", (), "The store's dispute field for it reads \"No dispute\"."),
    ("?", (), "The store's records don't show whether it had a dispute."),  # a question isn't "none"
    ("None?", (), "The store's records don't show whether it had a dispute."),
    ("FRAUD?", (), "The store's dispute field for it reads \"FRAUD?\"."),  # a word with it is quoted, never hidden
    ("Unknown / N/A", (), "The store's records don't show whether it had a dispute."),  # any part unsure
    ("N/A; undefined", (), "The store's records don't show whether it had a dispute."),
    ("None, None", (), "The store's records show no dispute or fraud report on it."),
    (None, ((Decimal("10.50"), 200), (Decimal("5"), 230)),
     "The store's records show no dispute or fraud report on it. USD 5.00 of it was refunded on 2026-02-08. USD 10.50 of "
     "it was refunded on 2026-03-10."),
], ids=["a dispute that wasn't for fraud, and a refund", "a dispute of another kind", "a placeholder", "unknown",
        "undefined", "unknown, punctuated", "a placeholder, punctuated", "a value that's no dispute", "a question mark",
        "a placeholder with a question mark", "a word with a question mark", "an unknown part", "an undefined part",
        "placeholders joined", "two refunds"])
def test_an_earlier_order_shows_a_dispute_and_refunds_on_it(dispute, refunds, ends):
    refunds = tuple(Refund(amount, HEADPHONES_DELIVERED_AT - timedelta(days=days)) for amount, days in refunds)
    store = with_orders(o1012=replace(FERNHILL.order("1012"), prior_dispute=dispute, refunds=refunds))
    assert next(line for line in first_file(store).lines if "#1012" in line).endswith(ends)


@pytest.mark.parametrize("brand, last4, paid", [
    *((key, "0008", f"{name} ending 0008") for key, name in BRANDS.items()),
    ("unknown", "0008", "a card ending 0008"), ("", "0008", "a card ending 0008"), ("JCB", "0008", "JCB ending 0008"),
    ("Union Pay", "0008", "UnionPay ending 0008"), ("maestro", "0008", "Maestro ending 0008"),
    ("cartes_bancaires", "0008", "Cartes Bancaires ending 0008"), ("visa", "n/a", "Visa"),
], ids=[*BRANDS, "a placeholder brand", "no brand", "upper case", "with a space", "a brand with no spelling",
        "words joined by _", "no last digits"])
def test_the_card_is_named_as_a_bank_names_it(brand, last4, paid):
    order = replace(HEADPHONES_ORDER, card_brand=brand, card_last4=last4)
    assert first_file(with_orders(o1109=order)).lines[0].endswith(f", paid with {paid}.")


def test_an_orders_items_read_the_same_in_any_order():
    # Order lines can come back from a database in any order: the file lists them in a fixed one.
    pads = replace(HEADPHONES_ORDER.items[0], sku="FH-EP-01", description="Replacement ear pads", serial=None, quantity=2,
                   unit_price=Decimal("25"))
    files = [first_file(with_orders(o1109=replace(HEADPHONES_ORDER, items=items)))
             for items in ((*HEADPHONES_ORDER.items, pads), (pads, *HEADPHONES_ORDER.items))]
    assert files[0] == files[1]
    assert files[0].lines[0] == ("Disputed order #1109, placed 2026-09-22: 2 x Replacement ear pads; Over-ear studio "
                                 "headphones, serial FH-HP-03-55821, USD 1,299.00, paid with Visa ending 0008.")


@pytest.mark.parametrize("change", [
    {"o1012": replace(FERNHILL.order("1012"), refunds=(Refund(Decimal("NaN"), HEADPHONES_DELIVERED_AT),
                                                       Refund(Decimal("5"), HEADPHONES_DELIVERED_AT)))},
    {"o1109": replace(HEADPHONES_ORDER, items=tuple(replace(item, unit_price=Decimal("sNaN"))
                                                    for item in HEADPHONES_ORDER.items))},
    {"o1012": replace(FERNHILL.order("1012"), refunds=(Refund(Decimal("1E+1000000"), HEADPHONES_DELIVERED_AT),))},
], ids=["two refunds at one moment, one not a number", "a signalling NaN price", "a refund past decimal's range"])
def test_an_amount_that_isnt_a_number_stops_the_file_plainly(change):
    # Neither may slip through as decimal's own error: a caller hands a ValueError to a person.
    with pytest.raises(ValueError, match=re.escape(NOT_A_NUMBER)):
        first_file(with_orders(**change))


@pytest.mark.parametrize("price", [Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("9E+999999")])
def test_an_earlier_orders_odd_price_stops_the_file_plainly_after_the_check(price):
    # The check runs first and reads such an order as refunded; then the file stops with its own error, not decimal's.
    order = FERNHILL.order("1012")
    store = with_orders(o1012=replace(order, refunds=(Refund(Decimal("5"), HEADPHONES_DELIVERED_AT),),
                                      items=tuple(replace(item, unit_price=price, quantity=2) for item in order.items)))
    case = store.case("headphones")
    result = ce3.check(store, case)
    assert any(reason.startswith("Order #1012 ") and "it was refunded" in reason for reason in result.reasons)
    with pytest.raises(ValueError, match=re.escape(NOT_A_NUMBER)):
        purchase_history(store, case, result)


def test_a_total_doesnt_follow_the_order_the_items_come_back_in():
    # A price read from a float holds dozens of digits, and decimal rounds a running sum to 28 of them, so in some row
    # orders the plain sum comes out another way. The file adds them up exactly.
    rng = random.Random(1)
    prices = next(found for found in ([Decimal(round(rng.uniform(1, 500), 2)) for _ in range(3)] for _ in range(10000))
                  if len({sum((price * 1 for price in order), Decimal(0)) for order in permutations(found)}) > 1)
    items = tuple(replace(HEADPHONES_ORDER.items[0], sku=f"FH-X-{index}", unit_price=price)
                  for index, price in enumerate(prices))
    texts = {first_file(with_orders(o1109=replace(HEADPHONES_ORDER, items=order))).lines[0] for order in permutations(items)}
    assert len(texts) == 1


def test_amounts_keep_every_decimal_and_items_their_quantity():
    order = replace(HEADPHONES_ORDER, currency="KWD",
                    items=tuple(replace(item, unit_price=Decimal("380.125")) for item in HEADPHONES_ORDER.items))
    pair = replace(FERNHILL.order("1038"), items=tuple(replace(item, quantity=2) for item in FERNHILL.order("1038").items))
    lines = first_file(with_orders(o1109=order, o1038=pair, o1077=replace(FERNHILL.order("1077"), items=()))).lines
    assert lines[0] == ("Disputed order #1109, placed 2026-09-22: Over-ear studio headphones, serial FH-HP-03-55821, "
                        "KWD 380.125, paid with Visa ending 0008.")
    assert any(line.startswith(f"{ITEM}Order #1038, placed 2026-03-18, 188 days before the disputed order: 2 x Hard "
                               "travel case, USD 78.00. Same account") for line in lines)
    assert any(line.startswith(f"{ITEM}Order #1077, placed 2026-08-12, 41 days before the disputed order: no items, "
                               "USD 0.00. Same account") for line in lines)


THE_SIGN_UP_SAYS = f"{ITEM}It came from device {WARRANTY_SIGN_UP.device_id} and IP address"
ALEXS_ORDERS = "#1012 (2025-11-24), #1038 (2026-03-18), #1077 (2026-08-12) and #1109 (2026-09-22)"  # with when each was


@pytest.mark.parametrize("change, says", [
    ({"registered_at": HEADPHONES_DELIVERED_AT - timedelta(days=2)},
     f"Serial {HEADPHONES_SERIAL} was registered for warranty on 2026-09-24, 2 days before delivery:"),
    ({"registered_at": HEADPHONES_DELIVERED_AT + timedelta(hours=1)},
     f"Serial {HEADPHONES_SERIAL} was registered for warranty on 2026-09-26, on the day of delivery:"),
    ({"registered_at": datetime(2026, 9, 28, 21, 30, tzinfo=timezone(timedelta(hours=-7)))},
     f"Serial {HEADPHONES_SERIAL} was registered for warranty on 2026-09-29, 3 days after delivery:"),
    ({"email": "someone.else@example.com"}, f"{ITEM}It used someone.else@example.com, which isn't on the customer's "
                                             "account or orders."),
    ({"email": ""}, f"{ITEM}It has no email."),
    ({"sms_verified_phone": None}, f"{ITEM}It wasn't confirmed by a text message."),
    ({"sms_verified_phone": "+1 4155550199"}, f"{ITEM}A code sent by text message to +1 4155550199 confirmed it."),
    ({"sms_verified_phone": "+14155550123"},
     f"{ITEM}A code sent by text message to +14155550123, the cardholder's phone on order #1109, confirmed it."),
    ({"device_id": ALEX_DEVICE, "ip_address": ALEX_IP},
     f"{ITEM}It came from device {ALEX_DEVICE} and IP address {ALEX_IP}. The customer's orders from the same device: "
     f"{ALEXS_ORDERS}; orders from the same IP address: {ALEXS_ORDERS}."),
    ({"ip_address": ALEX_IP},
     f"{THE_SIGN_UP_SAYS} {ALEX_IP}. The customer's orders from the same IP address: {ALEXS_ORDERS}."),
    ({"ip_address": f"::ffff:{ALEX_IP}"},
     f"{THE_SIGN_UP_SAYS} ::ffff:{ALEX_IP}. The customer's orders from the same IP address: {ALEXS_ORDERS}."),
    ({"ip_address": ""}, f"{ITEM}It came from device {WARRANTY_SIGN_UP.device_id}. None of the customer's orders came from "
                         "that device."),
    ({"device_id": "unknown"}, f"{ITEM}It came from IP address {WARRANTY_SIGN_UP.ip_address}. None of the customer's "
                               "orders came from that IP address."),
    ({"device_id": "", "ip_address": "unknown"}, f"{ITEM}The registration doesn't show its device or IP address."),
    ({"device_id": "00000000-0000-0000-0000-000000000000", "ip_address": "0.0.0.0"},
     f"{ITEM}The registration doesn't show its device or IP address."),
    *(({"device_id": "unknown", "ip_address": own}, f"{ITEM}The registration doesn't show its device or IP address.")
      for own in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "169.254.10.20", "fe80::1", "172.17.0.1", "10.0.0.5",
                  "192.168.1.10", "100.64.0.1", "fd00::1")),
], ids=["before delivery", "on the day", "near midnight elsewhere", "another email", "no email", "no text message",
        "another phone", "the cardholder's phone written another way", "the disputed device", "the disputed IP",
        "the disputed IP written another way", "no IP", "no device", "no device or IP", "zeros for both",
        "loopback", "loopback in IPv6", "loopback mapped", "link-local", "link-local in IPv6", "private 172.16/12",
        "private 10/8", "private 192.168/16", "shared 100.64/10", "unique local fc00::/7"])
def test_the_warranty_file_says_where_the_sign_up_is_weaker(change, says):
    entry = replace(WARRANTY_SIGN_UP, **change)
    assert says in second_file(with_records(warranty=(entry,)), entry).lines


@pytest.mark.parametrize("written", [ALEX_DEVICE.replace("-", ""), ALEX_DEVICE.replace("-", "").upper(),
                                     f"{{{ALEX_DEVICE}}}", f"urn:uuid:{ALEX_DEVICE}"],
                         ids=["no hyphens", "upper case", "braces", "urn"])
def test_a_device_written_another_way_is_the_same_device(written):
    # The CE 3.0 check reads a UUID however it's written. A file that didn't would call a sign-up from the disputed
    # order's own device independent proof.
    assert ce3._comparable("device", written) == ce3._comparable("device", ALEX_DEVICE)
    same = f"The customer's orders from the same device: {ALEXS_ORDERS}."
    entry = replace(WARRANTY_SIGN_UP, device_id=written)
    assert f"{ITEM}It came from device {written} and IP address {entry.ip_address}. {same}" in second_file(
        with_records(warranty=(entry,)), entry).lines
    entry = replace(WARRANTY_SIGN_UP, device_id=ALEX_DEVICE)  # and the other way round: the order's record is written so
    store = replace(with_orders(o1109=replace(HEADPHONES_ORDER, device_id=written)), warranty=(entry,))
    assert f"{ITEM}It came from device {ALEX_DEVICE} and IP address {entry.ip_address}. {same}" in second_file(
        store, entry).lines


def test_an_ip_written_another_way_on_the_order_is_the_same_ip():
    # The other way round from the sign-up's: the order's own record holds the mapped form.
    entry = replace(WARRANTY_SIGN_UP, ip_address=ALEX_IP)
    store = replace(with_orders(o1109=replace(HEADPHONES_ORDER, ip_address=f"::ffff:{ALEX_IP}")), warranty=(entry,))
    assert (f"{THE_SIGN_UP_SAYS} {ALEX_IP}. The customer's orders from the same IP address: {ALEXS_ORDERS}."
            in second_file(store, entry).lines)


@pytest.mark.parametrize("other_on, says", [
    ((), "the email on the customer's orders since order #1012 (2025-11-24)."),
    (("1038", "1077", "1109"), "the email on the customer's order #1012 (2025-11-24)."),
    (("1038", "1077"), "the email on the customer's orders #1012 (2025-11-24) and #1109 (2026-09-22)."),
    (("1012", "1038", "1077"), "the email on the customer's order #1109 (2026-09-22)."),
    (("1012", "1038"), "the email on the customer's orders since order #1077 (2026-08-12)."),
], ids=["on every order", "changed after the first", "with a gap", "only on the disputed order", "on the last two"])
def test_the_email_line_says_since_only_when_every_later_order_has_it(other_on, says):
    store = replace(FERNHILL, orders=tuple(replace(order, email="alex.new@example.com") if order.number in other_on
                                           else order for order in FERNHILL.orders))
    assert f"{ITEM}It used {ALEX.email}, {says}" in second_file(store).lines


def test_a_sign_up_with_the_accounts_email_says_so():
    alex = replace(ALEX, email="alex.account@example.com")  # on the account, and on no order
    entry = replace(WARRANTY_SIGN_UP, email=alex.email)
    store = with_records(customers=tuple(alex if customer.id == ALEX.id else customer for customer in FERNHILL.customers),
                         warranty=(entry,))
    assert f"{ITEM}It used {alex.email}, the email on the customer's account." in second_file(store, entry).lines


def test_a_tie_first_seen_after_the_sign_up_shows_when():
    # The store first had the email and the device a month after the sign-up, and after the dispute opened: the dates
    # say so.
    device = "7d7d7d7d-1111-4222-8333-444455556666"
    later = replace(HEADPHONES_ORDER, number="1130", placed_at=datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc),
                    email="alex.new@example.com", device_id=device, items=())
    entry = replace(WARRANTY_SIGN_UP, email=later.email, device_id=device)
    lines = second_file(with_records(orders=(*FERNHILL.orders, later), warranty=(entry,)), entry).lines
    assert f"{ITEM}It used {later.email}, the email on the customer's order #1130 (2026-10-30)." in lines
    assert (f"{ITEM}It came from device {device} and IP address {entry.ip_address}. The customer's orders from the same "
            "device: #1130 (2026-10-30).") in lines


ON_1090 = " The store's records show a fraud dispute on order #1090 (2026-08-29)."


@pytest.mark.parametrize("dispute, fraud", [(None, ""), ("FRAUD", ON_1090)], ids=["no dispute", "a fraud dispute"])
def test_an_order_by_someone_else_on_the_same_card_is_named_apart(dispute, fraud):
    # Robin once paid with Alex's card, from Robin's own device, and the sign-up came from that device with Robin's
    # email. That ties the sign-up to Robin's order, not to Alex, the customer, so the file says both.
    robins = replace(FERNHILL.order("1098"), number="1090", card_fingerprint="card-alex", card_last4="0008",
                     prior_dispute=dispute)
    entry = replace(WARRANTY_SIGN_UP, email=robins.email, device_id=robins.device_id, ip_address=robins.ip_address)
    lines = second_file(with_records(orders=(*FERNHILL.orders, robins), warranty=(entry,)), entry).lines
    assert (f"{ITEM}It used {robins.email}, which isn't on the customer's account or orders. Orders on this card by other "
            f"customers, with the same email: #1090 (2026-08-29).{fraud}") in lines
    assert (f"{ITEM}It came from device {robins.device_id} and IP address {robins.ip_address}. None of the customer's "
            "orders came from that device or that IP address. Orders on this card by other customers, from the same "
            f"device: #1090 (2026-08-29); from the same IP address: #1090 (2026-08-29).{fraud}") in lines
    # On another card, Robin's order ties nothing, and the file names nothing. Nor does a card with no fingerprint on
    # record: two "unknown"s aren't one card.
    alone = f"{ITEM}It used {robins.email}, which isn't on the customer's account or orders."
    assert alone in second_file(with_records(warranty=(entry,)), entry).lines
    for blank in ("unknown", "(none)", "--", "N/A."):  # however the blank is written
        unknown = with_orders(with_records(orders=(*FERNHILL.orders, replace(robins, card_fingerprint=blank)),
                                           warranty=(entry,)), o1109=replace(HEADPHONES_ORDER, card_fingerprint=blank))
        assert alone in second_file(unknown, entry).lines, blank


ALEXS_NUMBERS = ("1012", "1038", "1077", "1109")


@pytest.mark.parametrize("blank", ["unknown", "(none)", "--", "N/A.", "#N/A", "—", "?"])
def test_a_card_with_no_fingerprint_is_no_card_however_the_blank_is_written(blank):
    # Alex's orders all hold the same blank for the card. Two blanks aren't one card, so no earlier order is "paid with
    # the same card", and there's no purchase history to send.
    store = with_orders(**{f"o{number}": replace(FERNHILL.order(number), card_fingerprint=blank)
                           for number in ALEXS_NUMBERS})
    with pytest.raises(ValueError, match="cites no earlier order"):
        first_file(store)


def test_a_blank_device_ties_the_sign_up_to_no_order():
    # The sign-up and Alex's orders all hold "(none)" for the device: that's no device, so nothing matches on it.
    entry = replace(WARRANTY_SIGN_UP, device_id="(none)")
    store = with_orders(with_records(warranty=(entry,)),
                        **{f"o{number}": replace(FERNHILL.order(number), device_id="(none)") for number in ALEXS_NUMBERS})
    assert (f"{ITEM}It came from IP address {entry.ip_address}. None of the customer's orders came from that IP "
            "address.") in second_file(store, entry).lines


def test_none_came_from_it_is_said_only_of_what_the_orders_show():
    # Alex's orders hold no device: the sign-up's device can't be compared, so the file doesn't say none came from it.
    store = with_orders(**{f"o{number}": replace(FERNHILL.order(number), device_id="unknown") for number in ALEXS_NUMBERS})
    assert (f"{ITEM}It came from device {WARRANTY_SIGN_UP.device_id} and IP address {WARRANTY_SIGN_UP.ip_address}. None of "
            "the customer's orders came from that IP address. The customer's orders show no device to compare it with."
            ) in second_file(store).lines
    store = with_orders(store, **{f"o{number}": replace(store.order(number), ip_address="") for number in ALEXS_NUMBERS})
    assert (f"{ITEM}It came from device {WARRANTY_SIGN_UP.device_id} and IP address {WARRANTY_SIGN_UP.ip_address}. The "
            "customer's orders show no device or buyer's IP address to compare it with.") in second_file(store).lines


NO_DEVICE_ON_1109 = ("Order #1109 (2026-09-22) shows no device, and none of the customer's other orders came from that "
                     "device.")


@pytest.mark.parametrize("orders, says", [
    ({"1109": {"device_id": "unknown"}}, f"None of the customer's orders came from that IP address. {NO_DEVICE_ON_1109}"),
    ({"1109": {"device_id": "", "ip_address": ""}},
     "Order #1109 (2026-09-22) shows no device or buyer's IP address, and none of the customer's other orders came from "
     "that device or that IP address."),
    ({"1038": {"ip_address": "10.0.0.5"}, "1109": {"ip_address": "172.17.0.1"}},
     "None of the customer's orders came from that device. Orders #1038 (2026-03-18) and #1109 (2026-09-22) show no "
     "buyer's IP address, and none of the customer's other orders came from that IP address."),
    ({"1012": {"ip_address": ""}, "1109": {"device_id": "00000000-0000-0000-0000-000000000000"}},
     f"{NO_DEVICE_ON_1109} Order #1012 (2025-11-24) shows no buyer's IP address, and none of the customer's other orders "
     "came from that IP address."),
    ({number: {"ip_address": ""} for number in ALEXS_NUMBERS} | {"1109": {"device_id": "", "ip_address": ""}},
     f"{NO_DEVICE_ON_1109} The customer's orders show no buyer's IP address to compare it with."),
    ({"1109": {"ip_address": "localhost.localdomain"}},
     "None of the customer's orders came from that device. Order #1109 (2026-09-22) shows no buyer's IP address, and none "
     "of the customer's other orders came from that IP address."),
    ({"1077": {"device_id": "", "prior_dispute": "FRAUD"}},
     "None of the customer's orders came from that IP address. Order #1077 (2026-08-12) shows no device, and none of the "
     "customer's other orders came from that device. The store's records show a fraud dispute on order #1077 "
     "(2026-08-12)."),
], ids=["the disputed order shows no device", "nor an IP", "two show a proxy's IP", "each misses another detail",
        "and no order shows an IP", "the server's name for an IP", "a fraud dispute on an order named"])
def test_none_came_from_it_isnt_said_of_an_order_that_shows_nothing(orders, says):
    # The disputed order's device is what the sign-up must be independent of. When it isn't on record, "none of the
    # customer's orders came from that device" says more than the records do, so the orders that show none are named.
    store = replace(FERNHILL, orders=tuple(replace(order, **orders.get(order.number, {})) for order in FERNHILL.orders))
    assert f"{THE_SIGN_UP_SAYS} {WARRANTY_SIGN_UP.ip_address}. {says}" in second_file(store).lines


@pytest.mark.parametrize("sign_up, orders, says", [
    ({"ip_address": f"{ALEX_IP}."}, {}, f"The customer's orders from the same IP address: {ALEXS_ORDERS}."),
    ({"device_id": f"{ALEX_DEVICE}.", "ip_address": ""}, {}, f"The customer's orders from the same device: {ALEXS_ORDERS}."),
    ({"ip_address": ALEX_IP}, {"ip_address": f"{ALEX_IP}."},
     f"The customer's orders from the same IP address: {ALEXS_ORDERS}."),
], ids=["the sign-up's IP", "the sign-up's device", "the orders' IP"])
def test_a_full_stop_at_the_end_of_a_device_or_ip_hides_no_tie(sign_up, orders, says):
    # "203.0.113.7." is that IP, cut from a sentence, as the CE 3.0 check reads it: the file shows the dot, so it must
    # count the match the bank can see.
    store = replace(FERNHILL, orders=tuple(replace(order, **orders) if order.number in ALEXS_NUMBERS else order
                                           for order in FERNHILL.orders))
    entry = replace(WARRANTY_SIGN_UP, **sign_up)
    lines = second_file(replace(store, warranty=(entry,)), entry).lines
    assert any(line.startswith(f"{ITEM}It came from") and line.endswith(says) for line in lines)


@pytest.mark.parametrize("ip", ["172.17.0.1", "localhost.localdomain"])
def test_a_detail_that_says_nothing_to_the_check_isnt_shown(ip):
    # A proxy's address on the disputed order is no buyer's, so CE 3.0 can't compare it, and the file doesn't show it.
    lines = first_file(with_orders(o1109=replace(HEADPHONES_ORDER, ip_address=ip))).lines
    assert not any(line.startswith("IP address") for line in lines)
    assert f"Device ID: {ALEX_DEVICE}." in lines


NEW_EMAIL, NEW_DEVICE = "alex.new@example.com", "7d7d7d7d-1111-4222-8333-444455556666"
ON_1077 = "The store's records show a fraud dispute on order #1077 (2026-08-12)."  # dated, like every order a line names


@pytest.mark.parametrize("orders, sign_up, says", [
    ({"1077": {"email": NEW_EMAIL, "prior_dispute": "FRAUD"}, "1109": {"email": NEW_EMAIL}}, {"email": NEW_EMAIL},
     f"{ITEM}It used {NEW_EMAIL}, the email on the customer's orders since order #1077 (2026-08-12). {ON_1077}"),
    ({"1077": {"email": NEW_EMAIL, "prior_dispute": " fraud "}}, {"email": NEW_EMAIL},
     f"{ITEM}It used {NEW_EMAIL}, the email on the customer's order #1077 (2026-08-12). {ON_1077}"),
    ({"1077": {"device_id": NEW_DEVICE, "prior_dispute": "FRAUD"}}, {"device_id": NEW_DEVICE},
     f"{ITEM}It came from device {NEW_DEVICE} and IP address {WARRANTY_SIGN_UP.ip_address}. The customer's orders from "
     f"the same device: #1077 (2026-08-12). {ON_1077}"),
    ({"1012": {"prior_dispute": "FRAUD"}, "1077": {"prior_dispute": "FRAUD"}, "1109": {"prior_dispute": "FRAUD"}},
     {"ip_address": ALEX_IP}, f"{THE_SIGN_UP_SAYS} {ALEX_IP}. The customer's orders from the same IP address: "
                              f"{ALEXS_ORDERS}. The store's records show fraud disputes on orders #1012 (2025-11-24) "
                              "and #1077 (2026-08-12)."),
    ({"1077": {"email": NEW_EMAIL, "prior_dispute": "NON_FRAUD"}, "1109": {"email": NEW_EMAIL}}, {"email": NEW_EMAIL},
     f"{ITEM}It used {NEW_EMAIL}, the email on the customer's orders since order #1077 (2026-08-12)."),
], ids=["the email since it", "the email only on it", "its device", "two, and the disputed order's own",
        "a dispute that wasn't for fraud"])
def test_a_tie_through_an_order_disputed_as_fraud_says_so(orders, sign_up, says):
    # To the bank, an order its cardholder disputed as fraud is someone else's, so CE 3.0 never cites one. A sign-up with
    # its email, device or IP points to whoever placed it, so the file says what the store's records show about it.
    store = replace(FERNHILL, orders=tuple(replace(order, **orders.get(order.number, {})) for order in FERNHILL.orders))
    entry = replace(WARRANTY_SIGN_UP, **sign_up)
    assert says in second_file(replace(store, warranty=(entry,)), entry).lines


# ----------------------------------------------------------------------------- the repository
def test_git_keeps_pdfs_as_bytes():
    # A PDF's xref holds byte offsets, so a line-ending change breaks it. With core.autocrlf, git archive and a checkout
    # would change them: PDFs have no NUL byte, so git takes them for text.
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "*.pdf binary" in rules and "*.jpg binary" in rules
