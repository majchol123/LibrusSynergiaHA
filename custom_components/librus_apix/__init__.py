"""The Librus APIX integration with Timetable and Homework Details support."""

import asyncio
import logging
import traceback
from datetime import date, timedelta
from typing import Dict, Any, List

import voluptuous as vol
from bs4 import BeautifulSoup
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_USERNAME, CONF_PASSWORD
from homeassistant.helpers import config_validation as cv

from librus_apix.client import Client, new_client
from librus_apix.exceptions import TokenError
from librus_apix.helpers import no_access_check

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

WEEKDAYS_PL = [
    "Poniedziałek",
    "Wtorek",
    "Środa",
    "Czwartek",
    "Piątek",
    "Sobota",
    "Niedziela",
]


def _current_semester() -> int:
    """Return current Polish school semester number (1 or 2)."""
    m = date.today().month
    return 1 if m >= 9 else 2


PLATFORMS = ["sensor"]

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


def _parse_timetable_week(client: Client, monday_date: date) -> Dict[str, List[Dict[str, Any]]]:
    """Fetch and safely parse Librus timetable for the week starting on monday_date."""
    sunday_date = monday_date + timedelta(days=6)
    week_param = f"{monday_date.strftime('%Y-%m-%d')}_{sunday_date.strftime('%Y-%m-%d')}"
    response = client.post(client.TIMETABLE_URL, data={"tydzien": week_param})
    soup = no_access_check(BeautifulSoup(response.text, "lxml"))

    days_map: Dict[str, List[Dict[str, Any]]] = {}
    for offset in range(7):
        d_str = (monday_date + timedelta(days=offset)).strftime("%Y-%m-%d")
        days_map[d_str] = []

    period_rows = soup.select("table.decorated.plan-lekcji tr.line1")
    if not period_rows:
        return days_map

    for row in period_rows:
        td_center = row.select_one("td.center")
        try:
            lesson_number = int(td_center.text.strip()) if td_center else 0
        except ValueError:
            lesson_number = 0

        th_time = row.select_one("th")
        row_time_from, row_time_to = "", ""
        if th_time:
            raw_time = th_time.text.replace("\xa0", " ").replace("&nbsp;", " ").strip()
            if "-" in raw_time:
                parts = [p.strip() for p in raw_time.split("-", 1)]
                row_time_from, row_time_to = parts[0], parts[1]

        cells = row.select('td[id="timetableEntryBox"]')
        for weekday_idx, cell in enumerate(cells[:7]):
            expected_date = (monday_date + timedelta(days=weekday_idx)).strftime("%Y-%m-%d")
            cell_date = cell.attrs.get("data-date", expected_date)
            time_from = cell.attrs.get("data-time_from", row_time_from)
            time_to = cell.attrs.get("data-time_to", row_time_to)

            lesson_divs = cell.select("div.text")
            if not lesson_divs:
                continue

            subjects = []
            teachers_rooms = []
            for div in lesson_divs:
                b_tag = div.select_one("b")
                if b_tag:
                    subjects.append(b_tag.text.strip())
                raw_txt = (
                    div.text.replace("\xa0", " ")
                    .replace("\n", " ")
                    .replace("&nbsp;", " ")
                    .strip()
                )
                if "-" in raw_txt:
                    tc_part = "-".join(raw_txt.split("-")[1:]).strip()
                    if tc_part:
                        teachers_rooms.append(tc_part)

            subject_str = " / ".join(subjects)
            if not subject_str:
                continue

            tooltip = cell.select_one("div.center.plan-lekcji-info")
            flag = tooltip.text.strip() if tooltip else ""
            swap_info = ""
            a_tag = cell.select_one("a[title]")
            if a_tag and a_tag.attrs.get("title"):
                swap_info = (
                    a_tag.attrs["title"]
                    .replace("<b>", "")
                    .replace("</b>", "")
                    .replace("<br>", ", ")
                    .replace("<br/>", ", ")
                    .replace("\xa0", " ")
                    .strip()
                )

            days_map.setdefault(cell_date, []).append(
                {
                    "numer": lesson_number,
                    "od": time_from,
                    "do": time_to,
                    "godziny": f"{time_from} - {time_to}" if time_from and time_to else "",
                    "przedmiot": subject_str,
                    "nauczyciel_sala": " / ".join(teachers_rooms),
                    "status": flag,
                    "szczegoly_zmiany": swap_info,
                    "odwolana": "odwołane" in flag.lower(),
                    "zastepstwo": "zastępstwo" in flag.lower(),
                }
            )

    for d_key in days_map:
        days_map[d_key].sort(key=lambda item: item["numer"])

    return days_map


class LibrusApiClient:
    """Class to interface with the Librus API."""

    def __init__(self, username: str, password: str):
        """Initialize the client."""
        self.username = username
        self.password = password
        self._client: Client = None
        self._token = None
        self._auth_lock = asyncio.Lock()

    def _reset_auth(self) -> None:
        """Reset authentication state to force re-authentication on next call."""
        self._client = None
        self._token = None

    async def async_authenticate(self):
        """Authenticate with Librus API."""
        async with self._auth_lock:
            try:
                loop = asyncio.get_running_loop()
                self._client = await loop.run_in_executor(None, new_client)
                self._token = await loop.run_in_executor(
                    None, self._client.get_token, self.username, self.password
                )
                return True
            except Exception as ex:
                _LOGGER.error("Authentication failed: %s\n%s", ex, traceback.format_exc())
                self._reset_auth()
                return False

    async def async_get_timetable(self) -> Dict[str, Any]:
        """Get timetable for current and next week."""
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return None

                today = date.today()
                current_monday = today - timedelta(days=today.weekday())
                next_monday = current_monday + timedelta(days=7)

                loop = asyncio.get_running_loop()

                def _fetch():
                    combined: Dict[str, List[Dict[str, Any]]] = {}
                    combined.update(_parse_timetable_week(self._client, current_monday))
                    try:
                        combined.update(_parse_timetable_week(self._client, next_monday))
                    except Exception as next_err:
                        _LOGGER.debug("Next week timetable not available yet: %s", next_err)
                    return combined

                return await loop.run_in_executor(None, _fetch)

            except TokenError:
                self._reset_auth()
                if attempt == 1:
                    return None
            except Exception as ex:
                _LOGGER.error(
                    "Failed to get timetable (attempt %d/2): %s\n%s",
                    attempt + 1,
                    ex,
                    traceback.format_exc(),
                )
                self._reset_auth()
                if attempt == 1:
                    return None

    async def async_get_grades(self):
        """Get grades from Librus."""
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return None
                client = self._client
                from librus_apix.grades import get_grades

                loop = asyncio.get_running_loop()
                numeric_grades, _, descriptive_grades = await loop.run_in_executor(
                    None, get_grades, client, "all"
                )

                current_sem = _current_semester()
                all_grades = []

                for subject_grades in numeric_grades:
                    for subject, grades_list in subject_grades.items():
                        for grade in grades_list:
                            if grade.semester != current_sem:
                                continue
                            all_grades.append({
                                "subject": subject,
                                "grade": grade.grade,
                                "date": grade.date,
                                "category": grade.category,
                                "teacher": getattr(grade, "teacher", ""),
                                "semester": grade.semester,
                                "type": "numeric",
                            })

                for subject_grades in descriptive_grades:
                    for subject, grades_list in subject_grades.items():
                        for desc_grade in grades_list:
                            if desc_grade.semester != current_sem:
                                continue
                            grade_val = desc_grade.grade.strip()
                            if grade_val and (
                                grade_val.replace("+", "").replace("-", "").isdigit()
                            ):
                                all_grades.append({
                                    "subject": subject,
                                    "grade": desc_grade.grade,
                                    "date": desc_grade.date,
                                    "category": getattr(desc_grade, "desc", "").split("\n")[0]
                                    if hasattr(desc_grade, "desc")
                                    else "",
                                    "teacher": getattr(desc_grade, "teacher", ""),
                                    "semester": desc_grade.semester,
                                    "type": "descriptive",
                                })

                return all_grades

            except TokenError:
                self._reset_auth()
                if attempt == 1:
                    return []
            except Exception as ex:
                _LOGGER.warning("Failed to get grades (attempt %d/2): %s", attempt + 1, ex)
                self._reset_auth()
                if attempt == 1:
                    return []

    async def async_get_messages(self, count: int = 10):
        """Get latest messages from Librus without marking as read."""
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return None
                from librus_apix.messages import get_received

                loop = asyncio.get_running_loop()
                messages = await loop.run_in_executor(None, get_received, self._client, 0)
                messages = messages[:count] if messages else []
                return [
                    {
                        "author": msg.author,
                        "title": msg.title,
                        "date": msg.date,
                        "href": msg.href,
                        "unread": msg.unread,
                        "has_attachment": msg.has_attachment,
                    }
                    for msg in messages
                ]
            except TokenError:
                self._reset_auth()
                if attempt == 1:
                    return None
            except Exception as ex:
                _LOGGER.warning("Failed to get messages (attempt %d/2): %s", attempt + 1, ex)
                self._reset_auth()
                if attempt == 1:
                    return None

    async def async_get_homework(self):
        """Get upcoming homework assignments (from Monday of current week + 30 days) with descriptions."""
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return None

                from librus_apix.homework import get_homework, homework_detail

                today = date.today()
                date_from = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
                date_to = (today + timedelta(days=30)).strftime("%Y-%m-%d")

                loop = asyncio.get_running_loop()

                def _fetch_hw():
                    hw_list = get_homework(self._client, date_from, date_to)
                    enriched = []
                    for hw in hw_list:
                        opis = ""
                        if hw.href:
                            try:
                                details = homework_detail(self._client, hw.href)
                                opis = details.get("Opis", "").strip()
                            except Exception:
                                opis = ""
                        enriched.append(
                            {
                                "przedmiot": hw.subject.strip(),
                                "kategoria": hw.category.strip(),
                                "nauczyciel": hw.teacher.strip(),
                                "lekcja": hw.lesson.strip(),
                                "opis": opis,
                                "data_zadania": hw.task_date.strip(),
                                "termin": hw.completion_date.strip(),
                                "termin_data": hw.completion_date.strip().split(" ")[0],
                                "href": hw.href,
                            }
                        )
                    return sorted(enriched, key=lambda z: z["termin"])

                return await loop.run_in_executor(None, _fetch_hw)

            except TokenError:
                self._reset_auth()
                if attempt == 1:
                    return None
            except Exception as ex:
                _LOGGER.warning("Failed to get homework (attempt %d/2): %s", attempt + 1, ex)
                self._reset_auth()
                if attempt == 1:
                    return None

    async def async_get_schedule(self):
        """Get calendar events from Librus (from Monday of current week + next month)."""
        for attempt in range(2):
            try:
                if not self._client or not self._token:
                    if not await self.async_authenticate():
                        return None

                from librus_apix.schedule import get_schedule

                today = date.today()
                monday_this_week = today - timedelta(days=today.weekday())
                loop = asyncio.get_running_loop()

                def _fetch_two_months():
                    events = []
                    months_to_fetch = {
                        (monday_this_week.year, monday_this_week.month),
                        (today.year, today.month),
                    }
                    next_m = (
                        (today.year + 1, 1)
                        if today.month == 12
                        else (today.year, today.month + 1)
                    )
                    months_to_fetch.add(next_m)

                    for year, month in sorted(months_to_fetch):
                        monthly = get_schedule(self._client, str(month).zfill(2), str(year))
                        for day_num, day_events in monthly.items():
                            try:
                                event_date = date(year, month, int(day_num))
                            except ValueError:
                                continue
                            if event_date < monday_this_week:
                                continue
                            for ev in day_events:
                                opis = ""
                                if isinstance(ev.data, dict):
                                    opis = ev.data.get("Opis", "")
                                    if opis == "unknown":
                                        opis = ""
                                events.append({
                                    "data": event_date.strftime("%Y-%m-%d"),
                                    "tydzien": WEEKDAYS_PL[event_date.weekday()],
                                    "tytul": ev.title,
                                    "przedmiot": ev.subject if ev.subject != "unspecified" else "",
                                    "godzina": str(ev.hour) if ev.hour != "unknown" else "",
                                    "numer_lekcji": str(ev.number) if ev.number != "unknown" else "",
                                    "opis": opis,
                                    "szczegoly": ev.data,
                                    "href": ev.href,
                                })
                    return sorted(events, key=lambda e: (e["data"], str(e["numer_lekcji"])))

                return await loop.run_in_executor(None, _fetch_two_months)

            except TokenError:
                self._reset_auth()
                if attempt == 1:
                    return None
            except Exception as ex:
                _LOGGER.warning("Failed to get schedule (attempt %d/2): %s", attempt + 1, ex)
                self._reset_auth()
                if attempt == 1:
                    return None

    async def async_get_student_information(self):
        """Get student information from Librus."""
        try:
            if not self._client or not self._token:
                if not await self.async_authenticate():
                    return None
            from librus_apix.student_information import get_student_information

            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, get_student_information, self._client)
        except Exception as ex:
            _LOGGER.warning("Failed to get student information: %s", ex)
            self._reset_auth()
            return None


async def async_setup(hass: HomeAssistant, config: Dict[str, Any]) -> bool:
    """Set up the Librus APIX component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Librus APIX from a config entry."""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]

    client = LibrusApiClient(username, password)
    if not await client.async_authenticate():
        _LOGGER.error("Failed to authenticate Librus APIX")
        return False

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = client

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
