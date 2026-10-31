import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
from src.core.config import settings
from src.services.screenshot_storage import save_screenshot, get_screenshot

def test_screenshot_storage_local_fallback(tmp_path, monkeypatch):
    # Set the upload directory to a temporary path to avoid cluttering local disk
    test_uploads_dir = tmp_path / "deposits"
    test_uploads_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.services.screenshot_storage.UPLOADS_DIR", test_uploads_dir)
    monkeypatch.setattr(settings, "azure_storage_connection_string", None)

    filename = "test_local.png"
    content = b"fake_png_data"

    # Test save
    save_screenshot(filename, content)
    local_file = test_uploads_dir / filename
    assert local_file.is_file()
    assert local_file.read_bytes() == content

    # Test get
    retrieved_content, mime = get_screenshot(filename)
    assert retrieved_content == content
    assert mime == "image/png"


def test_screenshot_storage_azure(tmp_path, monkeypatch):
    # Set the upload directory to a temporary path to avoid cluttering local disk
    test_uploads_dir = tmp_path / "deposits"
    test_uploads_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.services.screenshot_storage.UPLOADS_DIR", test_uploads_dir)
    monkeypatch.setattr(settings, "azure_storage_connection_string", "DefaultEndpointsProtocol=https;AccountName=test;AccountKey=key;EndpointSuffix=core.windows.net")
    monkeypatch.setattr(settings, "azure_storage_container_name", "test-container")

    filename = "test_azure.jpg"
    content = b"fake_jpg_data"

    # Create mock clients
    mock_blob_client = MagicMock()
    mock_container_client = MagicMock()
    mock_blob_service_client = MagicMock()

    mock_blob_service_client.get_container_client.return_value = mock_container_client
    mock_container_client.get_blob_client.return_value = mock_blob_client
    mock_blob_service_client.get_blob_client.return_value = mock_blob_client

    # Set exists() and download_blob() mock returns
    mock_blob_client.exists.return_value = True
    mock_stream = MagicMock()
    mock_stream.readall.return_value = content
    mock_blob_client.download_blob.return_value = mock_stream

    with patch("azure.storage.blob.BlobServiceClient.from_connection_string", return_value=mock_blob_service_client):
        # Test save
        save_screenshot(filename, content)
        mock_blob_service_client.get_container_client.assert_called_once_with("test-container")
        mock_container_client.get_blob_client.assert_called_once_with(filename)
        mock_blob_client.upload_blob.assert_called_once_with(content, overwrite=True)

        # Test get
        retrieved_content, mime = get_screenshot(filename)
        assert retrieved_content == content
        assert mime == "image/jpeg"
        mock_blob_service_client.get_blob_client.assert_called_once_with(container="test-container", blob=filename)
        mock_blob_client.download_blob.assert_called_once()
