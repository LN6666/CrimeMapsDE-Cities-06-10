"""Stable city identifiers and source boundaries for the second city group."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class CitySpec:
    slug: str
    display_name: str
    metric_epsg: int
    source_type: str
    source_url: str
    default_db: str

    def export(self) -> dict:
        return asdict(self)


CITIES: dict[str, CitySpec] = {
    "dusseldorf": CitySpec(
        "dusseldorf", "Düsseldorf", 25832, "police_authored_newsroom",
        "https://www.presseportal.de/blaulicht/nr/13248",
        ".runtime/cities/dusseldorf/police.sqlite",
    ),
    "stuttgart": CitySpec(
        "stuttgart", "Stuttgart", 25832, "police_authored_newsroom",
        "https://www.presseportal.de/blaulicht/nr/110977",
        ".runtime/cities/stuttgart/police.sqlite",
    ),
    "leipzig": CitySpec(
        "leipzig", "Leipzig", 25833, "official_media_manual_input",
        "https://medienservice.sachsen.de/medien/?search%5Binstitution_ids%5D%5B%5D=10976",
        ".runtime/cities/leipzig/police.sqlite",
    ),
    "dortmund": CitySpec(
        "dortmund", "Dortmund", 25832, "native_police_archive",
        "https://dortmund.polizei.nrw/presse/pressemitteilungen",
        ".runtime/cities/dortmund/police.sqlite",
    ),
    "bremen": CitySpec(
        "bremen", "Bremen", 25832, "native_police_archive_manual_input",
        "https://www.polizei.bremen.de/news/pressestelle/pressearchiv-5034",
        ".runtime/cities/bremen/police.sqlite",
    ),
}
