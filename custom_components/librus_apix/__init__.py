"""The Librus APIX integration."""

import asyncio
import logging
import traceback
from datetime import date
from typing import Dict, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers import config_validation as cv

from librus_apix.client import Client, new_client
from librus_apix.exceptions import TokenError

from .const import DOMAIN, SCAN_INTERVAL

_LOGGER = logging.getLogger(__name__)


def _current_semester() -> int:
    """Zwroc numer biezacego semestru (1 lub 2) wg polskiego roku szkolnego.

    Semestr 1: wrzesien (9) - styczen (1)
    Semestr 2: luty (2) - czerwiec (6)
    Lipiec-sierpien to wakacje - zwracamy 2 (ostatni semestr roku).
    """
    m = date.today().month
    return 1 if m >= 9 else 2

PLATFORMS = ["sensor", "calendar", "todo", "button"]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Required(CONF_USERNAME): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


class LibrusApiClient:
    """Class to interface with the Librus API."""

    def __init__(self, username: str, password: str, options: dict = None):
        """Initialize the client."""
        self.username = username
        self.password = password
        self.options = options or {}
        self._client: Client = None
        self._token = None
        self._last_auth_time: float = 0.0
        self._auth_lock = asyncio.Lock()

    def _reset_auth(self) -> None:
        """Reset authentication state to force re-authentication on next call."""
        self._client = None
        self._token = None
        self._last_auth_time = 0.0

    async def async_authenticate(self):
        """Authenticate with Librus API."""
        import time
        async with self._auth_lock:
            # If authenticated very recently, reuse current session
            if self._client and self._token and (time.monotonic() - self._last_auth_time < 10):
                return True
            try:
                loop = asyncio.get_running_loop()
                self._client = await loop.run_in_executor(None, new_client)
                self._token = await loop.run_in_executor(
                    None, self._client.get_token, self.username, self.password
                )
                self._last_auth_time = time.monotonic()
                _LOGGER.debug("Authentication successful for %s", self.username)
                return True
            except Exception as ex:
                _LOGGER.error("Authentication failed: %s\n%s", ex, traceback.format_exc())
                self._reset_auth()
                return False

    async def async_get_grades(self):
        """Get grades from Librus."""
        import time
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return []
                client = self._client

                from librus_apix.grades import get_grades

                loop = asyncio.get_running_loop()
                grades_result = await loop.run_in_executor(
                    None, get_grades, client, "all"
                )
                if not grades_result:
                    return []
                numeric_grades, average_grades, descriptive_grades = grades_result

                current_sem = _current_semester()
                _LOGGER.debug("Filtrowanie ocen dla semestru %d", current_sem)

                # Process all grades
                all_grades = []

                # Process numeric grades (only current semester)
                if numeric_grades:
                    for subject_grades in numeric_grades:
                        for subject, grades_list in subject_grades.items():
                            for grade in grades_list:
                                if getattr(grade, "semester", None) != current_sem:
                                    continue
                                all_grades.append({
                                    'subject': subject,
                                    'grade': getattr(grade, 'grade', ''),
                                    'date': getattr(grade, 'date', ''),
                                    'category': getattr(grade, 'category', ''),
                                    'teacher': getattr(grade, 'teacher', ''),
                                    'semester': getattr(grade, 'semester', None),
                                    'type': 'numeric'
                                })

                # Process descriptive grades (only current semester, many are actually numeric)
                if descriptive_grades:
                    for subject_grades in descriptive_grades:
                        for subject, grades_list in subject_grades.items():
                            for desc_grade in grades_list:
                                if getattr(desc_grade, "semester", None) != current_sem:
                                    continue
                                grade_val = desc_grade.grade.strip() if hasattr(desc_grade, 'grade') and desc_grade.grade else ''
                                if grade_val and (grade_val.replace('+', '').replace('-', '').isdigit() or
                                                grade_val in ['1', '2', '3', '4', '5', '6', '1+', '1-', '2+', '2-',
                                                             '3+', '3-', '4+', '4-', '5+', '5-', '6+', '6-']):
                                    all_grades.append({
                                        'subject': subject,
                                        'grade': desc_grade.grade,
                                        'date': getattr(desc_grade, 'date', ''),
                                        'category': getattr(desc_grade, 'desc', '').split('\n')[0] if hasattr(desc_grade, 'desc') else '',
                                        'teacher': getattr(desc_grade, 'teacher', ''),
                                        'semester': getattr(desc_grade, 'semester', None),
                                        'type': 'descriptive'
                                    })

                return all_grades

            except TokenError as ex:
                if attempt == 0 and (time.monotonic() - self._last_auth_time > 30):
                    _LOGGER.debug(
                        "Token expired fetching grades (attempt 1/2), re-authenticating..."
                    )
                    self._reset_auth()
                else:
                    _LOGGER.debug(
                        "Brak dostepu do modulu ocen (np. konto przedszkolaka / zerowki): %s",
                        ex,
                    )
                    return []
            except Exception as ex:
                if type(ex).__name__ == "ParseError":
                    return []
                _LOGGER.warning(
                    "Failed to get grades (attempt %d/2): %s",
                    attempt + 1, ex,
                )
                if attempt == 1:
                    return []

    async def async_get_messages(self, count: int = 10):
        """Get latest messages from Librus (subject and sender only, no content fetch to avoid marking as read)."""
        import time
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return []
                client = self._client

                from librus_apix.messages import get_received, message_content

                loop = asyncio.get_running_loop()
                messages = await loop.run_in_executor(None, get_received, client, 0)
                messages = messages[:count] if messages else []
                
                fetch_content = self.options.get("fetch_messages_content", False)

                result = []
                for msg in messages:
                    msg_dict = {
                        "author": getattr(msg, "author", ""),
                        "title": getattr(msg, "title", ""),
                        "date": getattr(msg, "date", ""),
                        "href": getattr(msg, "href", ""),
                        "unread": getattr(msg, "unread", False),
                        "has_attachment": getattr(msg, "has_attachment", False),
                    }
                    if fetch_content and msg_dict["href"]:
                        try:
                            msg_data = await loop.run_in_executor(None, message_content, client, msg.href)
                            content_str = msg_data.content if hasattr(msg_data, 'content') else str(msg_data)
                            msg_dict["content"] = content_str.replace("\n", "<br>") if isinstance(content_str, str) else content_str
                        except Exception as e:
                            _LOGGER.warning("Could not fetch content for message %s: %s", msg.href, e)
                            msg_dict["content"] = None
                    
                    result.append(msg_dict)

                return result

            except TokenError as ex:
                if attempt == 0 and (time.monotonic() - self._last_auth_time > 30):
                    _LOGGER.debug(
                        "Token expired fetching messages (attempt 1/2), re-authenticating..."
                    )
                    self._reset_auth()
                else:
                    _LOGGER.debug("Brak dostepu do wiadomosci: %s", ex)
                    return []
            except Exception as ex:
                if type(ex).__name__ == "ParseError":
                    return []
                _LOGGER.warning(
                    "Failed to get messages (attempt %d/2): %s",
                    attempt + 1, ex,
                )
                if attempt == 1:
                    return []

    async def async_get_homework(self):
        """Get upcoming homework assignments from Librus (next 30 days)."""
        import time
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return []

                from librus_apix.homework import get_homework
                from datetime import date as _date, timedelta

                today = _date.today()
                date_from = today.strftime("%Y-%m-%d")
                date_to = (today + timedelta(days=30)).strftime("%Y-%m-%d")

                loop = asyncio.get_running_loop()
                hw = await loop.run_in_executor(
                    None, get_homework, self._client, date_from, date_to
                )
                return hw if hw is not None else []

            except TokenError as ex:
                if attempt == 0 and (time.monotonic() - self._last_auth_time > 30):
                    _LOGGER.debug(
                        "Token expired fetching homework (attempt 1/2), re-authenticating..."
                    )
                    self._reset_auth()
                else:
                    _LOGGER.debug("Brak dostepu do modulu zadan domowych (np. zerowka): %s", ex)
                    return []
            except Exception as ex:
                if type(ex).__name__ == "ParseError":
                    return []
                _LOGGER.warning(
                    "Failed to get homework (attempt %d/2): %s",
                    attempt + 1, ex,
                )
                if attempt == 1:
                    return []

    async def async_get_schedule(self):
        """Get upcoming calendar events from Librus (current + next month, filtered to future dates)."""
        import time
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return []

                from librus_apix.schedule import get_schedule
                from datetime import date as _date

                today = _date.today()
                loop = asyncio.get_running_loop()

                def _fetch_two_months():
                    events = []
                    for year, month in [
                        (today.year, today.month),
                        (
                            today.year + 1 if today.month == 12 else today.year,
                            1 if today.month == 12 else today.month + 1,
                        ),
                    ]:
                        monthly = get_schedule(self._client, str(month).zfill(2), str(year))
                        if not monthly:
                            continue
                        for day_num, day_events in monthly.items():
                            try:
                                event_date = _date(year, month, int(day_num))
                            except ValueError:
                                continue
                            if event_date < today:
                                continue
                            dni = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]
                            for ev in day_events:
                                events.append({
                                    "data": event_date.strftime("%Y-%m-%d"),
                                    "tydzien": dni[event_date.weekday()],
                                    "tytul": getattr(ev, "title", ""),
                                    "przedmiot": getattr(ev, "subject", ""),
                                    "godzina": getattr(ev, "hour", ""),
                                    "numer_lekcji": getattr(ev, "number", ""),
                                    "szczegoly": getattr(ev, "data", ""),
                                    "href": getattr(ev, "href", ""),
                                })
                    return sorted(events, key=lambda e: e["data"])

                result = await loop.run_in_executor(None, _fetch_two_months)
                return result if result is not None else []

            except TokenError as ex:
                if attempt == 0 and (time.monotonic() - self._last_auth_time > 30):
                    _LOGGER.debug(
                        "Token expired fetching schedule (attempt 1/2), re-authenticating..."
                    )
                    self._reset_auth()
                else:
                    _LOGGER.debug("Brak dostepu do terminarza (np. zerowka): %s", ex)
                    return []
            except Exception as ex:
                if type(ex).__name__ == "ParseError":
                    return []
                _LOGGER.warning(
                    "Failed to get schedule (attempt %d/2): %s",
                    attempt + 1, ex,
                )
                if attempt == 1:
                    return []

    async def async_get_timetable(self):
        """Get timetable (plan lekcji) from Librus."""
        import time
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return []
                client = self._client

                from librus_apix.timetable import get_timetable
                from datetime import date as _date, datetime, timedelta

                today = _date.today()
                monday = today - timedelta(days=today.weekday())
                next_monday = monday + timedelta(days=7)

                loop = asyncio.get_running_loop()
                
                def _fetch_two_weeks():
                    tt1 = get_timetable(client, datetime.combine(monday, datetime.min.time()))
                    tt2 = get_timetable(client, datetime.combine(next_monday, datetime.min.time()))
                    return (tt1 or []) + (tt2 or [])
                    
                timetable = await loop.run_in_executor(None, _fetch_two_weeks)
                if not timetable:
                    return []

                result = []
                dni_nazwy = ["Poniedziałek", "Wtorek", "Środa", "Czwartek", "Piątek", "Sobota", "Niedziela"]
                
                start_idx = today.weekday()
                
                for i in range(start_idx, min(start_idx + 7, len(timetable))):
                    day = timetable[i]
                    day_date = (monday + timedelta(days=i)).strftime("%Y-%m-%d")
                    dzien_tyg = dni_nazwy[i % 7]
                    
                    day_list = []
                    for period in day:
                        if getattr(period, "subject", None):
                            subject = period.subject
                            teacher_and_classroom = getattr(period, "teacher_and_classroom", "")

                            if getattr(period, "info", None):
                                for info_val in period.info.values():
                                    if isinstance(info_val, dict):
                                        subject = subject.strip().replace("\n", " ")
                                        teacher_and_classroom = teacher_and_classroom.strip()

                                        subject_swap = info_val.get("subject_swap", "").strip()
                                        old_subject = subject_swap.split("->")[0].strip() if "->" in subject_swap else subject_swap
                                        if old_subject and old_subject.lower() != subject.lower():
                                            subject = f"{old_subject} ➔ {subject}"
                                        
                                        teacher_swap = info_val.get("teacher_swap", "").strip()
                                        classroom_swap = info_val.get("classroom_swap", "").strip()
                                        
                                        tc_parts = teacher_and_classroom.rsplit("-", 1)
                                        curr_teacher = tc_parts[0].strip()
                                        curr_room = tc_parts[1].strip() if len(tc_parts) > 1 else ""
                                        
                                        new_teacher = curr_teacher
                                        if teacher_swap:
                                            old_teacher = teacher_swap.split("->")[0].strip() if "->" in teacher_swap else teacher_swap
                                            if old_teacher and not curr_teacher.startswith(old_teacher):
                                                new_teacher = f"({old_teacher} ➔ {curr_teacher})"
                                                
                                        new_room = curr_room
                                        if classroom_swap:
                                            old_room = classroom_swap.split("->")[0].strip() if "->" in classroom_swap else classroom_swap
                                            if old_room and old_room != curr_room:
                                                if not curr_room or curr_room == "[brak]":
                                                    new_room = f"({old_room} ➔ brak)"
                                                else:
                                                    new_room = f"({old_room} ➔ {curr_room})"
                                                
                                        if new_teacher and new_room:
                                            teacher_and_classroom = f"{new_teacher} - {new_room}"
                                        elif new_teacher:
                                            teacher_and_classroom = new_teacher
                                        elif new_room:
                                            teacher_and_classroom = f"Sala {new_room}"
                                        break

                            day_list.append({
                                "przedmiot": subject,
                                "nauczyciel_i_sala": teacher_and_classroom,
                                "godzina_od": getattr(period, "date_from", ""),
                                "godzina_do": getattr(period, "date_to", ""),
                                "data": getattr(period, "date", "") or day_date,
                                "numer": getattr(period, "number", 0),
                            })
                    result.append({
                        "dzien_tygodnia": dzien_tyg,
                        "data": day_date,
                        "lekcje": day_list
                    })

                return result

            except TokenError as ex:
                if attempt == 0 and (time.monotonic() - self._last_auth_time > 30):
                    _LOGGER.debug(
                        "Token expired fetching timetable (attempt 1/2), re-authenticating..."
                    )
                    self._reset_auth()
                else:
                    _LOGGER.debug("Brak dostepu do planu lekcji (np. zerowka / wakacje): %s", ex)
                    return []
            except Exception as ex:
                if type(ex).__name__ == "ParseError":
                    _LOGGER.info("Brak planu lekcji w tym tygodniu (wakacje/brak danych). Zwracam pusty plan.")
                    return []
                _LOGGER.warning(
                    "Failed to get timetable (attempt %d/2): %s",
                    attempt + 1, ex,
                )
                if attempt == 1:
                    return []

    async def async_get_student_information(self):
        """Get student information from Librus."""
        import time
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return None

                from librus_apix.student_information import get_student_information

                loop = asyncio.get_running_loop()
                return await loop.run_in_executor(None, get_student_information, self._client)

            except TokenError as ex:
                if attempt == 0 and (time.monotonic() - self._last_auth_time > 30):
                    _LOGGER.debug(
                        "Token expired fetching student information (attempt 1/2), re-authenticating..."
                    )
                    self._reset_auth()
                else:
                    _LOGGER.debug(
                        "Brak dostepu do informacji o uczniu / szczesliwym numerku (np. zerowka): %s",
                        ex,
                    )
                    return None
            except Exception as ex:
                if type(ex).__name__ == "ParseError":
                    return None
                _LOGGER.warning(
                    "Failed to get student information (attempt %d/2): %s",
                    attempt + 1, ex,
                )
                if attempt == 1:
                    return None

    async def async_get_attendance(self):
        """Get attendance from Librus."""
        import time
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return []

                from librus_apix.attendance import get_attendance
                loop = asyncio.get_running_loop()
                attendance = await loop.run_in_executor(None, get_attendance, self._client)
                
                result = []
                if attendance:
                    for sem in attendance:
                        for a in sem:
                            result.append({
                                "symbol": getattr(a, "symbol", ""),
                                "typ": getattr(a, "type", ""),
                                "data": getattr(a, "date", ""),
                                "przedmiot": getattr(a, "subject", ""),
                                "nauczyciel": getattr(a, "teacher", ""),
                                "godzina": getattr(a, "period", 0)
                            })
                return result
            except TokenError as ex:
                if attempt == 0 and (time.monotonic() - self._last_auth_time > 30):
                    _LOGGER.debug("Token expired fetching attendance (attempt 1/2), re-authenticating...")
                    self._reset_auth()
                else:
                    _LOGGER.debug("Brak dostepu do frekwencji (np. zerowka): %s", ex)
                    return []
            except Exception as ex:
                if type(ex).__name__ == "ParseError":
                    return []
                _LOGGER.warning("Failed to get attendance (attempt %d/2): %s", attempt + 1, ex)
                if attempt == 1:
                    return []

    async def async_get_announcements(self):
        """Get announcements from Librus."""
        import time
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return []

                from librus_apix.announcements import get_announcements
                loop = asyncio.get_running_loop()
                ann = await loop.run_in_executor(None, get_announcements, self._client)
                
                result = []
                if ann:
                    for a in ann:
                        result.append({
                            "tytul": getattr(a, "title", ""),
                            "nadawca": getattr(a, "author", ""),
                            "opis": getattr(a, "description", ""),
                            "data": getattr(a, "date", "")
                        })
                return result
            except TokenError as ex:
                if attempt == 0 and (time.monotonic() - self._last_auth_time > 30):
                    _LOGGER.debug("Token expired fetching announcements (attempt 1/2), re-authenticating...")
                    self._reset_auth()
                else:
                    _LOGGER.debug("Brak dostepu do ogloszen: %s", ex)
                    return []
            except Exception as ex:
                if type(ex).__name__ == "ParseError":
                    return []
                _LOGGER.warning("Failed to get announcements (attempt %d/2): %s", attempt + 1, ex)
                if attempt == 1:
                    return []


async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
    """Set up the Librus APIX component."""
    hass.data.setdefault(DOMAIN, {})
    
    if DOMAIN in config:
        username = config[DOMAIN][CONF_USERNAME]
        password = config[DOMAIN][CONF_PASSWORD]
        
        client = LibrusApiClient(username, password)
        hass.data[DOMAIN]["client"] = client
        
        # Test authentication
        if not await client.async_authenticate():
            _LOGGER.error("Failed to authenticate")
            return False

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Librus APIX from a config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    options = entry.options
    
    client = LibrusApiClient(username, password, options)
    
    # Test authentication
    if not await client.async_authenticate():
        _LOGGER.error("Failed to authenticate")
        return False
    
    entry.async_on_unload(entry.add_update_listener(update_listener))
    
    from .sensor import LibrusDataUpdateCoordinator
    coordinator = LibrusDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()
    client.coordinator = coordinator
    
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = client
    
    # Setup platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    
    return unload_ok

async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)