import threading
import unittest.mock

from graphrefly.core.node import MessageType
from graphrefly.extra.adapters import from_http
from graphrefly.extra.sources import first_where


def test_from_http_get():
    mock_response_data = b'{"foo": "bar"}'

    with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = unittest.mock.MagicMock()
        mock_response.read.return_value = mock_response_data
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        bundle = from_http("https://example.com")
        unsub = bundle.node.subscribe(lambda _: None)

        first_where(bundle.status, lambda v: v == "completed", timeout=5.0)

        assert bundle.fetch_count.get() == 1
        assert bundle.node.get() == {"foo": "bar"}

        mock_urlopen.assert_called_once()
        args, _kwargs = mock_urlopen.call_args
        req = args[0]
        assert req.full_url == "https://example.com"
        assert req.get_method() == "GET"
        unsub()


def test_from_http_post():
    mock_response_data = b'{"success": true}'
    body = {"test": 123}

    with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = unittest.mock.MagicMock()
        mock_response.read.return_value = mock_response_data
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        bundle = from_http("https://example.com/post", method="POST", body=body)
        unsub = bundle.node.subscribe(lambda _: None)

        first_where(bundle.status, lambda v: v == "completed", timeout=5.0)

        assert bundle.fetch_count.get() == 1
        assert bundle.node.get() == {"success": True}

        args, _kwargs = mock_urlopen.call_args
        req = args[0]
        assert req.get_method() == "POST"
        assert req.data == b'{"test": 123}'
        unsub()


def test_from_http_error():
    with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = Exception("Connection refused")

        bundle = from_http("https://example.com/fail")
        # Wire observer BEFORE the producer starts (subscribe activates the fetch thread).
        errored = threading.Event()
        unsub_status = bundle.status.subscribe(
            lambda msgs: [errored.set() for m in msgs if m[0] is MessageType.DATA and m[1] == "errored"]
        )
        unsub = bundle.node.subscribe(lambda _: None)

        errored.wait(timeout=5.0)
        unsub_status()

        assert bundle.status.get() == "errored"
        assert str(bundle.error.get()) == "Connection refused"
        unsub()


def test_from_http_completes_after_fetch():
    """One-shot: emits DATA then COMPLETE."""
    mock_response_data = b'{"done": true}'

    with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = unittest.mock.MagicMock()
        mock_response.read.return_value = mock_response_data
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        bundle = from_http("https://example.com")
        unsub = bundle.node.subscribe(lambda _: None)

        first_where(bundle.status, lambda v: v == "completed", timeout=5.0)

        assert bundle.status.get() == "completed"
        assert bundle.node.get() == {"done": True}
        assert bundle.fetch_count.get() == 1
        assert bundle.last_updated.get() > 0
        unsub()


def test_from_http_transform_receives_raw_bytes():
    mock_response_data = b'{"foo": "bar"}'
    seen: list[bytes] = []

    def transform(raw: bytes):
        seen.append(raw)
        return {"raw_len": len(raw)}

    with unittest.mock.patch("urllib.request.urlopen") as mock_urlopen:
        mock_response = unittest.mock.MagicMock()
        mock_response.read.return_value = mock_response_data
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        bundle = from_http("https://example.com", transform=transform)
        unsub = bundle.node.subscribe(lambda _: None)
        first_where(bundle.status, lambda v: v == "completed", timeout=5.0)

        assert seen == [mock_response_data]
        assert bundle.node.get() == {"raw_len": len(mock_response_data)}
        unsub()
