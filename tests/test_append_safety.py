import datetime as dt
from unittest.mock import MagicMock

import pytest

import checks_writer as cw
import sheets_util


def test_id_ignores_cache_and_refuses_partial_book(monkeypatch):
    api = MagicMock()
    monkeypatch.setattr(sheets_util, "sheets", lambda: api)
    monkeypatch.setattr(cw, "tutor_id_prefix", lambda name: "")
    monkeypatch.setattr(cw, "load_config", lambda: {"sheets": {
        "history": "hist", "recept": "checks", "recept_checks_sheet": "Чеки"}})
    monkeypatch.setattr(cw, "_IDS_CACHE", {"ts": float("inf"), "ids": ["191199"]})
    sheet = api.spreadsheets.return_value
    sheet.get.return_value.execute.return_value = {
        "sheets": [{"properties": {"title": "История"}}, {"properties": {"title": "История 2"}}]}
    results = sheet.values.return_value.batchGet.return_value.execute
    checks = {"valueRanges": [{"range": "Чеки!L2:L10", "values": [["AL191101"]]}]}
    history = {"valueRanges": [{"range": "История!L2:L10"}, {
        "range": "'История 2'!L2:L10", "values": [["AL191102"]]}]}
    results.side_effect = [checks, history, checks, {"valueRanges": [history["valueRanges"][0]]}]
    assert cw.next_lesson_id("Чеки", "Test", "checks", dt.date(2026, 11, 19)) == "AL191103"
    with pytest.raises(RuntimeError, match="не полностью"):
        cw.next_lesson_id("Чеки", "Test", "checks", dt.date(2026, 11, 19))


def test_failed_read_does_not_select_an_occupied_row(monkeypatch):
    def timeout(*args):
        raise TimeoutError("simulated")

    monkeypatch.setattr(cw, "get_values", timeout)
    with pytest.raises(TimeoutError):
        cw.find_next_free_row("Lessons", "book")


@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("background", [None, cw.BLACK, cw.WHITE])
def test_append_locks_through_write_and_sets_neutral_text(monkeypatch, confirmed, background):
    api = MagicMock()
    sheet = api.spreadsheets.return_value
    sheet.get.return_value.execute.return_value = {
        "properties": {
            "defaultFormat": {} if background is None else {"backgroundColor": background},
            "spreadsheetTheme": {"themeColors": [
                {"colorType": "ACCENT1", "color": {"rgbColor": cw.BLACK}},
            ]},
        },
        "sheets": [{
            "properties": {"sheetId": 9, "title": "Lessons"},
            "data": [{"rowData": [{"values": [
                {"effectiveFormat": {"backgroundColorStyle": {"rgbColor": cw.WHITE}}},
                {"effectiveFormat": {"backgroundColorStyle": {"rgbColor": {}}}},
                {"effectiveFormat": {"backgroundColorStyle": {"themeColor": "ACCENT1"}}},
            ]}]}],
        }],
    }
    monkeypatch.setattr(sheets_util, "sheets", lambda: api)
    monkeypatch.setattr(cw, "_target", lambda tutor: ("book", "Lessons"))
    monkeypatch.setattr(cw, "ensure_target_sheet", lambda *args: None)
    monkeypatch.setattr(cw, "find_next_free_row", lambda *args: 4)
    monkeypatch.setattr(cw, "next_lesson_id", lambda *args: "123")
    monkeypatch.setattr(cw, "get_tutor_color", lambda tutor: cw.GREEN)
    monkeypatch.setattr(cw, "log", lambda msg: None)

    def write(book, target, values, **options):
        assert cw._ID_LOCK.locked()
        assert target == "'Lessons'!A4:L4" and values[0][11] == "123"
        assert sheet.get.call_args.kwargs["ranges"] == ["'Lessons'!A4:L4"]
        requests = sheet.batchUpdate.call_args.kwargs["body"]["requests"]
        colors = requests[0]["updateCells"]
        assert colors["start"] == {"sheetId": 9, "rowIndex": 3, "columnIndex": 0}
        assert colors["fields"] == cw._TEXT_COLOR_FIELDS
        default = cw.WHITE if background == cw.BLACK else cw.BLACK
        expected_colors = [cw.BLACK, cw.WHITE, cw.WHITE] + [default] * 9
        cells = colors["rows"][0]["values"]
        assert [c["userEnteredFormat"]["textFormat"] for c in cells] == [
            {"foregroundColor": color, "foregroundColorStyle": {"rgbColor": color}}
            for color in expected_colors
        ]

    monkeypatch.setattr(cw, "update_values", write)
    assert cw.append_check({"сумма": 1200, "оплачено": confirmed}) == 4
    sheet.batchUpdate.assert_called_once()


def test_manual_payment_status_can_recolor_neutral_sum(monkeypatch):
    api = MagicMock()
    monkeypatch.setattr(sheets_util, "sheets", lambda: api)
    monkeypatch.setattr(cw, "_find_lesson_anywhere", lambda key: ("book", "Lessons", 4))
    monkeypatch.setattr(cw, "_sheet_gid", lambda *args: 9)
    monkeypatch.setattr(cw, "log", lambda msg: None)
    assert cw.set_lesson_paid("AL201101", True)
    changed = api.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"]["requests"][0]
    change = changed["repeatCell"]
    assert change["range"]["startColumnIndex"] == 5
    assert change["fields"] == cw._TEXT_COLOR_FIELDS
    assert change["cell"]["userEnteredFormat"]["textFormat"]["foregroundColorStyle"] == {
        "rgbColor": cw.GREEN,
    }
