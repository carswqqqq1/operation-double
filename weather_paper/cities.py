"""Twenty paper books. One city each. Coordinates are the resolution stations.

Timezones were checked against the Open-Meteo ensemble at these points on
2026-10-04. Hong Kong is a live daily market and is not a book here: it
resolves from the Hong Kong Observatory, and this table only lists stations
whose coordinates were checked.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class City:
    slug: str
    station: str
    latitude: str
    longitude: str
    station_name: str


CITIES: tuple[City, ...] = (
    City("tokyo", "rjtt", "35.552299", "139.779999", "Tokyo Haneda Airport"),
    City("nyc", "klga", "40.777199", "-73.872597", "LaGuardia Airport"),
    City("london", "eglc", "51.505278", "0.055278", "London City Airport"),
    City("paris", "lfpb", "48.969398", "2.441390", "Paris-Le Bourget Airport"),
    City("seoul", "rksi", "37.469101", "126.450996", "Incheon Intl Airport"),
    City("chicago", "kord", "41.9786", "-87.9048", "Chicago O'Hare Intl Airport"),
    City("miami", "kmia", "25.7932", "-80.2906", "Miami Intl Airport"),
    City("los-angeles", "klax", "33.942501", "-118.407997", "Los Angeles International Airport"),
    City("dallas", "kdal", "32.847099", "-96.851799", "Dallas Love Field"),
    City("seattle", "ksea", "47.449001", "-122.308998", "Seattle-Tacoma International Airport"),
    City("atlanta", "katl", "33.6367", "-84.428101", "Hartsfield-Jackson International Airport"),
    City("toronto", "cyyz", "43.6772", "-79.6306", "Toronto Pearson Intl Airport"),
    City("madrid", "lemd", "40.471926", "-3.56264", "Adolfo Suárez Madrid-Barajas Airport"),
    City("shanghai", "zspd", "31.1434", "121.805", "Shanghai Pudong International Airport"),
    City("beijing", "zbaa", "40.080101", "116.584999", "Beijing Capital International Airport"),
    City("taipei", "rcss", "25.069401", "121.552002", "Taipei Songshan Airport"),
    City("munich", "eddm", "48.353802", "11.7861", "Munich Airport"),
    City("wellington", "nzwn", "-41.327202", "174.804993", "Wellington Intl Airport"),
    City("singapore", "wsss", "1.35019", "103.994003", "Singapore Changi Airport"),
    City("austin", "kaus", "30.1945", "-97.669899", "Austin-Bergstrom International Airport"),
)

if not 1 <= len(CITIES) <= 20:
    raise RuntimeError("paper books must stay between 1 and 20")
if len({city.slug for city in CITIES}) != len(CITIES):
    raise RuntimeError("paper book slugs must be unique")
if CITIES[0].slug != "tokyo":
    raise RuntimeError("tokyo is the first book")
