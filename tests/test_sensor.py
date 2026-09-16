import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.librus_apix.sensor import LibrusDataUpdateCoordinator
from custom_components.librus_apix.__init__ import LibrusApiClient

@pytest.fixture
def mock_client():
    client = MagicMock(spec=LibrusApiClient)
    client.async_authenticate = AsyncMock(return_value=True)
    client.async_get_student_information = AsyncMock()
    client.async_get_grades = AsyncMock()
    client.async_get_messages = AsyncMock(return_value=[])
    client.async_get_homework = AsyncMock(return_value=[])
    client.async_get_schedule = AsyncMock(return_value=[])
    client.async_get_timetable = AsyncMock(return_value=[])
    client.async_get_attendance = AsyncMock(return_value=[])
    client.async_get_announcements = AsyncMock(return_value=[])
    return client

@pytest.fixture
def coordinator(hass, mock_client):
    # Dummy config entry
    config_entry = MagicMock()
    config_entry.entry_id = "test_123"
    
    # Utworz koordynator
    coord = LibrusDataUpdateCoordinator(hass, mock_client)
    return coord

@pytest.mark.asyncio
async def test_update_missing_grades(coordinator, mock_client):
    """Test czy pobranie None przy ocenach nie blokuje aktualizacji gdy cache jest puste."""
    
    # Mock zwrocenia None dla ocen (np. konto przedszkolaka)
    mock_client.async_get_grades.return_value = None
    
    # Mock student info
    student_info = MagicMock()
    student_info.name = "Jan Kowalski"
    student_info.class_name = "1A"
    mock_client.async_get_student_information.return_value = student_info
    
    # Upewnijmy sie ze koordynator ma puste dane
    coordinator.data = None
    
    # Zamiast wyrzucac wyjatek, powinno zwrocic puste tablice z fallbacku
    result = await coordinator._async_update_data()
    
    # Oceny powinny być pustą listą a nie rzucać UpdateFailed
    assert result["oceny"] == []
    assert result["student_info"].name == "Jan Kowalski"

@pytest.mark.asyncio
async def test_student_info_getattr_fix(coordinator, mock_client):
    """Test upewniajacy sie ze zabezpieczony dostep przez getattr nie rzuca wyjatkiem."""
    
    # Normalne oceny
    mock_client.async_get_grades.return_value = []
    
    # Symulujemy zwrocenie obiektu StudentInformation przez APIX
    class FakeStudentInformation:
        def __init__(self, name):
            self.name = name
            self.class_name = "2B"
    
    student_info = FakeStudentInformation("Piotr Nowak")
    mock_client.async_get_student_information.return_value = student_info
    
    result = await coordinator._async_update_data()
    
    assert result["student_info"].name == "Piotr Nowak"
    # To potwierdza ze wywolania getattr(student_info, 'name') wewnatrz integracji nie zglosza AttributeError


@pytest.mark.asyncio
async def test_update_zerowka_account(coordinator, mock_client):
    """Test czy konto zerowki (brak ocen, brak student_info) poprawnie sie aktualizuje."""
    mock_client.async_get_student_information.return_value = None
    mock_client.async_get_grades.return_value = []
    mock_client.async_get_messages.return_value = [
        {"author": "Nauczyciel", "title": "Wycieczka", "date": "2026-09-16", "href": "msg1", "unread": True, "has_attachment": False}
    ]
    mock_client.async_get_homework.return_value = []
    mock_client.async_get_schedule.return_value = []
    mock_client.async_get_timetable.return_value = []
    mock_client.async_get_attendance.return_value = []
    mock_client.async_get_announcements.return_value = []

    coordinator.data = None
    result = await coordinator._async_update_data()

    assert result["student_info"] is None
    assert result["oceny"] == []
    assert len(result["wiadomosci"]) == 1
    assert result["wiadomosci"][0]["tytul"] == "Wycieczka"
