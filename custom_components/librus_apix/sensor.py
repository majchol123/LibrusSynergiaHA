"""Sensor platform for Librus APIX integration."""

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import DOMAIN, SCAN_INTERVAL
from . import WEEKDAYS_PL

_LOGGER = logging.getLogger(__name__)


def _jest_nowa(date_str: str) -> bool:
    """Check if date string is from today or yesterday."""
    if not date_str:
        return False
    wczoraj = date.today() - timedelta(days=1)
    for fmt in (
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            d = datetime.strptime(date_str.strip(), fmt).date()
            return d >= wczoraj
        except ValueError:
            continue
    return False


def _srednia_ocen(oceny: List[Dict]) -> Optional[float]:
    """Calculate numeric grade average."""
    wartosci = []
    for g in oceny:
        grade_str = g.get("ocena", "")
        try:
            base = float(grade_str[0])
            if len(grade_str) > 1:
                if "+" in grade_str:
                    base += 0.5
                elif "-" in grade_str:
                    base -= 0.25
            wartosci.append(base)
        except (ValueError, IndexError):
            continue
    return round(sum(wartosci) / len(wartosci), 2) if wartosci else None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Librus APIX sensors."""
    client = hass.data[DOMAIN][config_entry.entry_id]

    coordinator = LibrusDataUpdateCoordinator(hass, client)
    await coordinator.async_config_entry_first_refresh()

    entities: List[SensorEntity] = [
        LibrusUczenSensor(coordinator, config_entry),
        LibrusSzczesliwyNumerekSensor(coordinator, config_entry),
        LibrusPlanLekcjiSensor(coordinator, config_entry),
        LibrusOcenySensor(coordinator, config_entry),
        LibrusWiadomosciSensor(coordinator, config_entry),
        LibrusZadaniaSensor(coordinator, config_entry),
        LibrusTerminarzSensor(coordinator, config_entry),
        LibrusSredniaOcenSensor(coordinator, config_entry),
    ]

    for subject in coordinator.data.get("oceny_wg_przedmiotu", {}).keys():
        entities.append(LibrusPrzedmiotSensor(coordinator, subject, config_entry))
        entities.append(LibrusSredniaPrzedmiotuSensor(coordinator, subject, config_entry))

    async_add_entities(entities)


EVENT_NOWA_WIADOMOSC = f"{DOMAIN}_nowa_wiadomosc"
EVENT_NOWA_OCENA = f"{DOMAIN}_nowa_ocena"
EVENT_NOWE_ZADANIE = f"{DOMAIN}_nowe_zadanie"
EVENT_NOWE_ZDARZENIE = f"{DOMAIN}_nowe_zdarzenie"


class LibrusDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator fetching data from Librus Synergia."""

    def __init__(self, hass: HomeAssistant, client: Any) -> None:
        """Initialize coordinator."""
        self.client = client
        self._seen_message_hrefs: set = set()
        self._seen_grade_ids: set = set()
        self._seen_homework_ids: set = set()
        self._seen_schedule_ids: set = set()
        self._first_run: bool = True
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )

    async def _async_update_data(self) -> Dict[str, Any]:
        """Fetch latest data from Librus API."""
        current_sem = 1 if date.today().month >= 9 else 2
        prev = self.data or {}

        try:
            student_info = await self.client.async_get_student_information()
            timetable = await self.client.async_get_timetable()
            grades = await self.client.async_get_grades()
            messages = await self.client.async_get_messages(count=10)
            homework_raw = await self.client.async_get_homework()
            schedule_raw = await self.client.async_get_schedule()

            if grades is None:
                grades = prev.get("oceny", [])

            oceny_wg_przedmiotu: Dict[str, List[Dict]] = {}
            for grade in grades:
                subject = grade["subject"]
                if subject not in oceny_wg_przedmiotu:
                    oceny_wg_przedmiotu[subject] = []
                oceny_wg_przedmiotu[subject].append({
                    "ocena": grade["grade"],
                    "data": grade["date"],
                    "kategoria": grade["category"],
                    "nauczyciel": grade["teacher"],
                    "semestr": grade.get("semester"),
                    "jest_nowa": _jest_nowa(grade["date"]),
                })

            wiadomosci = (
                self._build_wiadomosci(messages)
                if messages is not None
                else prev.get("wiadomosci", [])
            )
            zadania = (
                homework_raw
                if homework_raw is not None
                else prev.get("zadania", [])
            )
            terminarz = (
                schedule_raw
                if schedule_raw is not None
                else prev.get("terminarz", [])
            )
            plan_lekcji = (
                timetable
                if timetable is not None
                else prev.get("plan_lekcji", {})
            )

            result = {
                "student_info": student_info or prev.get("student_info"),
                "plan_lekcji": plan_lekcji,
                "oceny": grades,
                "oceny_wg_przedmiotu": oceny_wg_przedmiotu,
                "wiadomosci": wiadomosci,
                "zadania": zadania,
                "terminarz": terminarz,
                "semestr_biezacy": current_sem,
            }

            if self._first_run:
                self._first_run = False
                for msg in wiadomosci:
                    self._seen_message_hrefs.add(msg["href"])
                for grade in grades:
                    self._seen_grade_ids.add(
                        (grade["subject"], grade["date"], grade["grade"])
                    )
                for zadanie in zadania:
                    self._seen_homework_ids.add(
                        (zadanie["przedmiot"], zadanie["termin"], zadanie["kategoria"])
                    )
                for zdarzenie in terminarz:
                    self._seen_schedule_ids.add(
                        (zdarzenie["data"], zdarzenie["tytul"], zdarzenie["przedmiot"])
                    )
            else:
                self._fire_events(wiadomosci, grades)
                self._fire_homework_events(zadania)
                self._fire_schedule_events(terminarz)

            return result

        except Exception as err:
            if prev:
                _LOGGER.warning("Blad odczytu Librus API (%s), zwracam dane z cache", err)
                return prev
            raise UpdateFailed(f"Blad komunikacji z API Librus: {err}") from err

    def _fire_events(self, messages: List[Dict], grades: List[Dict]) -> None:
        for msg in messages:
            href = msg.get("href", "")
            if href and href not in self._seen_message_hrefs:
                self._seen_message_hrefs.add(href)
                self.hass.bus.fire(
                    EVENT_NOWA_WIADOMOSC,
                    {
                        "nadawca": msg.get("author", ""),
                        "temat": msg.get("title", ""),
                        "data": msg.get("date", ""),
                        "ma_zalacznik": msg.get("has_attachment", False),
                    },
                )

        for grade in grades:
            grade_id = (grade["subject"], grade["date"], grade["grade"])
            if grade_id not in self._seen_grade_ids:
                self._seen_grade_ids.add(grade_id)
                self.hass.bus.fire(
                    EVENT_NOWA_OCENA,
                    {
                        "przedmiot": grade["subject"],
                        "ocena": grade["grade"],
                        "data": grade["date"],
                        "kategoria": grade["category"],
                        "nauczyciel": grade["teacher"],
                    },
                )

    def _fire_schedule_events(self, terminarz: List[Dict]) -> None:
        for zdarzenie in terminarz:
            ev_id = (zdarzenie["data"], zdarzenie["tytul"], zdarzenie["przedmiot"])
            if ev_id not in self._seen_schedule_ids:
                self._seen_schedule_ids.add(ev_id)
                self.hass.bus.fire(
                    EVENT_NOWE_ZDARZENIE,
                    {
                        "data": zdarzenie["data"],
                        "tytul": zdarzenie["tytul"],
                        "przedmiot": zdarzenie["przedmiot"],
                        "godzina": zdarzenie["godzina"],
                    },
                )

    def _build_wiadomosci(self, messages: Optional[List[Dict]]) -> List[Dict]:
        result = []
        for msg in messages or []:
            msg["jest_nowa"] = _jest_nowa(msg.get("date", ""))
            result.append(msg)
        return result

    def _fire_homework_events(self, zadania: List[Dict]) -> None:
        for zadanie in zadania:
            hw_id = (zadanie["przedmiot"], zadanie["termin"], zadanie["kategoria"])
            if hw_id not in self._seen_homework_ids:
                self._seen_homework_ids.add(hw_id)
                self.hass.bus.fire(
                    EVENT_NOWE_ZADANIE,
                    {
                        "przedmiot": zadanie["przedmiot"],
                        "kategoria": zadanie["kategoria"],
                        "termin": zadanie["termin"],
                        "nauczyciel": zadanie["nauczyciel"],
                    },
                )


def _device_info(coordinator: DataUpdateCoordinator, config_entry: ConfigEntry) -> Dict[str, Any]:
    data = coordinator.data or {}
    student_info = data.get("student_info")
    name = student_info.name if student_info else "Uczeń"
    return {
        "identifiers": {(DOMAIN, config_entry.entry_id)},
        "name": f"Librus - {name}",
        "manufacturer": "Librus",
        "model": "Synergia",
    }


class LibrusPlanLekcjiSensor(CoordinatorEntity, SensorEntity):
    """Sensor exposing the student's lesson timetable (today, tomorrow, full week)."""

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_has_entity_name = False
        self._attr_name = "Librus Plan lekcji"
        self._attr_unique_id = f"{config_entry.entry_id}_plan_lekcji"
        self._attr_icon = "mdi:timetable"

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> int:
        plan = (self.coordinator.data or {}).get("plan_lekcji", {})
        today_str = date.today().strftime("%Y-%m-%d")
        return len(plan.get(today_str, []))

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        plan: Dict[str, List[Dict[str, Any]]] = (self.coordinator.data or {}).get("plan_lekcji", {})
        today = date.today()
        today_str = today.strftime("%Y-%m-%d")
        tomorrow = today + timedelta(days=1)
        tomorrow_str = tomorrow.strftime("%Y-%m-%d")

        next_school_date = tomorrow
        for offset in range(1, 8):
            cand = today + timedelta(days=offset)
            cand_str = cand.strftime("%Y-%m-%d")
            if plan.get(cand_str):
                next_school_date = cand
                break
        next_school_str = next_school_date.strftime("%Y-%m-%d")

        lekcje_dzisiaj = plan.get(today_str, [])
        lekcje_jutro = plan.get(tomorrow_str, [])
        lekcje_kolejny_dzien = plan.get(next_school_str, [])

        godziny_dzisiaj = ""
        if lekcje_dzisiaj:
            pierwsza = lekcje_dzisiaj[0].get("od", "")
            ostatnia = lekcje_dzisiaj[-1].get("do", "")
            if pierwsza and ostatnia:
                godziny_dzisiaj = f"{pierwsza} - {ostatnia}"

        return {
            "dzisiaj_data": today_str,
            "dzisiaj_dzien": WEEKDAYS_PL[today.weekday()],
            "godziny_dzisiaj": godziny_dzisiaj,
            "liczba_lekcji_dzisiaj": len(lekcje_dzisiaj),
            "lekcje_dzisiaj": lekcje_dzisiaj,
            "jutro_data": tomorrow_str,
            "jutro_dzien": WEEKDAYS_PL[tomorrow.weekday()],
            "lekcje_jutro": lekcje_jutro,
            "kolejny_dzien_szkolny_data": next_school_str,
            "kolejny_dzien_szkolny_dzien": WEEKDAYS_PL[next_school_date.weekday()],
            "lekcje_kolejny_dzien_szkolny": lekcje_kolejny_dzien,
            "plan_tygodniowy": plan,
        }


class LibrusUczenSensor(CoordinatorEntity, SensorEntity):
    """Sensor with student information."""

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_has_entity_name = False
        self._attr_name = "Librus Uczen"
        self._attr_unique_id = f"{config_entry.entry_id}_uczen"
        self._attr_icon = "mdi:account-school"

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> Optional[str]:
        info = (self.coordinator.data or {}).get("student_info")
        return info.name if info else None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        info = (self.coordinator.data or {}).get("student_info")
        if not info:
            return {}
        return {
            "klasa": info.class_name,
            "numer_w_klasie": info.number,
            "wychowawca": info.tutor,
            "szkola": info.school,
            "szczesliwy_numerek": info.lucky_number,
        }


class LibrusSzczesliwyNumerekSensor(CoordinatorEntity, SensorEntity):
    """Sensor with the lucky number of the day."""

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_has_entity_name = False
        self._attr_name = "Librus Szczesliwy numerek"
        self._attr_unique_id = f"{config_entry.entry_id}_szczesliwy_numerek"
        self._attr_icon = "mdi:numeric"

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> Any:
        info = (self.coordinator.data or {}).get("student_info")
        return info.lucky_number if info else None


class LibrusOcenySensor(CoordinatorEntity, SensorEntity):
    """Sensor with all grades."""

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_has_entity_name = False
        self._attr_name = "Librus Oceny"
        self._attr_unique_id = f"{config_entry.entry_id}_oceny"
        self._attr_icon = "mdi:school"

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("oceny", []))

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        data = self.coordinator.data or {}
        oceny_wg_przedmiotu = data.get("oceny_wg_przedmiotu", {})
        sa_nowe = any(
            g["jest_nowa"]
            for grades in oceny_wg_przedmiotu.values()
            for g in grades
        )
        return {
            "oceny_wg_przedmiotu": oceny_wg_przedmiotu,
            "liczba_ocen": len((self.coordinator.data or {}).get("oceny", [])),
            "liczba_przedmiotow": len(oceny_wg_przedmiotu),
            "sa_nowe_oceny": sa_nowe,
            "semestr": data.get("semestr_biezacy"),
        }


class LibrusPrzedmiotSensor(CoordinatorEntity, SensorEntity):
    """Sensor with grades for a specific subject."""

    def __init__(
        self,
        coordinator: LibrusDataUpdateCoordinator,
        subject: str,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._subject = subject
        safe_name = subject.lower().replace(" ", "_").replace("/", "_")
        self._attr_has_entity_name = False
        self._attr_name = f"Librus {subject}"
        self._attr_unique_id = f"{config_entry.entry_id}_przedmiot_{safe_name}"
        self._attr_icon = "mdi:book-open-variant"

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> Optional[str]:
        oceny = (self.coordinator.data or {}).get("oceny_wg_przedmiotu", {}).get(self._subject, [])
        if not oceny:
            return None
        return ", ".join(g["ocena"] for g in oceny)

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        oceny = (self.coordinator.data or {}).get("oceny_wg_przedmiotu", {}).get(self._subject, [])
        if not oceny:
            return {}
        srednia = _srednia_ocen(oceny)
        return {
            "oceny": oceny,
            "lista_ocen": ", ".join(g["ocena"] for g in oceny),
            "srednia": srednia,
            "sa_nowe_oceny": any(g["jest_nowa"] for g in oceny),
        }


class LibrusSredniaOcenSensor(CoordinatorEntity, SensorEntity):
    """Sensor with overall grade average."""

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_has_entity_name = False
        self._attr_name = "Librus Srednia ocen"
        self._attr_unique_id = f"{config_entry.entry_id}_srednia_ocen"
        self._attr_icon = "mdi:chart-line"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> Optional[float]:
        data = self.coordinator.data or {}
        wszystkie = [
            g
            for oceny in data.get("oceny_wg_przedmiotu", {}).values()
            for g in oceny
        ]
        return _srednia_ocen(wszystkie)


class LibrusSredniaPrzedmiotuSensor(CoordinatorEntity, SensorEntity):
    """Sensor with subject grade average."""

    def __init__(
        self,
        coordinator: LibrusDataUpdateCoordinator,
        subject: str,
        config_entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._subject = subject
        safe_name = subject.lower().replace(" ", "_").replace("/", "_")
        self._attr_has_entity_name = False
        self._attr_name = f"Librus Srednia {subject}"
        self._attr_unique_id = f"{config_entry.entry_id}_srednia_{safe_name}"
        self._attr_icon = "mdi:chart-bar"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> Optional[float]:
        oceny = (self.coordinator.data or {}).get("oceny_wg_przedmiotu", {}).get(self._subject, [])
        return _srednia_ocen(oceny)


class LibrusTerminarzSensor(CoordinatorEntity, SensorEntity):
    """Sensor with Librus schedule events (today, this week, upcoming)."""

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_has_entity_name = False
        self._attr_name = "Librus Terminarz"
        self._attr_unique_id = f"{config_entry.entry_id}_terminarz"
        self._attr_icon = "mdi:calendar-month"

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("terminarz", []))

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        terminarz = (self.coordinator.data or {}).get("terminarz", [])
        today = date.today()
        today_str = today.strftime("%Y-%m-%d")
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        monday_str = monday.strftime("%Y-%m-%d")
        sunday_str = sunday.strftime("%Y-%m-%d")

        dzisiaj = [z for z in terminarz if z.get("data", "") == today_str]
        ten_tydzien = [
            z for z in terminarz if monday_str <= z.get("data", "") <= sunday_str
        ]
        najblizsze_7_dni = [
            z
            for z in terminarz
            if today_str <= z.get("data", "") <= (today + timedelta(days=7)).strftime("%Y-%m-%d")
        ]
        return {
            "zdarzenia_dzisiaj": dzisiaj,
            "liczba_zdarzen_dzisiaj": len(dzisiaj),
            "zdarzenia_ten_tydzien": ten_tydzien,
            "zdarzenia_7_dni": najblizsze_7_dni,
            "zdarzenia": terminarz,
            "liczba_zdarzen": len(terminarz),
        }


class LibrusZadaniaSensor(CoordinatorEntity, SensorEntity):
    """Sensor with Librus homework assignments."""

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_has_entity_name = False
        self._attr_name = "Librus Zadania"
        self._attr_unique_id = f"{config_entry.entry_id}_zadania"
        self._attr_icon = "mdi:calendar-check"

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> int:
        return len((self.coordinator.data or {}).get("zadania", []))

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        zadania = (self.coordinator.data or {}).get("zadania", [])
        today = date.today()
        today_str = today.strftime("%Y-%m-%d")
        monday = today - timedelta(days=today.weekday())
        sunday = monday + timedelta(days=6)
        monday_str = monday.strftime("%Y-%m-%d")
        sunday_str = sunday.strftime("%Y-%m-%d")

        zadania_dzisiaj = [
            z
            for z in zadania
            if z.get("termin_data", z.get("termin", "")[:10]) == today_str
        ]
        zadania_ten_tydzien = [
            z
            for z in zadania
            if monday_str <= z.get("termin_data", z.get("termin", "")[:10]) <= sunday_str
        ]
        zadania_7_dni = [
            z
            for z in zadania
            if today_str
            <= z.get("termin_data", z.get("termin", "")[:10])
            <= (today + timedelta(days=7)).strftime("%Y-%m-%d")
        ]
        return {
            "zadania_dzisiaj": zadania_dzisiaj,
            "zadania_ten_tydzien": zadania_ten_tydzien,
            "zadania_7_dni": zadania_7_dni,
            "zadania": zadania,
            "liczba_zadan": len(zadania),
        }


class LibrusWiadomosciSensor(CoordinatorEntity, SensorEntity):
    """Sensor with Librus messages."""

    def __init__(self, coordinator: LibrusDataUpdateCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._config_entry = config_entry
        self._attr_has_entity_name = False
        self._attr_name = "Librus Wiadomosci"
        self._attr_unique_id = f"{config_entry.entry_id}_wiadomosci"
        self._attr_icon = "mdi:message-text"

    @property
    def device_info(self) -> Dict[str, Any]:
        return _device_info(self.coordinator, self._config_entry)

    @property
    def native_value(self) -> int:
        msgs = (self.coordinator.data or {}).get("wiadomosci", [])
        return sum(1 for m in msgs if m.get("unread", False))

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        msgs = (self.coordinator.data or {}).get("wiadomosci", [])[:5]
        return {
            "wiadomosci": [
                {
                    "nadawca": m["author"],
                    "temat": m["title"],
                    "data": m["date"],
                    "nieprzeczytana": m.get("unread", False),
                    "jest_nowa": m.get("jest_nowa", False),
                    "ma_zalacznik": m.get("has_attachment", False),
                }
                for m in msgs
            ],
            "liczba_nieprzeczytanych": sum(1 for m in msgs if m.get("unread", False)),
            "sa_nowe_wiadomosci": any(m.get("jest_nowa", False) for m in msgs),
        }
