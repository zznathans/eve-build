from app.db import rabbitmq


def test_price_refresh_job_message_roundtrips_through_json() -> None:
    message = rabbitmq.PriceRefreshJobMessage(refresh_id="run-1")

    decoded = rabbitmq.decode_price_refresh_job(rabbitmq.encode_price_refresh_job(message))

    assert decoded == message


def test_price_refresh_result_message_roundtrips_through_json() -> None:
    message = rabbitmq.PriceRefreshResultMessage(
        refresh_id="run-1",
        prices=[{"type_id": 34, "adjusted_price": 5.12, "average_price": 5.5}],
    )

    decoded = rabbitmq.decode_price_refresh_result(rabbitmq.encode_price_refresh_result(message))

    assert decoded == message


def test_tracked_info_refresh_job_message_roundtrips_through_json() -> None:
    message = rabbitmq.TrackedInfoRefreshJobMessage(kind="character_assets", character_id=1)

    decoded = rabbitmq.decode_tracked_info_refresh_job(
        rabbitmq.encode_tracked_info_refresh_job(message)
    )

    assert decoded == message


def test_tracked_info_refresh_job_message_roundtrips_with_corporation_id() -> None:
    message = rabbitmq.TrackedInfoRefreshJobMessage(
        kind="corporation_assets", character_id=1, corporation_id=2
    )

    decoded = rabbitmq.decode_tracked_info_refresh_job(
        rabbitmq.encode_tracked_info_refresh_job(message)
    )

    assert decoded == message
