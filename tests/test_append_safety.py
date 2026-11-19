from unittest.mock import MagicMock

import pytest

import checks_writer as cw
import sheets_util


def test_id_ignores_cache_and_refuses_partial_book(monkeypatch):
    api = MagicMock()
    monkeypatch.setattr(sheets_util, "sheets", lambda: api)
    monkeypatch.setattr(cw, "tutor_id_prefix", lambda name: "")
    monkeypatch.setattr(cw, "id_prefix", lambda: "1911")
    monkeypatch.setattr(cw, "load_config", lambda: {"sheets": {
        "history": "hist", "recept": "checks", "recept_checks_sheet": "Чеки"}})
    monkeypatch.setattr(cw, "_IDS_CACHE", {"ts": float("inf"), "ids": ["191199"]})
    sheet = api.spreadsheets.return_value
    sheet.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "История"}}, {"properties": {"title": "История 2"}}]}
    results = sheet.values.return_value.batchGet.return_value.execute
    checks = {"valueRanges": [{"range": "Чеки!L2:L10", "values": [["19112"]]}]}
    history = {"valueRanges": [{"range": "История!L2:L10"}, {
        "range": "'История 2'!L2:L10", "values": [["19118"]]}]}
    results.side_effect = [checks, history, checks, {"valueRanges": [history["valueRanges"][0]]}]
    assert cw.next_lesson_id("Чеки", "Test", "checks") == "19119"
    with pytest.raises(RuntimeError, match="не полностью"):
        cw.next_lesson_id("Чеки", "Test", "checks")


def test_failed_read_does_not_select_an_occupied_row(monkeypatch):
    def timeout(*args):
        raise TimeoutError("simulated")

    monkeypatch.setattr(cw, "get_values", timeout)
    with pytest.raises(TimeoutError):
        cw.find_next_free_row("Lessons", "book")


@pytest.mark.parametrize("confirmed", [True, False])
def test_append_locks_through_write_and_sets_payment_colour(monkeypatch, confirmed):
    api = MagicMock()
    sheet = api.spreadsheets.return_value
    sheet.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"sheetId": 9, "title": "Lessons"}}]}
    monkeypatch.setattr(sheets_util, "sheets", lambda: api)
    monkeypatch.setattr(cw, "_target", lambda tutor: ("book", "Lessons"))
    monkeypatch.setattr(cw, "ensure_target_sheet", lambda *args: None)
    monkeypatch.setattr(cw, "find_next_free_row", lambda *args: 4)
    monkeypatch.setattr(cw, "next_lesson_id", lambda *args: "123")
    monkeypatch.setattr(cw, "_text_color", lambda *args: cw.WHITE)
    monkeypatch.setattr(cw, "get_tutor_color", lambda tutor: None)
    monkeypatch.setattr(cw, "log", lambda msg: None)

    def write(book, target, values, **options):
        assert cw._ID_LOCK.locked()
        assert target == "'Lessons'!A4:L4" and values[0][11] == "123"
        requests = sheet.batchUpdate.call_args.kwargs["body"]["requests"]
        green = [r["repeatCell"] for r in requests if
                 r.get("repeatCell", {}).get("range", {}).get("startColumnIndex") == 5]
        if confirmed:
            assert green[0]["cell"]["userEnteredFormat"]["textFormat"]["foregroundColor"] == cw.GREEN
        else:
            assert not green

    monkeypatch.setattr(cw, "update_values", write)
    assert cw.append_check({"сумма": 1200, "оплачено": confirmed}) == 4
