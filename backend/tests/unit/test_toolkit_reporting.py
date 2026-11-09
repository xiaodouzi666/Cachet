from backend.app.paypal.toolkit import reporting_filters


def test_toolkit_search_dates_use_whole_seconds_and_keep_the_requested_timezone():
    assert reporting_filters({
        "start_date": "2026-10-09T11:43:23.789056Z",
        "end_date": "2026-11-09T13:43:23.789056+02:00",
        "page_size": 1,
        "transaction_status": "None",
    }) == {
        "start_date": "2026-10-09T11:43:23Z",
        "end_date": "2026-11-09T13:43:23+02:00",
        "page_size": 1,
    }
