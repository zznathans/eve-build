from app.db import rabbitmq


def test_scrape_job_message_roundtrips_through_json() -> None:
    message = rabbitmq.ScrapeJobMessage(region_id=10000002, scrape_run_id="run-1")

    decoded = rabbitmq.decode_scrape_job(rabbitmq.encode_scrape_job(message))

    assert decoded == message


def test_order_message_roundtrips_through_json() -> None:
    message = rabbitmq.OrderMessage(
        region_id=10000002,
        scrape_run_id="run-1",
        order={"order_id": 1, "type_id": 34, "price": 5.5},
    )

    decoded = rabbitmq.decode_order(rabbitmq.encode_order(message))

    assert decoded == message
