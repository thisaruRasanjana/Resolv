import json
from unittest.mock import patch, AsyncMock

import pytest

from src.queue import publish_event, move_to_dead_letter


@pytest.mark.asyncio
@patch("src.queue.get_redis")
async def test_publish_event(mock_get_redis):
    mock_redis = AsyncMock()
    mock_redis.xadd.return_value = "123-0"
    mock_get_redis.return_value = mock_redis

    event_data = {"key": "value"}
    msg_id = await publish_event(event_data)
    
    assert msg_id == "123-0"
    mock_redis.xadd.assert_called_once()
    args, kwargs = mock_redis.xadd.call_args
    assert args[0] == "resolv:events"
    assert json.loads(args[1]["data"]) == event_data


@pytest.mark.asyncio
@patch("src.queue.get_redis")
async def test_move_to_dead_letter(mock_get_redis):
    mock_redis = AsyncMock()
    mock_get_redis.return_value = mock_redis

    event_data = {"key": "value"}
    error = "Test error"
    
    await move_to_dead_letter(event_data, error)
    
    mock_redis.xadd.assert_called_once()
    args, kwargs = mock_redis.xadd.call_args
    assert args[0] == "resolv:dead-letters"
    assert json.loads(args[1]["data"]) == event_data
    assert args[1]["error"] == error
